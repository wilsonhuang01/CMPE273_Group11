"""Independent trusted worker executor: broker delivery, RPC leases, and Docker sandboxes."""

import fcntl
import json
import logging
import os
import secrets
import tempfile
import threading
import traceback
from pathlib import Path
from uuid import uuid4

import grpc
import pika
from google.protobuf.json_format import ParseDict

from compiler_optimizer.runtime.dependencies import declare_topology
from compiler_optimizer.runtime.fleet_client import fleet_connection, result_frames
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.scheduler.fleet import worker_queue
from compiler_optimizer.shared.fleet import canonical, describe_runner, encode_invocation
from compiler_optimizer.shared.lease_context import EXECUTION_LEASE
from compiler_optimizer.shared.manifest import parse_manifest, require, validate_path
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import SubmissionPackage, package_from_files, unbundle
from compiler_optimizer.workers.targets import DEFAULT_IMAGES


def worker_identity(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "worker-id"
    if path.is_symlink():
        raise ValueError("worker identity cannot be a symlink")
    try:
        with path.open("x") as output:
            output.write(str(uuid4()))
    except FileExistsError:
        pass
    return pb.FleetIdentity(
        worker_id=path.read_text().strip(),
        incarnation_id=str(uuid4()),
        secret=secrets.token_hex(32),
    )


class WorkerExecutor:
    def __init__(self, settings, runner, root):
        self.settings, self.runner, self.root = settings, runner, root
        self.identity = worker_identity(root)
        self.channel, self.fleet, self.client = fleet_connection(settings)
        self.active = None
        self.sequence = 0
        self.cancelled = threading.Event()
        self.lock = threading.Lock()

    def check(self, lease):
        self.fleet.CheckLease(
            pb.FleetAuthority(identity=self.identity, lease=lease),
            timeout=3,
            metadata=self.client.metadata,
        )

    def heartbeat(self, stop):
        while not stop.is_set():
            with self.lock:
                active = self.active
            self.sequence += 1
            request = pb.FleetHeartbeat(identity=self.identity, sequence=self.sequence)
            if active is not None:
                request.active.CopyFrom(active)
            try:
                pulse = self.fleet.Heartbeat(request, timeout=3, metadata=self.client.metadata)
                if active is not None and not pulse.lease_valid:
                    self.cancelled.set()
                    self.runner.cancel_recovery_attempt(active.token)
                for lease in pulse.cleanup:
                    if self.runner.cancel_recovery_attempt(lease.token):
                        self.fleet.ConfirmCleanup(
                            pb.FleetAuthority(identity=self.identity, lease=lease),
                            timeout=5,
                            metadata=self.client.metadata,
                        )
            except Exception as error:
                logging.warning("worker heartbeat interrupted: %s", type(error).__name__)
                if active is not None:
                    self.cancelled.set()
                    try:
                        self.runner.cancel_recovery_attempt(active.token)
                    except InfrastructureError:
                        pass
            stop.wait(1)

    def execute(self, assignment):
        lease = assignment.lease
        authority = EXECUTION_LEASE.set(
            {
                "lease_token": lease.token,
                "worker_id": self.identity.worker_id,
                "assert_live": lambda: self.check(lease),
            }
        )
        try:
            self.check(lease)
            with tempfile.TemporaryDirectory(prefix="job-", dir=self.root) as temporary:
                root = Path(temporary)
                self.client.fetch(assignment.task_id, assignment.input, root / "input.json")
                value = json.loads((root / "input.json").read_text())
                require(
                    ParseDict(value["compiler"], pb.CompilerSettings())
                    == self.runner.compiler(value["compiler"]["language"]),
                    "worker compiler does not match assignment",
                )

                def fetch(name):
                    ref = ParseDict(value[name], pb.ArtifactRef())
                    self.client.fetch(assignment.task_id, ref, root / name)
                    return (root / name).read_bytes()

                if value["operation"] == "compile":
                    package = package_from_files(unbundle(fetch("package")))
                    require(
                        value.get("reservation_mib", 0)
                        >= package.manifest.limits.compile_memory_mib,
                        "compile exceeds assigned memory reservation",
                    )
                    invocation = self.runner.compile(package, build_profile=value["build_profile"])
                    result = encode_invocation(invocation)
                elif value["operation"] == "execute":
                    manifest = parse_manifest(value["manifest"])
                    require(
                        value.get("reservation_mib", 0) >= manifest.limits.run_memory_mib,
                        "execution exceeds assigned memory reservation",
                    )
                    case = ParseDict(value["case"], pb.TestCase())
                    validate_path(case.stdin)
                    require(
                        len(case.args) <= 32
                        and all(len(a.encode()) <= 1024 and "\x00" not in a for a in case.args),
                        "invalid program arguments",
                    )
                    package = SubmissionPackage(
                        manifest, {case.stdin: fetch("stdin")}, {}, b"", b""
                    )
                    cpu = value["cpu_id"]
                    require(cpu is None or (type(cpu) is int and 0 <= cpu <= 8191), "invalid CPU")
                    result = encode_invocation(
                        self.runner.execute(
                            fetch("binary"),
                            case,
                            package,
                            build_profile=value["build_profile"],
                            cpu_id=cpu,
                        )
                    )
                elif value["operation"] == "environment":
                    result = {"environment": self.runner.benchmark_environment(value["cpu_id"])}
                else:
                    raise ValueError("unsupported work operation")
                self.check(lease)
                return canonical(result)
        except Exception as error:
            trace = traceback.extract_tb(error.__traceback__)[-1]
            logging.warning(
                "sandbox operation failed: %s at %s:%s",
                type(error).__name__,
                trace.name,
                trace.lineno,
            )
            return canonical({"error": "InfrastructureError"})
        finally:
            EXECUTION_LEASE.reset(authority)

    def run(self, stop, ready=None):
        self.runner.check_health()
        profile = canonical(describe_runner(self.runner)).decode()
        self.fleet.Register(
            pb.FleetRegistration(identity=self.identity, profile_json=profile),
            timeout=10,
            metadata=self.client.metadata,
        )
        if ready is not None:
            ready.set()
        heartbeat = threading.Thread(target=self.heartbeat, args=(stop,), daemon=True)
        heartbeat.start()
        connection = None
        try:
            while not stop.is_set():
                try:
                    if connection is None or not connection.is_open:
                        connection = RabbitProposalQueue(self.settings)._connect()
                        channel = connection.channel()
                        declare_topology(channel)
                        queue = worker_queue(channel, self.identity.worker_id)
                        channel.basic_qos(prefetch_count=1)
                    method, properties, body = channel.basic_get(queue=queue, auto_ack=False)
                    if method is None:
                        connection.process_data_events(time_limit=0.1)
                        continue
                    try:
                        require(len(body) <= 1024, "oversized worker delivery")
                        dispatch = pb.FleetDispatch.FromString(body)
                        require(
                            properties.content_type == "application/x-protobuf"
                            and properties.delivery_mode == 2
                            and properties.message_id == dispatch.job_id
                            and properties.type == dispatch.DESCRIPTOR.full_name,
                            "invalid worker delivery",
                        )
                    except Exception:
                        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                        continue
                    assignment = self.fleet.Claim(
                        pb.FleetClaim(identity=self.identity, dispatch=dispatch),
                        timeout=5,
                        metadata=self.client.metadata,
                    )
                    if not assignment.accepted:
                        channel.basic_ack(delivery_tag=method.delivery_tag)
                        continue
                    with self.lock:
                        self.active = assignment.lease
                        self.cancelled.clear()
                    result = []

                    def execute(assignment=assignment, result=result):
                        result.append(self.execute(assignment))

                    thread = threading.Thread(target=execute, daemon=True)
                    thread.start()
                    while thread.is_alive() and not stop.is_set():
                        connection.process_data_events(time_limit=0.1)
                    if stop.is_set():
                        self.runner.cancel_recovery_attempt(assignment.lease.token)
                        break
                    try:
                        if not self.cancelled.is_set():
                            self.fleet.UploadResult(
                                result_frames(self.identity, assignment.lease, result[0]),
                                timeout=60,
                                metadata=self.client.metadata,
                            )
                    except grpc.RpcError as error:
                        if error.code() not in (
                            grpc.StatusCode.FAILED_PRECONDITION,
                            grpc.StatusCode.PERMISSION_DENIED,
                        ):
                            raise
                    finally:
                        with self.lock:
                            self.active = None
                    # Ack committed or fenced work; duplicate deliveries cannot reclaim it.
                    channel.basic_ack(delivery_tag=method.delivery_tag)
                except Exception as error:
                    logging.warning("worker delivery interrupted: %s", type(error).__name__)
                    if connection is not None:
                        try:
                            connection.close()
                        except pika.exceptions.AMQPError:
                            pass
                    connection = None
                    stop.wait(1)
        finally:
            if ready is not None:
                ready.clear()
            stop.set()
            heartbeat.join(timeout=5)
            if connection is not None and connection.is_open:
                connection.close()
            self.channel.close()


def run_worker(settings, stop, ready=None):
    root = settings.worker_directory.absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if settings.worker_target == "auto":
            from compiler_optimizer.evaluation.__main__ import native_target

            target = native_target()
        else:
            target = settings.worker_target
        runner = DockerRunner(
            DEFAULT_IMAGES[target], target=target, watchdog_directory=root / "watchdogs"
        )
        executor = WorkerExecutor(settings, runner, root)
        try:
            executor.run(stop, ready)
        finally:
            executor.channel.close()
    finally:
        os.close(fd)
