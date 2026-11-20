"""Kill a leased worker process, sever AMQP before ack and inject duplicate deliveries."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pika
import pytest
from test_recovery_docker import prepare, run, unique_package

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.monitoring.metrics import Metrics
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.proposal_queue import DeliveryError, RabbitProposalQueue
from compiler_optimizer.scheduler.recovery import RecoveryController
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.docker_runner import DockerRunner

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOVERY_INTEGRATION_TESTS") != "1",
        reason="requires real Docker, RabbitMQ and Redis",
    ),
]


def test_worker_process_termination_recovers_expired_lease(tmp_path):
    # The current worker pool lives in the host coordinator process. Kill that
    # owner after lease acquisition; the broker must requeue its unacked delivery.
    code = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_recovery_docker import prepare, run, unique_package
root = Path(sys.argv[2])
repo, first, second, task, cache, broker, coordinator = prepare(root, unique_package())
(root/'task.json').write_text(task)
def die(*args, **kwargs):
    os._exit(73)
first.compile = die
run(coordinator, task)
"""
    root = tmp_path / "owner"
    child = subprocess.run(
        [sys.executable, "-c", code, str(Path(__file__).parent), str(root)],
        capture_output=True,
        timeout=90,
    )
    assert child.returncode == 73, child.stderr.decode()[-2000:]
    task = (root / "task.json").read_text()
    repo = Repository(root / "state")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=root / "replacement-watchdogs",
    )
    settings = Settings.from_env("coordinator")
    recovery = RecoveryController.local(repo, runner, settings)
    coordinator = CohortCoordinator(repo, runner, RabbitProposalQueue(settings), recovery=recovery)
    audit = coordinator.run(task)
    assert audit["status"] == "retained_baseline", audit
    proposal = repo.list_proposals(task)[0]
    attempts = repo.recovery_attempts(task, proposal.proposal_id)
    assert [a["status"] for a in attempts] == ["lease_expired", "completed"]
    assert attempts[0]["worker_id"] != attempts[1]["worker_id"]
    assert repo.get_recovery_job(task, proposal.proposal_id)["cleanup_token"] is None
    assert (
        b'optimizer_retries_total{reason="lease_expired"} 1.0' in Metrics(repository=repo).render()
    )


def test_broker_disconnect_before_ack_reuses_result_and_settles_duplicates(tmp_path):
    repo, first, second, task, _, broker, coordinator = prepare(tmp_path, unique_package())
    connect = broker._connect
    failed = [False]

    def disrupted_connect():
        connection = connect()
        get_channel = connection.channel

        def channel(*args, **kwargs):
            current = get_channel(*args, **kwargs)
            acknowledge = current.basic_ack

            def lose_ack(*args, **kwargs):
                if not failed[0] and repo.selection_progress(task):
                    failed[0] = True
                    connection.close()
                    raise pika.exceptions.AMQPConnectionError("injected connection loss")
                return acknowledge(*args, **kwargs)

            current.basic_ack = lose_ack
            return current

        connection.channel = channel
        return connection

    broker._connect = disrupted_connect
    with pytest.raises(DeliveryError):
        run(coordinator, task)
    assert failed[0] and repo.selection_progress(task)
    counts = first.calls, second.calls, len(repo.list_benchmarks(task))
    before = Metrics(repository=repo).render().decode()
    broker._connect = connect
    # Resume publishes another immutable copy while the unacked original is also
    # present. Both must be settled without repeating successful measurement.
    audit = coordinator.run(task)
    assert audit["status"] == "retained_baseline"
    assert counts == (first.calls, second.calls, len(repo.list_benchmarks(task)))
    assert len(repo.recovery_attempts(task, repo.list_proposals(task)[0].proposal_id)) == 1
    assert before.count("optimizer_verification_results_total{") == (
        Metrics(repository=repo).render().decode().count("optimizer_verification_results_total{")
    )


@pytest.mark.parametrize(
    "mode,reason",
    [("loop", "TIMEOUT"), ("memory", "MEMORY_LIMIT"), ("compiler", "COMPILATION_ERROR")],
)
def test_failure_proposals_never_pass_acceptance_and_expose_metrics(tmp_path, mode, reason):
    class FailingPatch:
        def generate(self, request, sources):
            new = {
                "loop": "int main(void) { volatile unsigned value=0; for (;;) value++;",
                "memory": "#include <stdlib.h>\nint main(void) { "
                "volatile char *p=malloc(256UL*1024*1024); if(!p) return 2; "
                "for(unsigned long i=0;i<256UL*1024*1024;i+=4096) p[i]=1;",
                "compiler": "int main(void) { invalid C syntax here;",
            }[mode]
            return json.dumps(
                {
                    "schema_version": 1,
                    "parent_source_sha256": request.source.bundle.sha256,
                    "transformation": "failure-injection-" + mode,
                    "rationale": "Deliberately unsafe candidate for failure testing.",
                    "edits": [{"path": "main.c", "old": "int main(void) {", "new": new}],
                }
            ).encode()

    from compiler_optimizer.workers.package import package_from_files

    files = dict(unique_package().files)
    manifest = json.loads(files["submission.json"])
    manifest["limits"].update(run_wall_seconds=1, run_memory_mib=64)
    files["submission.json"] = json.dumps(manifest).encode()
    repo, _, _, task, cache, _, coordinator = prepare(
        tmp_path, package_from_files(files), backend=FailingPatch()
    )
    key = None
    try:
        audit = run(coordinator, task)
        assert audit["status"] == "retained_baseline", audit
        assert audit["selected_proposal_id"] is None and not repo.list_benchmarks(task)
        payload = Metrics(repository=repo).render().decode()
        assert f'reason="{reason}"' in payload
        proposal = repo.list_proposals(task)[0]
        job = repo.get_recovery_job(task, proposal.proposal_id)
        if mode != "compiler":
            assert job["state"] == "quarantined"
            key = repo.recovery_outcome(task, proposal.proposal_id)["blacklist_key"]
            assert cache.client.exists(key)
        else:
            assert job["state"] == "completed"
            assert len(repo.recovery_attempts(task, proposal.proposal_id)) == 1
    finally:
        if key:
            cache.client.delete(key)
