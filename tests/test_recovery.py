"""Healthy-worker retries, fencing, leases, idempotent results and context-scoped quarantine."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
from test_selection import MeasuredRunner

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.runtime.worker_health import WorkerHealthService
from compiler_optimizer.scheduler.blacklist import RedisBlacklist
from compiler_optimizer.scheduler.recovery import RecoveryController, Worker
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.recovery import (
    DeadLetterResult,
    RecoveryPolicy,
    RetryDelivery,
    quarantine_key,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import ConflictError, StateError
from compiler_optimizer.storage.recovery import LeaseLost
from compiler_optimizer.workers.baseline import metadata, run_baseline
from compiler_optimizer.workers.docker_runner import InfrastructureError
from compiler_optimizer.workers.package import read_package

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.writes = 0

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, *, nx, pxat):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.writes += 1
        return True


class RecoveryBroker:
    def __init__(self, repository):
        self.repository = repository
        self.retries, self.deadletters, self.acks = [], [], []
        self.fail_ack = False

    def exchange(self, proposal, process, published):
        published()
        for _ in range(50):
            try:
                result = process(proposal)
            except RetryDelivery as retry:
                self.retries.append(retry.event_id)
                retry.confirmed()
                time.sleep(retry.delay_ms / 1000 + 0.001)
                continue
            if isinstance(result, DeadLetterResult):
                self.deadletters.append((result.event_id, result.reason))
                result.confirmed()
                result = result.result
            self.acks.append(proposal.proposal_id)
            if self.fail_ack:
                raise OSError("ack lost")
            return result
        raise AssertionError("unbounded retry loop")

    def publish_recovery_event(self, proposal, kind, event_id, **kwargs):
        if kind == "deadletter":
            self.deadletters.append((event_id, kwargs["reason"]))
        else:
            self.retries.append(event_id)


def policy(**changes):
    values = dict(
        lease_seconds=0.6,
        heartbeat_seconds=0.05,
        worker_health_seconds=0.6,
        attempt_timeout_seconds=5.0,
        backoff_seconds=0.01,
        maximum_backoff_seconds=0.02,
        worker_cooldown_seconds=0.02,
        cleanup_grace_seconds=0.1,
        blacklist_ttl_seconds=60,
    )
    values.update(changes)
    return replace(RecoveryPolicy(), **values)


def setup(tmp_path, *, cache=None, crash=False, infra=0):
    package = read_package(EXAMPLES / "sum-array")
    repo = Repository(tmp_path / "state")
    first, second = MeasuredRunner(package), MeasuredRunner(package)
    # Fake binaries are portable between workers just like the pinned real toolchain output.
    second.binaries, second.kinds = first.binaries, first.kinds
    baseline = run_baseline(package, repo, first)
    for runner in (first, second):
        runner.check_health = lambda: None
        runner.recovery_context = lambda: {"engine": "unit", "profile": "same"}
        runner.cancel_recovery_attempt = lambda token: True
        if crash:
            runner.mode = "candidate-ub"
    compile_first = first.compile
    calls = [infra]

    def compile_once(*args, **kwargs):
        if calls[0]:
            calls[0] -= 1
            raise InfrastructureError("compiler service lost")
        return compile_first(*args, **kwargs)

    first.compile = compile_once
    broker = RecoveryBroker(repo)
    cache = cache or MemoryRedis()
    recovery = RecoveryController(
        repo,
        [Worker(runner=first), Worker(runner=second)],
        RedisBlacklist(cache),
        # Real SQLite/fsync and thread scheduling vary on shared CI hosts.
        # Failure injection below controls expiry; healthy work needs headroom.
        policy=policy(lease_seconds=5, worker_health_seconds=5, attempt_timeout_seconds=30),
    )
    coordinator = CohortCoordinator(repo, first, broker, recovery=recovery)
    return repo, first, second, baseline.meta.task_id, broker, recovery, coordinator, cache


def test_infrastructure_retry_uses_a_different_healthy_worker_and_keeps_plans(tmp_path):
    repo, _, _, task, broker, recovery, coordinator, cache = setup(tmp_path, infra=1)
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "selected", audit
    proposal = repo.list_proposals(task)[0]
    attempts = repo.recovery_attempts(task, proposal.proposal_id)
    assert [a["status"] for a in attempts] == ["infrastructure_failure", "completed"]
    assert attempts[0]["worker_id"] != attempts[1]["worker_id"]
    jobs = [j for j in repo.list_jobs(task) if j.WhichOneof("subject") == "proposal_id"]
    assert [j.attempt for j in jobs] == [1, 2]
    assert (
        jobs[0].job_id == jobs[1].job_id and jobs[0].verification_plan == jobs[1].verification_plan
    )
    assert len(broker.retries) == 1 and not broker.deadletters
    assert not cache.values
    before = len(repo.recovery_attempts(task, proposal.proposal_id))
    assert coordinator.run(task) == audit
    assert len(repo.recovery_attempts(task, proposal.proposal_id)) == before


def test_two_matching_crashes_quarantine_only_exact_execution_context(tmp_path):
    repo, _, _, task, broker, recovery, coordinator, cache = setup(tmp_path, crash=True)
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "retained_baseline"
    proposal = repo.list_proposals(task)[0]
    job = repo.get_recovery_job(task, proposal.proposal_id)
    assert job["state"] == "quarantined"
    assert len(broker.retries) == 1 and broker.deadletters[0][1] == "confirmed_program_crash"
    assert len(cache.values) == 1
    key, value = next(iter(cache.values.items()))
    record = json.loads(value)
    assert len({r["worker_id"] for r in record["confirmations"]}) == 2
    assert record["source_sha256"] == proposal.source.bundle.sha256
    assert key == quarantine_key(
        proposal.transformation, proposal.source.bundle.sha256, record["context"]
    )
    assert quarantine_key("other", proposal.source.bundle.sha256, record["context"]) != key
    assert quarantine_key(proposal.transformation, "a" * 64, record["context"]) != key
    other = {**record["context"], "package_sha256": "b" * 64}
    assert quarantine_key(proposal.transformation, proposal.source.bundle.sha256, other) != key


def test_shared_blacklist_skips_execution_in_a_fresh_coordinator_state(tmp_path):
    values = setup(tmp_path / "first", crash=True)
    values[6].run(values[3], agents=["performance"])
    cache = values[-1]
    repo, first, second, task, broker, _, coordinator, _ = setup(tmp_path / "second", cache=cache)
    before = list(first.events)
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "retained_baseline"
    assert first.events == before and not second.events
    assert broker.deadletters[0][1] == "blacklisted_execution_context"
    assert (
        repo.recovery_outcome(task, repo.list_proposals(task)[0].proposal_id)["reason"]
        == "blacklisted_execution_context"
    )


def test_unknown_infrastructure_failure_is_never_blacklisted(tmp_path):
    repo, first, second, task, broker, _, coordinator, cache = setup(tmp_path)
    first.compile = second.compile = lambda *a, **kw: (_ for _ in ()).throw(
        InfrastructureError("daemon down")
    )
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "retained_baseline"
    proposal = repo.list_proposals(task)[0]
    assert len(repo.recovery_attempts(task, proposal.proposal_id)) == 3
    assert broker.deadletters[0][1] == "infrastructure_attempts_exhausted"
    assert not cache.values


def test_worker_heartbeats_are_monotonic_and_idempotent(tmp_path):
    repo, _, _, task, _, _, _, _ = setup(tmp_path)
    worker, incarnation = str(uuid4()), str(uuid4())
    repo.register_recovery_worker(task, worker, incarnation)
    heartbeat = pb.WorkerHeartbeat(
        meta=metadata(""),
        worker_id=worker,
        incarnation_id=incarnation,
        sequence=1,
        state=pb.IDLE,
        targets=["linux-x86_64"],
    )
    assert repo.report_worker_heartbeat(heartbeat)
    assert repo.report_worker_heartbeat(heartbeat) is False
    other = pb.WorkerHeartbeat.FromString(heartbeat.SerializeToString())
    other.meta.message_id = str(uuid4())
    with pytest.raises(ConflictError):
        repo.report_worker_heartbeat(other)
    other.sequence = 2
    other.incarnation_id = str(uuid4())
    with pytest.raises(ContractError, match="incarnation"):
        repo.report_worker_heartbeat(other)
    repo.quarantine_worker(worker, 0.1)
    with pytest.raises(StateError, match="quarantined"):
        repo.report_worker_heartbeat(
            pb.WorkerHeartbeat(
                meta=metadata(""),
                worker_id=worker,
                incarnation_id=incarnation,
                sequence=2,
                state=pb.IDLE,
                targets=["linux-x86_64"],
            )
        )


def test_expired_lease_is_fenced_before_and_after_takeover(tmp_path):
    repo, _, _, task, broker, recovery, coordinator, _ = setup(tmp_path)
    coordinator._prepare(task, ["performance"], None, None, None)
    member = repo.get_selection_session(task)["members"][0]
    from compiler_optimizer.agents.performance import ExamplePerformanceBackend, PerformanceAgent

    request = repo.list_requests(task)[-1]
    proposal, _ = PerformanceAgent(repo.artifacts, ExamplePerformanceBackend()).generate(request)
    repo.save_proposal(proposal)
    clock = [100000]
    repo.recovery_clock_ms = lambda: clock[0]
    repo.ensure_recovery_job(proposal, {"base": 1}, repo.artifacts.put(policy().encode()))
    workers = []
    for _ in range(2):
        wid, incarnation = str(uuid4()), str(uuid4())
        repo.register_recovery_worker(task, wid, incarnation)
        repo.report_worker_heartbeat(
            pb.WorkerHeartbeat(
                meta=metadata(""),
                worker_id=wid,
                incarnation_id=incarnation,
                sequence=1,
                state=pb.IDLE,
                targets=["linux-x86_64"],
            )
        )
        workers.append((wid, incarnation))
    lease = repo.claim_recovery_job(task, member["proposal_id"], *workers[0], {"context": 1})
    with repo.execution_lease(lease):
        repo.begin_agent_iteration(
            task, pb.CORRECTNESS, proposal.proposal_id, repo.artifacts.put(b"{}")
        )
    clock[0] += 1000
    with pytest.raises(LeaseLost), repo.execution_lease(lease):
        pass
    assert repo.expire_recovery_lease(task, proposal.proposal_id)
    repo.confirm_recovery_cleanup(task, proposal.proposal_id, lease["lease_token"])
    clock[0] += 1000
    # Refresh the healthy replacement after advancing server time.
    repo.report_worker_heartbeat(
        pb.WorkerHeartbeat(
            meta=metadata(""),
            worker_id=workers[1][0],
            incarnation_id=workers[1][1],
            sequence=2,
            state=pb.IDLE,
            targets=["linux-x86_64"],
        )
    )
    newer = repo.claim_recovery_job(task, proposal.proposal_id, *workers[1], {"context": 1})
    assert newer["fence"] > lease["fence"]
    with pytest.raises(LeaseLost), repo.execution_lease(lease):
        pass
    revision = repo.get_task(task)["revision"]
    with pytest.raises(LeaseLost), repo.execution_lease(newer), repo._transaction() as db:
        db.execute("UPDATE tasks SET revision=revision+1 WHERE task_id=?", (task,))
        clock[0] += 1000
    assert repo.get_task(task)["revision"] == revision


@pytest.mark.parametrize(
    "field,value",
    [
        ("maximum_attempts", 0),
        ("lease_seconds", float("nan")),
        ("heartbeat_seconds", 100),
        ("blacklist_ttl_seconds", True),
        ("jitter_fraction", 2),
    ],
)
def test_invalid_recovery_policy_is_rejected(field, value):
    data = asdict(RecoveryPolicy())
    data[field] = value
    with pytest.raises(ContractError):
        RecoveryPolicy.parse(data)


def test_backoff_is_deterministic_and_capped():
    value = RecoveryPolicy()
    assert value.delay_ms("job", 1) == value.delay_ms("job", 1)
    assert 800 <= value.delay_ms("job", 1) <= 1200
    assert value.delay_ms("job", 100) <= value.maximum_backoff_seconds * 1000


def test_stalled_worker_is_retired_and_late_result_cannot_commit(tmp_path):
    repo, first, second, task, _, recovery, coordinator, _ = setup(tmp_path)
    offset_ms = [0]
    repo.recovery_clock_ms = lambda: time.time_ns() // 1_000_000 + offset_ms[0]
    release, finished = threading.Event(), threading.Event()
    compile_first = first.compile

    def stall(*args, **kwargs):
        # Expire only the stalled attempt, without imposing a 250 ms wall-clock
        # deadline on the healthy replacement's verification and measurement.
        offset_ms[0] += int(recovery.policy.attempt_timeout_seconds * 1000) + 1
        try:
            assert release.wait(30)
            return compile_first(*args, **kwargs)
        finally:
            finished.set()

    first.compile = stall
    compile_second = second.compile
    slow_once = [True]

    def healthy_compile(*args, **kwargs):
        if slow_once[0]:
            slow_once[0] = False
            time.sleep(0.35)  # Healthy work may legitimately exceed the old 250 ms budget.
        return compile_second(*args, **kwargs)

    second.compile = healthy_compile
    try:
        audit = coordinator.run(task, agents=["performance"])
        assert audit["status"] == "selected", audit
        proposal = repo.list_proposals(task)[0]
        attempts = repo.recovery_attempts(task, proposal.proposal_id)
        assert [a["status"] for a in attempts] == ["lease_expired", "completed"]
        assert attempts[0]["worker_id"] != attempts[1]["worker_id"]
        assert recovery.workers[0].retired and not recovery.workers[1].retired
        before = repo.list_results(task)
        release.set()
        assert finished.wait(2)
        time.sleep(0.1)
        assert repo.list_results(task) == before
    finally:
        release.set()


def test_failed_measurement_retries_without_repeating_verification(tmp_path):
    repo, first, _, task, _, _, coordinator, _ = setup(tmp_path)

    def broken_environment(cpu):
        raise InfrastructureError("measurement service disconnected")

    first.benchmark_environment = broken_environment
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "selected", audit
    results = [r for r in repo.list_results(task) if r.WhichOneof("subject") == "proposal_id"]
    assert len(results) == 1 and results[0].WhichOneof("outcome") == "success"
    reports = repo.list_benchmarks(task)
    assert len(reports) == 2
    assert reports[0]["job_id"] == reports[1]["job_id"]


def test_non_reproducible_crash_is_excluded_without_shared_blacklist(tmp_path):
    repo, _, second, task, broker, _, coordinator, cache = setup(tmp_path, crash=True)
    second.mode = "valid"
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "retained_baseline", audit
    assert not cache.values
    assert broker.deadletters[0][1] == "unstable_program_failure"
    proposal = repo.list_proposals(task)[0]
    assert len(repo.recovery_attempts(task, proposal.proposal_id)) == 2


def test_blacklist_outage_fails_closed_with_bounded_infrastructure_retries(tmp_path):
    import redis

    repo, first, second, task, broker, _, coordinator, cache = setup(tmp_path)
    before = list(first.events)
    cache.get = lambda key: (_ for _ in ()).throw(redis.ConnectionError("cache down"))
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "retained_baseline"
    assert first.events == before and not second.events
    assert len(repo.recovery_attempts(task, repo.list_proposals(task)[0].proposal_id)) == 3
    assert broker.deadletters[0][1] == "infrastructure_attempts_exhausted"
    assert not cache.values


def test_lost_ack_reuses_committed_execution(tmp_path):
    repo, first, second, task, broker, _, coordinator, _ = setup(tmp_path)
    broker.fail_ack = True
    with pytest.raises(OSError, match="ack lost"):
        coordinator.run(task, agents=["performance"])
    before = list(first.events), list(second.events)
    broker.fail_ack = False
    assert coordinator.run(task)["status"] == "selected"
    assert before == (first.events, second.events)
    assert len(broker.acks) == 2  # The resume settles delivery using its cached result.
    assert len(repo.recovery_attempts(task, repo.list_proposals(task)[0].proposal_id)) == 1


def test_terminal_commit_before_progress_recovers_outbox_without_execution(tmp_path):
    repo, first, second, task, broker, _, coordinator, cache = setup(tmp_path, crash=True)
    record_progress = repo.record_selection_progress
    fail = [True]

    def interrupted(*args, **kwargs):
        if fail[0]:
            fail[0] = False
            raise OSError("owner stopped after terminal commit")
        return record_progress(*args, **kwargs)

    repo.record_selection_progress = interrupted
    with pytest.raises(OSError, match="owner stopped"):
        coordinator.run(task, agents=["performance"])
    assert not cache.values
    before = list(first.events), list(second.events)
    assert coordinator.run(task)["status"] == "retained_baseline"
    assert before == (first.events, second.events)
    assert len(cache.values) == 1 and broker.deadletters
    assert not repo.recovery_outbox(task)


def test_worker_heartbeat_failure_moves_work_to_another_worker(tmp_path):
    repo, first, _, task, _, recovery, coordinator, _ = setup(tmp_path)
    heartbeat = recovery.workers[0].heartbeat
    compile_first = first.compile

    def lost_heartbeat(repository, task_id, job_id=None):
        if job_id is not None:
            raise StateError("worker heartbeat transport lost")
        return heartbeat(repository, task_id, job_id)

    def slow_compile(*args, **kwargs):
        time.sleep(0.15)
        return compile_first(*args, **kwargs)

    recovery.workers[0].heartbeat = lost_heartbeat
    first.compile = slow_compile
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "selected", audit
    assert recovery.workers[0].retired
    attempts = repo.recovery_attempts(task, repo.list_proposals(task)[0].proposal_id)
    assert len(attempts) == 2 and attempts[0]["worker_id"] != attempts[1]["worker_id"]


def test_malformed_blacklist_is_not_a_quarantine_decision():
    from compiler_optimizer.scheduler.blacklist import BlacklistUnavailable

    client = MemoryRedis()
    client.values["key"] = b"[]"
    with pytest.raises(BlacklistUnavailable):
        RedisBlacklist(client).get("key", 0)


def test_heartbeat_rpc_accepts_duplicates_and_rejects_unknown_workers(tmp_path):
    repo, _, _, task, _, _, _, _ = setup(tmp_path)
    worker, incarnation = str(uuid4()), str(uuid4())
    repo.register_recovery_worker(task, worker, incarnation)
    heartbeat = pb.WorkerHeartbeat(
        meta=metadata(""),
        worker_id=worker,
        incarnation_id=incarnation,
        sequence=1,
        state=pb.IDLE,
        targets=["linux-x86_64"],
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        rpc.add_WorkerHealthServiceServicer_to_server(WorkerHealthService(repo), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = rpc.WorkerHealthServiceStub(channel)
                for _ in range(2):
                    receipt = stub.ReportHeartbeat(heartbeat, timeout=5)
                    assert receipt.accepted_message_id == heartbeat.meta.message_id
                heartbeat.worker_id = str(uuid4())
                with pytest.raises(grpc.RpcError) as failure:
                    stub.ReportHeartbeat(heartbeat, timeout=5)
                assert failure.value.code() == grpc.StatusCode.NOT_FOUND
        finally:
            server.stop(0).wait()
