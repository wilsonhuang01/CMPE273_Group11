"""All host execution commands enforce the same scoped quarantine and durable budgets."""

import sqlite3
from dataclasses import replace
from importlib.resources import files

import pytest
from test_recovery import MemoryRedis, RecoveryBroker, setup

from compiler_optimizer.coordinator.pipeline import PipelineCoordinator
from compiler_optimizer.scheduler.blacklist import RedisBlacklist
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.lease_context import RECOVERY_SCOPE
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.recovery import RecoveryTerminal
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import ConflictError
from compiler_optimizer.workers.execution import execute_benchmark, execute_verification
from compiler_optimizer.workers.verification_plan import VerificationPlan
from compiler_optimizer.workers.verify import run_verification


class Pause(Exception):
    pass


class PauseBroker:
    def exchange(self, proposal, consume, published):
        published()
        raise Pause()


def pending(values):
    repo, runner, _, task, _, recovery, _, _ = values
    coordinator = PipelineCoordinator(repo, runner, PauseBroker(), recovery=recovery)
    with pytest.raises(Pause):
        coordinator.run(task)
    return repo.list_proposals(task)[0]


def seed_quarantine(tmp_path):
    values = setup(tmp_path, crash=True)
    values[6].run(values[3], agents=["performance"])
    return values[-1]


@pytest.mark.parametrize("entrypoint", ["pipeline", "verification", "benchmark"])
def test_shared_quarantine_denies_each_host_entrypoint(tmp_path, entrypoint):
    cache = seed_quarantine(tmp_path / "seed")
    values = setup(tmp_path / "fresh", cache=cache)
    repo, runner, second, task, _, recovery, _, _ = values
    proposal = pending(values)
    if entrypoint != "pipeline":
        with repo._transaction() as db:
            db.execute("DELETE FROM pipeline_runs WHERE task_id=?", (task,))
            db.execute("DELETE FROM selection_sessions WHERE task_id=?", (task,))
    if entrypoint == "benchmark":
        # Represents an older checkpoint verified before the quarantine was published.
        verified = run_verification(repo, task, proposal.proposal_id, runner)
    before = list(runner.events)
    if entrypoint == "pipeline":
        broker = RecoveryBroker(repo)
        report = PipelineCoordinator(repo, runner, broker, recovery=recovery).run(task)
        assert not report["candidate_selected"]
        assert report["recovery"]["reason"] == "blacklisted_execution_context"
        assert broker.deadletters
    else:
        with pytest.raises(RecoveryTerminal, match="blacklisted_execution_context"):
            if entrypoint == "verification":
                execute_verification(
                    repo,
                    task,
                    proposal.proposal_id,
                    runner,
                    VerificationPlan(),
                    BenchmarkPlan(),
                    recovery=recovery,
                )
            else:
                execute_benchmark(
                    repo, task, verified.job_id, runner, BenchmarkPlan(), recovery=recovery
                )
    assert runner.events == before
    assert not second.events


def test_verification_then_benchmark_have_separate_frozen_checkpoints(tmp_path):
    values = setup(tmp_path)
    repo, runner, _, task, _, recovery, _, _ = values
    proposal = pending(values)
    with repo._transaction() as db:
        db.execute("DELETE FROM pipeline_runs WHERE task_id=?", (task,))
        db.execute("DELETE FROM selection_sessions WHERE task_id=?", (task,))
    verified = execute_verification(
        repo,
        task,
        proposal.proposal_id,
        runner,
        VerificationPlan(),
        BenchmarkPlan(),
        recovery=recovery,
    )
    report = execute_benchmark(
        repo, task, verified.job_id, runner, BenchmarkPlan(), recovery=recovery
    )
    before = list(runner.events)
    assert (
        execute_benchmark(repo, task, verified.job_id, runner, BenchmarkPlan(), recovery=recovery)
        == report
    )
    assert runner.events == before
    assert report["analysis"]["qualified"]
    with pytest.raises((ConflictError, ContractError), match="frozen"):
        execute_verification(
            repo,
            task,
            proposal.proposal_id,
            runner,
            replace(VerificationPlan(), sanitizer="address-undefined"),
            BenchmarkPlan(),
            recovery=recovery,
        )


