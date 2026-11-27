"""Actual RPC intake and coordinator-process restart against Docker/RabbitMQ/Redis."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import grpc
import pytest

from compiler_optimizer.evaluation.__main__ import native_target
from compiler_optimizer.monitoring.watchdog import read_json, remove_owned
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("PIPELINE_INTEGRATION_TESTS") != "1",
        reason="set PIPELINE_INTEGRATION_TESTS=1 with native toolchain and services ready",
    ),
]
ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(action, process, log, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, log.read_text()[-8000:]
        value = action()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("coordinator did not reach checkpoint: " + log.read_text()[-8000:])


def test_submission_resumes_after_actual_coordinator_process_loss(tmp_path, service_database_env):
    repo = Repository(tmp_path / "state")
    package = read_package(ROOT / "examples/sum-array")
    target = native_target()
    compiler = DockerRunner(DEFAULT_IMAGES[target], target=target).compiler(
        package.manifest.language
    )
    request = pb.Submission(
        meta=metadata(str(uuid4())),
        manifest=package.manifest,
        package=repo.artifacts.put(package.package_bundle),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(package.source_bundle)),
        compiler=compiler,
    )
    port, health = free_port(), free_port()
    env = dict(
        os.environ,
        STATE_DIRECTORY=str(repo.root),
        GRPC_PORT=str(port),
        HEALTH_PORT=str(health),
        HEALTH_HOST="127.0.0.1",
        GRPC_HOST="127.0.0.1",
        EXECUTION_ENABLED="true",
        DISPATCH_LEASE_SECONDS="5",
        **service_database_env(repo.root),
    )
    log = tmp_path / "coordinator.log"
    process = None

    def start(output):
        return subprocess.Popen(
            [sys.executable, str(ROOT / "tools/project.py"), "serve", "--no-start"],
            cwd=ROOT,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )

    try:
        with log.open("w") as output:
            process = start(output)
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=15)
                client = rpc.CoordinatorServiceStub(channel)
                client.SubmitSubmission(request, timeout=10)
                task = request.meta.task_id
                verified = wait_for(
                    lambda: next(
                        (
                            row
                            for row in repo.list_results(task)
                            if row.WhichOneof("subject") == "proposal_id"
                            and row.WhichOneof("outcome") == "success"
                        ),
                        None,
                    ),
                    process,
                    log,
                )
                # Interrupt measurement, after the one-shot planning iteration commits.
                # Killing inside that bounded iteration can legitimately exclude the candidate.
                wait_for(
                    lambda: any(
                        row["role"] == pb.BENCHMARK and row["status"] == "completed"
                        for row in repo.list_agent_iterations(task)
                    ),
                    process,
                    log,
                )
                assert (
                    len(
                        [
                            r
                            for r in repo.list_results(task)
                            if r.WhichOneof("subject") == "baseline"
                        ]
                    )
                    == 1
                )
                process.kill()
                process.wait(timeout=10)
            process = start(output)
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=15)
                client = rpc.CoordinatorServiceStub(channel)
                # Repeated intake after restart must not reset policies or attempts.
                client.SubmitSubmission(request, timeout=10)
                wait_for(lambda: repo.get_execution(task)["finished"], process, log)
                result = client.GetOptimizationResult(pb.OptimizationQuery(task_id=task), timeout=5)
                assert result.status == "completed", result.report_json
                assert result.stage == "completed"
                # The killed coordinator could not release its claim: the restart took it over.
                assert '"event": "dispatch_takeover"' in log.read_text()
                assert result.selected_source.bundle.sha256
                assert repo.get_execution(task)["attempts"] == 2
                assert len(repo.list_tasks()) == 1
                assert (
                    len(
                        [
                            r
                            for r in repo.list_results(task)
                            if r.WhichOneof("subject") == "baseline"
                        ]
                    )
                    == 1
                )
                counts = len(repo.list_jobs(task)), len(repo.list_benchmarks(task))
                assert counts[1] >= 1
                assert [
                    row for row in repo.list_results(task) if row.job_id == verified.job_id
                ] == [verified]
            process.terminate()
            process.wait(timeout=15)
            # A further process restart must reuse the final decision unchanged.
            process = start(output)
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=15)
                final = rpc.CoordinatorServiceStub(channel).GetOptimizationResult(
                    pb.OptimizationQuery(task_id=task), timeout=5
                )
                assert final == result
                assert counts == (len(repo.list_jobs(task)), len(repo.list_benchmarks(task)))
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for path in (repo.root / "submission-watchdogs").glob("*/*/lease.json"):
            remove_owned(read_json(path))


def test_standby_replica_takes_over_when_the_owning_coordinator_is_killed(
    tmp_path, service_database_env
):
    """Two live coordinators compete for one task; killing its owner hands it over."""
    repo = Repository(tmp_path / "state")
    package = read_package(ROOT / "examples/sum-array")
    target = native_target()
    compiler = DockerRunner(DEFAULT_IMAGES[target], target=target).compiler(
        package.manifest.language
    )
    request = pb.Submission(
        meta=metadata(str(uuid4())),
        manifest=package.manifest,
        package=repo.artifacts.put(package.package_bundle),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(package.source_bundle)),
        compiler=compiler,
    )
    task = request.meta.task_id
    replicas = {}
    try:
        for name in ("a", "b"):
            port = free_port()
            log = tmp_path / f"coordinator-{name}.log"
            env = dict(
                os.environ,
                STATE_DIRECTORY=str(repo.root),
                GRPC_PORT=str(port),
                HEALTH_PORT=str(free_port()),
                HEALTH_HOST="127.0.0.1",
                GRPC_HOST="127.0.0.1",
                EXECUTION_ENABLED="true",
                DISPATCH_LEASE_SECONDS="5",
                **service_database_env(repo.root),
            )
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "tools/project.py"), "serve", "--no-start"],
                cwd=ROOT,
                env=env,
                stdout=log.open("w"),
                stderr=subprocess.STDOUT,
            )
            replicas[name] = (process, log, port)
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=15)
        with grpc.insecure_channel(f"127.0.0.1:{replicas['a'][2]}") as channel:
            rpc.CoordinatorServiceStub(channel).SubmitSubmission(request, timeout=10)
        process_a, log_a, _ = replicas["a"]
        # Wait until one replica owns the task and has committed real work under its claim.
        wait_for(
            lambda: (
                repo.dispatch_claims()
                and any(row["status"] == "completed" for row in repo.list_agent_iterations(task))
            ),
            process_a,
            log_a,
        )
        owner_pid = int(repo.dispatch_claims()[0]["owner_id"].split(":")[1])
        owner = next(name for name, (p, _, _) in replicas.items() if p.pid == owner_pid)
        standby = "b" if owner == "a" else "a"
        replicas[owner][0].kill()
        replicas[owner][0].wait(timeout=10)
        process, log, _ = replicas[standby]
        wait_for(lambda: repo.get_execution(task)["finished"], process, log)
        assert repo.get_task(task)["status"] == "completed"
        assert '"event": "dispatch_takeover"' in log.read_text()
        claims = repo.dispatch_claims()
        assert len(claims) == 1 and claims[0]["fence"] == 2
        assert claims[0]["owner_id"].split(":")[1] == str(process.pid)
        assert repo.get_decision(task) is not None
    finally:
        for process, _, _ in replicas.values():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        for path in (repo.root / "submission-watchdogs").glob("*/*/lease.json"):
            remove_owned(read_json(path))
