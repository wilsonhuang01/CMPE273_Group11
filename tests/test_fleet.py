"""Durable worker scheduling, RPC authority, cancellation and late-result fencing."""

import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
from google.protobuf.json_format import MessageToDict
from test_baseline import FakeRunner

from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.dispatch import SubmissionDispatcher
from compiler_optimizer.runtime.fleet import FleetService
from compiler_optimizer.runtime.fleet_client import result_frames
from compiler_optimizer.shared.fleet import canonical, encode_invocation, lease_from_row
from compiler_optimizer.shared.manifest import parse_manifest
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.shared.workflow import default_workflow
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import ConflictError, StateError
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import Invocation
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.policy import POLICY_DIGEST
from compiler_optimizer.workers.remote import RemoteRunner
from compiler_optimizer.workers.targets import compiler_flags


def profile(engine="unit-engine"):
    compilers = {}
    for language in ("c17", "cpp17"):
        compiler = FakeRunner().compiler(language)
        compiler.executable = "clang" if language == "c17" else "clang++"
        compiler.flags[:] = compiler_flags(compiler.target, language)
        compilers[language] = MessageToDict(compiler, preserving_proto_field_name=True)
    return {
        "schema_version": 1,
        "target": "linux-x86_64",
        "image": compilers["c17"]["image_digest"],
        "hardware": "unit worker",
        "platform": "linux/amd64",
        "native_execution": True,
        "native_amd64": True,
        "supports_sanitizers": True,
        "measurement_version": 2,
        "compilers": compilers,
        "context": {
            "policy_digest": POLICY_DIGEST,
            "target": "linux-x86_64",
            "platform": "linux/amd64",
            "hardware": {"engine_id": engine},
        },
    }


@pytest.fixture
def fleet(tmp_path):
    repo = Repository(tmp_path / "server")
    clock = [100000]
    repo.fleet_clock_ms = lambda: clock[0]
    package = read_package(Path(__file__).resolve().parents[1] / "examples/sum-array")
    task = run_baseline(package, repo, FakeRunner()).meta.task_id
    identities = [
        pb.FleetIdentity(
            worker_id=str(uuid4()), incarnation_id=str(uuid4()), secret=secrets.token_hex(32)
        )
        for _ in range(2)
    ]
    for identity in identities:
        repo.register_fleet_worker(identity, canonical(profile()).decode())
    return repo, task, identities, clock


def enqueue(repo, task, identity, parent=None):
    worker = next(w for w in repo.fleet_profiles() if w["worker_id"] == identity.worker_id)
    return repo.enqueue_fleet_job(
        task,
        worker,
        {
            "operation": "compile",
            "timeout_seconds": 60,
            "build_profile": "normal",
            "output_bytes": 65536,
        },
        [],
        parent_token=parent,
    )


def claim(repo, job, identity):
    jobs = repo.schedule_fleet()
    row = next(row for row in jobs if row["job_id"] == job)
    dispatch = pb.FleetDispatch(job_id=job, worker_id=identity.worker_id, fence=row["fence"])
    return repo.claim_fleet(pb.FleetClaim(identity=identity, dispatch=dispatch))


def result():
    return canonical(
        encode_invocation(
            Invocation(
                b"",
                b"",
                0.1,
                1024,
                0,
                binary=b"binary",
                sandbox={
                    "policy_digest": POLICY_DIGEST,
                    "watchdog": {"state": "removed"},
                    "cpu_id": None,
                },
            )
        )
    )


def test_confirmed_worker_result_is_task_scoped_and_immutable(fleet):
    repo, task, (first, _), _ = fleet
    job = enqueue(repo, task, first)
    assignment = claim(repo, job, first)
    assert assignment.accepted
    assert repo.artifacts.get(assignment.input)
    repo.commit_fleet_result(first, assignment.lease, result())
    assert repo.fleet_job(job)["state"] == "completed"
    assert repo.fleet_profiles()[0]["active_job"] is None
    with pytest.raises(StateError):
        repo.commit_fleet_result(first, assignment.lease, result())


def test_worker_loss_requires_same_engine_cleanup_before_reassignment(fleet):
    repo, task, (first, second), clock = fleet
    job = enqueue(repo, task, first)
    assignment = claim(repo, job, first)
    clock[0] += 7000
    pulse = repo.fleet_heartbeat(pb.FleetHeartbeat(identity=second, sequence=1))
    assert not pulse.cleanup
    repo.schedule_fleet()
    assert repo.fleet_job(job)["state"] == "cleanup"
    pulse = repo.fleet_heartbeat(pb.FleetHeartbeat(identity=second, sequence=2))
    assert pulse.cleanup[0].job_id == job
    with pytest.raises(StateError):
        repo.commit_fleet_result(first, assignment.lease, result())
    repo.confirm_fleet_cleanup(second, pulse.cleanup[0])
    assert repo.fleet_job(job)["state"] == "failed"
    next_job = enqueue(repo, task, second)
    assert claim(repo, next_job, second).accepted
    with pytest.raises(StateError):
        repo.commit_fleet_result(first, assignment.lease, result())


def test_old_incarnation_and_stolen_worker_identity_are_rejected(fleet):
    repo, task, (first, _), _ = fleet
    assignment = claim(repo, enqueue(repo, task, first), first)
    stolen = pb.FleetIdentity()
    stolen.CopyFrom(first)
    stolen.secret = "f" * 64
    with pytest.raises(PermissionError):
        repo.check_fleet_authority(stolen, assignment.lease)
    replacement = pb.FleetIdentity(
        worker_id=first.worker_id, incarnation_id=str(uuid4()), secret=secrets.token_hex(32)
    )
    repo.register_fleet_worker(replacement, canonical(profile()).decode())
    with pytest.raises(PermissionError):
        repo.commit_fleet_result(first, assignment.lease, result())