def test_cache_outage_is_bounded_before_any_execution(tmp_path):
    values = setup(tmp_path)
    repo, runner, _, task, _, recovery, _, _ = values
    proposal = pending(values)

    class Offline(MemoryRedis):
        def get(self, key):
            import redis

            raise redis.ConnectionError("offline")

    recovery.blacklist = RedisBlacklist(Offline())
    before = list(runner.events)
    with pytest.raises(RecoveryTerminal, match="infrastructure_attempts_exhausted"):
        execute_verification(
            repo,
            task,
            proposal.proposal_id,
            runner,
            VerificationPlan(),
            BenchmarkPlan(),
            recovery=recovery,
        )
    assert runner.events == before
    token = RECOVERY_SCOPE.set("verification")
    try:
        assert len(repo.recovery_attempts(task, proposal.proposal_id)) == 3
    finally:
        RECOVERY_SCOPE.reset(token)


def test_populated_schema_eight_upgrade_preserves_frozen_recovery(tmp_path):
    values = setup(tmp_path)
    repo, runner, _, task, _, recovery, coordinator, _ = values
    coordinator.run(task, agents=["performance"])
    proposal = repo.list_proposals(task)[0]
    before = repo.get_recovery_job(task, proposal.proposal_id)
    attempts = repo.recovery_attempts(task, proposal.proposal_id)
    outbox = repo.recovery_outbox(task, pending=False)
    # Reconstruct the previous schema while keeping real, populated recovery evidence.
    with sqlite3.connect(repo.database) as db:
        for table in (
            "recovery_jobs",
            "recovery_attempts",
            "recovery_outbox",
            "recovery_blacklist",
        ):
            db.execute(f"CREATE TEMP TABLE backup_{table} AS SELECT * FROM {table}")
        for table in (
            "recovery_attempts",
            "recovery_outbox",
            "recovery_jobs",
            "recovery_blacklist",
            "recovery_workers",
        ):
            if table == "recovery_workers":
                db.execute(f"CREATE TEMP TABLE backup_{table} AS SELECT * FROM {table}")
            db.execute(f"DROP TABLE {table}")
        schema = files("compiler_optimizer.storage").joinpath("recovery.sql").read_text()
        for statement in schema.split(";"):
            if statement.strip():
                db.execute(statement)
        for table in (
            "recovery_workers",
            "recovery_jobs",
            "recovery_attempts",
            "recovery_blacklist",
            "recovery_outbox",
        ):
            columns = ",".join(r[1] for r in db.execute(f"PRAGMA table_info({table})"))
            db.execute(f"INSERT INTO {table} SELECT {columns} FROM backup_{table}")
        for table in (
            "agent_rpc_calls",
            "debate_events",
            "debate_slots",
            "debate_runs",
            "cohort_work",
            "cohort_runs",
            "measurement_windows",
            "measurement_claims",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("ALTER TABLE fleet_jobs DROP COLUMN window_token")
        db.execute("ALTER TABLE fleet_jobs DROP COLUMN reservation_mib")
        db.execute("PRAGMA user_version=8")
    reopened = Repository(repo.root)
    assert reopened.get_recovery_job(task, proposal.proposal_id) == before
    assert reopened.recovery_attempts(task, proposal.proposal_id) == attempts
    assert reopened.recovery_outbox(task, pending=False) == outbox
    with reopened._transaction(write=False) as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_changed_context_executes_despite_shared_quarantine(tmp_path):
    cache = seed_quarantine(tmp_path / "seed")
    values = setup(tmp_path / "fresh", cache=cache)
    repo, runner, _, task, _, recovery, _, _ = values
    proposal = pending(values)
    with repo._transaction() as db:
        db.execute("DELETE FROM pipeline_runs WHERE task_id=?", (task,))
        db.execute("DELETE FROM selection_sessions WHERE task_id=?", (task,))
    before = len(runner.events)
    result = execute_verification(
        repo,
        task,
        proposal.proposal_id,
        runner,
        VerificationPlan(),
        replace(BenchmarkPlan(), repetitions=11),
        recovery=recovery,
    )
    assert result.WhichOneof("outcome") == "success"
    assert len(runner.events) > before


def test_legacy_pipeline_retries_and_resumes_without_execution(tmp_path):
    values = setup(tmp_path, infra=1)
    repo, runner, _, task, broker, recovery, _, _ = values
    coordinator = PipelineCoordinator(repo, runner, broker, recovery=recovery)
    report = coordinator.run(task)
    assert report["candidate_selected"]
    proposal = repo.list_proposals(task)[0]
    assert [a["status"] for a in repo.recovery_attempts(task, proposal.proposal_id)] == [
        "infrastructure_failure",
        "completed",
    ]
    before = list(runner.events)
    assert PipelineCoordinator(repo, runner, broker, recovery=recovery).run(task) == report
    assert runner.events == before
