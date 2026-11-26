"""Real RabbitMQ TTL redelivery, Redis quarantine and leased Docker evaluation."""

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.dependencies import DEAD_EXCHANGE, declare_topology
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.scheduler.blacklist import RedisBlacklist
from compiler_optimizer.scheduler.recovery import RecoveryController, Worker
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.recovery import RecoveryPolicy
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import package_from_files, read_package
from compiler_optimizer.workers.verification_plan import ArrayGenerator, VerificationPlan

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOVERY_INTEGRATION_TESTS") != "1",
        reason="requires Docker, RabbitMQ and Redis on their configured host ports",
    ),
]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class CountingRunner(DockerRunner):
    calls = 0
    fail_once = False

    def compile(self, *args, **kwargs):
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise InfrastructureError("injected compiler transport disconnect")
        return super().compile(*args, **kwargs)


def prepare(tmp_path, package, *, backend=None, fail_once=False):
    repo = Repository(tmp_path / "state")
    first, second = [
        CountingRunner(
            os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
            target=os.environ.get("WORKER_TARGET", "linux-x86_64"),
            watchdog_directory=tmp_path / "watchdogs",
        )
        for _ in range(2)
    ]
    baseline = run_baseline(package, repo, first)
    assert baseline.WhichOneof("outcome") == "success", baseline.failure
    first.calls = 0
    first.fail_once = fail_once
    cache = RedisBlacklist.from_settings(Settings.from_env("coordinator"))
    recovery = RecoveryController(
        repo,
        [Worker(runner=first), Worker(runner=second)],
        cache,
        policy=replace(
            RecoveryPolicy(),
            lease_seconds=6,
            heartbeat_seconds=0.5,
            worker_health_seconds=6,
            attempt_timeout_seconds=180,
            backoff_seconds=0.05,
            maximum_backoff_seconds=0.1,
            worker_cooldown_seconds=0.1,
            blacklist_ttl_seconds=60,
        ),
    )
    broker = RabbitProposalQueue(Settings.from_env("coordinator"))
    coordinator = CohortCoordinator(
        repo,
        first,
        broker,
        recovery=recovery,
        backends={"performance": backend} if backend else None,
    )
    return repo, first, second, baseline.meta.task_id, cache, broker, coordinator


def unique_package():
    files = dict(read_package(EXAMPLES / "sum-array").files)
    manifest = json.loads(files["submission.json"])
    manifest["name"] = "recovery-" + uuid4().hex
    files["submission.json"] = json.dumps(manifest).encode()
    return package_from_files(files)


def run(coordinator, task):
    return coordinator.run(
        task,
        agents=["performance"],
        verification_plan=VerificationPlan(generator=ArrayGenerator(273, 1, 1, 0, 8, -100, 100)),
        benchmark_plan=BenchmarkPlan(warmups=0, repetitions=7, minimum_sample_seconds=1.0),
    )


def test_real_infrastructure_retry_redelivers_to_another_healthy_worker(tmp_path):
    repo, first, second, task, _, _, coordinator = prepare(
        tmp_path, unique_package(), fail_once=True
    )
    audit = run(coordinator, task)
    assert audit["status"] == "retained_baseline", audit
    proposal = repo.list_proposals(task)[0]
    attempts = repo.recovery_attempts(task, proposal.proposal_id)
    assert [a["status"] for a in attempts] == ["infrastructure_failure", "completed"]
    assert attempts[0]["worker_id"] != attempts[1]["worker_id"]
    assert first.calls == 1 and second.calls > 0
    assert len(repo.list_benchmarks(task)) == 1
    counts = first.calls, second.calls
    assert coordinator.run(task) == audit
    assert counts == (first.calls, second.calls)
    assert not repo.recovery_outbox(task)


class OverflowPatch:
    def generate(self, request, sources):
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": "integration-volatile-overflow",
                "rationale": "Deliberate undefined behavior for recovery integration testing.",
                "edits": [
                    {
                        "path": "main.c",
                        "old": "int main(void) {",
                        "new": "int main(void) { volatile int maximum=2147483647; "
                        "volatile int overflow=maximum+1; (void)overflow;",
                    }
                ],
            }
        ).encode()


def test_real_confirmed_crash_deadletters_and_shared_redis_skips_exact_context(tmp_path):
    package = unique_package()
    repo, first, second, task, cache, broker, coordinator = prepare(
        tmp_path / "first", package, backend=OverflowPatch()
    )
    connection = broker._connect()
    channel = connection.channel()
    declare_topology(channel)
    observer = channel.queue_declare(queue="", exclusive=True).method.queue
    channel.queue_bind(queue=observer, exchange=DEAD_EXCHANGE, routing_key="recovery.#")
    key = None
    try:
        audit = run(coordinator, task)
        assert audit["status"] == "retained_baseline", audit
        proposal = repo.list_proposals(task)[0]
        job = repo.get_recovery_job(task, proposal.proposal_id)
        assert job["state"] == "quarantined"
        assert first.calls > 0 and second.calls > 0
        key = repo.recovery_outcome(task, proposal.proposal_id)["blacklist_key"]
        record = cache.get(key, repo.recovery_now())
        assert len({c["worker_id"] for c in record["confirmations"]}) == 2
        assert 0 < cache.client.pttl(key) <= 60000
        method, properties, _ = channel.basic_get(queue=observer, auto_ack=False)
        assert method is not None and properties.delivery_mode == 2
        assert properties.headers["recovery-reason"] == "confirmed_program_crash"
        channel.basic_ack(method.delivery_tag)
        other, a, b, other_task, _, _, resumed = prepare(
            tmp_path / "second", package, backend=OverflowPatch()
        )
        assert run(resumed, other_task)["status"] == "retained_baseline"
        assert a.calls == b.calls == 0
        receipt = other.recovery_outcome(
            other_task, other.list_proposals(other_task)[0].proposal_id
        )
        assert receipt["reason"] == "blacklisted_execution_context"
        assert receipt["blacklist_key"] == key
    finally:
        connection.close()
        if key is not None:
            cache.client.delete(key)