def test_cancellation_and_wrong_engine_cleanup_fail_closed(fleet):
    repo, task, (first, second), _ = fleet
    assignment = claim(repo, enqueue(repo, task, first), first)
    repo.cancel_fleet_job(assignment.lease.job_id)
    changed = pb.FleetIdentity(
        worker_id=second.worker_id, incarnation_id=str(uuid4()), secret=secrets.token_hex(32)
    )
    repo.register_fleet_worker(changed, canonical(profile("different-engine")).decode())
    with pytest.raises(StateError):
        repo.confirm_fleet_cleanup(changed, assignment.lease)
    with pytest.raises(StateError):
        repo.commit_fleet_result(first, assignment.lease, result())


def test_heartbeats_cannot_resurrect_expired_authority(fleet):
    repo, task, (first, _), clock = fleet
    assignment = claim(repo, enqueue(repo, task, first), first)
    clock[0] += 10001
    pulse = repo.fleet_heartbeat(
        pb.FleetHeartbeat(identity=first, sequence=1, active=assignment.lease)
    )
    assert not pulse.lease_valid
    with pytest.raises(StateError):
        repo.commit_fleet_result(first, assignment.lease, result())


def test_parent_recovery_fence_is_checked_on_worker_commit(fleet):
    repo, task, (first, _), _ = fleet
    assignment = claim(repo, enqueue(repo, task, first, parent="unassigned-parent-token"), first)
    with pytest.raises(StateError, match="parent"):
        repo.commit_fleet_result(first, assignment.lease, result())


def test_duplicate_broker_delivery_cannot_claim_running_work(fleet):
    repo, task, (first, _), _ = fleet
    job = enqueue(repo, task, first)
    assignment = claim(repo, job, first)
    dispatch = pb.FleetDispatch(job_id=job, worker_id=first.worker_id, fence=assignment.lease.fence)
    assert not repo.claim_fleet(pb.FleetClaim(identity=first, dispatch=dispatch)).accepted


def test_duplicate_registration_and_heartbeat_do_not_reset_authority(fleet):
    repo, task, (first, _), clock = fleet
    assignment = claim(repo, enqueue(repo, task, first), first)
    heartbeat = pb.FleetHeartbeat(identity=first, sequence=1, active=assignment.lease)
    repo.fleet_heartbeat(heartbeat)
    before = repo.fleet_job(assignment.lease.job_id)["expires_ms"]
    clock[0] += 1000
    repo.register_fleet_worker(first, canonical(profile()).decode())
    repo.fleet_heartbeat(heartbeat)
    assert repo.fleet_job(assignment.lease.job_id)["expires_ms"] == before
    with pytest.raises(ConflictError):
        repo.fleet_heartbeat(pb.FleetHeartbeat(identity=first, sequence=0))


def test_rpc_rejects_late_streamed_result_after_fencing(fleet):
    repo, task, (first, second), clock = fleet
    assignment = claim(repo, enqueue(repo, task, first), first)
    clock[0] += 7000
    repo.fleet_heartbeat(pb.FleetHeartbeat(identity=second, sequence=1))
    repo.schedule_fleet()
    repo.confirm_fleet_cleanup(second, lease_from_row(repo.fleet_job(assignment.lease.job_id)))
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        rpc.add_FleetServiceServicer_to_server(FleetService(repo), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                with pytest.raises(grpc.RpcError) as error:
                    rpc.FleetServiceStub(channel).UploadResult(
                        result_frames(first, assignment.lease, result()), timeout=5
                    )
                assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        finally:
            server.stop(0).wait(timeout=3)


def test_execution_manifest_round_trip_preserves_empty_required_arrays(fleet, monkeypatch):
    repo, task, (first, _), _ = fleet
    worker = next(w for w in repo.fleet_profiles() if w["worker_id"] == first.worker_id)
    runner = RemoteRunner(repo, task, worker)
    captured = []
    monkeypatch.setattr(runner, "_call", lambda value, refs: captured.append(value))
    package = read_package(Path(__file__).resolve().parents[1] / "examples/sum-array")
    runner.execute(b"binary", package.manifest.tests[0], package)
    assert parse_manifest(captured[0]["manifest"]) == package.manifest


def test_coordinator_mode_change_cannot_consume_an_accepted_local_task(fleet):
    repo, task, _, _ = fleet
    config = default_workflow()
    with repo._transaction() as db:
        db.execute(
            "INSERT INTO submission_executions(task_id,configuration_json) VALUES(?,?)",
            (task, json.dumps(config)),
        )
    dispatcher = SubmissionDispatcher(
        repo, Settings(role="coordinator", execution_backend="remote")
    )
    assert not dispatcher.run_once()
    assert repo.get_execution(task)["attempts"] == 0


def test_terminal_task_rejects_inflight_worker_evidence(fleet):
    repo, task, (first, _), _ = fleet
    assignment = claim(repo, enqueue(repo, task, first), first)
    with repo._transaction() as db:
        db.execute("UPDATE tasks SET status='failed' WHERE task_id=?", (task,))
    with pytest.raises(StateError):
        repo.commit_fleet_result(first, assignment.lease, result())
