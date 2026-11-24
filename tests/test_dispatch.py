"""Automatic intake, baseline/cohort checkpoints, bounded retry and restart safety."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import grpc
import pytest
from test_selection import CohortBroker, MeasuredRunner

from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.dispatch import SubmissionDispatcher
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.shared.workflow import default_workflow
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import ConflictError
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.docker_runner import InfrastructureError
from compiler_optimizer.workers.package import read_package


class Crash(BaseException):
    """A process loss, not an ordinary retryable execution exception."""


@pytest.fixture
def submitted(tmp_path):
    package = read_package(Path(__file__).resolve().parents[1] / "examples/sum-array")
    repo = Repository(tmp_path / "state")
    runner = MeasuredRunner(package)
    request = pb.Submission(
        meta=metadata(str(uuid4())),
        manifest=package.manifest,
        package=repo.artifacts.put(package.package_bundle),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(package.source_bundle)),
        compiler=runner.compiler(package.manifest.language),
    )
    return repo, runner, request


def dispatcher(repo, runner, broker=None):
    return SubmissionDispatcher(
        repo,
        Settings(role="coordinator"),
        runner_factory=lambda _: runner,
        broker_factory=lambda: broker or CohortBroker(repo),
        recovery_factory=lambda *_: None,
    )


def enqueue(repo, request):
    config = default_workflow()
    config["agents"] = ["performance", "architecture"]
    repo.save_submission(request, execution=config)


def ready_retry(repo, task):
    with repo._transaction() as db:
        db.execute("UPDATE submission_executions SET available_at_ms=0 WHERE task_id=?", (task,))


def test_intake_atomically_enqueues_frozen_idempotent_work(submitted):
    repo, _, request = submitted
    service = CoordinatorService(repo)
    receipt = service.SubmitSubmission(request, Mock())
    assert receipt.accepted_message_id == request.meta.message_id
    execution = Repository(repo.root).get_execution(request.meta.task_id)
    assert execution["stage"] == "queued" and execution["attempts"] == 0
    service = CoordinatorService(
        repo, workflow_configuration={**default_workflow(), "agents": ["memory"]}
    )
    service.SubmitSubmission(request, Mock())
    assert repo.get_execution(request.meta.task_id) == execution


def test_invalid_package_cannot_leave_task_or_queue_entry(submitted):
    repo, _, request = submitted
    request.source.bundle.CopyFrom(repo.artifacts.put(b"incorrect-source"))
    with pytest.raises(ContractError, match="package/source"):
        enqueue(repo, request)
    assert repo.list_tasks() == [] and repo.pending_executions() == []


def test_automatic_submission_runs_its_frozen_debate_policy(submitted):
    from compiler_optimizer.shared.debate import DebatePolicy

    repo, runner, request = submitted
    config = {
        **default_workflow(),
        "agents": ["performance"],
        "debate_policy": json.loads(
            DebatePolicy(heldout_cases=({"stdin": "7\n1 2 3 4 5 6 7\n", "args": []},)).encode()
        ),
    }
    repo.save_submission(request, execution=config)
    worker = dispatcher(repo, runner)
    assert worker.run_once()
    task = request.meta.task_id
    execution = repo.get_execution(task)
    assert execution["finished"] and execution["error_code"] is None
    assert repo.get_task(task)["status"] == "completed"
    run = repo.debate_run(task)
    assert len(run["slots"]) == 2 and run["final_state"] == "completed"
    assert execution["configuration"]["debate_policy"] == config["debate_policy"]


def test_queue_insert_failure_rolls_back_submission(submitted):
    repo, _, request = submitted
    with repo._transaction() as db:
        db.execute(
            "CREATE TRIGGER deny_execution BEFORE INSERT ON submission_executions "
            "BEGIN SELECT RAISE(ABORT, 'disk test'); END"
        )
    with pytest.raises(ConflictError, match="disk test"):
        enqueue(repo, request)
    assert repo.list_tasks() == []


def test_automatic_execution_reuses_task_and_enforces_final_selection(submitted):
    repo, runner, request = submitted
    enqueue(repo, request)
    worker = dispatcher(repo, runner)
    assert worker.run_once()
    task = request.meta.task_id
    assert [t["task_id"] for t in repo.list_tasks()] == [task]
    assert repo.get_task(task)["status"] == "completed"
    assert repo.get_execution(task)["stage"] == "completed"
    audit = repo.selection_report(task)[1]
    assert audit["status"] == "selected"
    assert all(
        c["votes"] == {"correctness": "approve", "critic": "approve"} for c in audit["candidates"]
    )
    assert len(repo.list_benchmarks(task)) == 2
    before = list(runner.events)
    assert not dispatcher(Repository(repo.root), runner).run_once()
    assert runner.events == before


@pytest.mark.parametrize(
    "boundary", ["compile", "baseline_commit", "cohort_prepare", "broker_ack", "final_commit"]
)
def test_restart_resumes_original_task_at_durable_boundaries(submitted, monkeypatch, boundary):
    repo, runner, request = submitted
    enqueue(repo, request)
    task = request.meta.task_id
    broker = CohortBroker(repo)
    worker = dispatcher(repo, runner, broker)
    with monkeypatch.context() as patch:
        if boundary == "compile":
            patch.setattr(runner, "compile", Mock(side_effect=Crash()))
        elif boundary == "baseline_commit":
            original = repo.save_result

            def committed(result):
                original(result)
                raise Crash()

            patch.setattr(repo, "save_result", committed)
        elif boundary == "cohort_prepare":
            original = repo.begin_selection

            def prepared(*args):
                original(*args)
                raise Crash()

            patch.setattr(repo, "begin_selection", prepared)
        elif boundary == "broker_ack":
            original = broker.exchange

            def ack_lost(*args):
                original(*args)
                raise Crash()

            patch.setattr(broker, "exchange", ack_lost)
        else:
            patch.setattr(repo, "finish_execution", Mock(side_effect=Crash()))
        with pytest.raises(Crash):
            worker.run_once()
    baseline_count = sum(r.WhichOneof("subject") == "baseline" for r in repo.list_results(task))
    benchmarks = len(repo.list_benchmarks(task))
    reopened = Repository(repo.root)
    assert dispatcher(reopened, runner).run_once()
    assert reopened.get_task(task)["status"] == "completed"
    assert reopened.get_execution(task)["finished"]
    assert len(reopened.list_tasks()) == 1
    if baseline_count:
        assert sum(r.WhichOneof("subject") == "baseline" for r in reopened.list_results(task)) == 1
    assert len(reopened.list_benchmarks(task)) == 2
    if boundary == "final_commit":
        assert benchmarks == 2


def test_baseline_infrastructure_retry_and_attempt_cap_survive_restart(submitted, monkeypatch):
    repo, runner, request = submitted
    enqueue(repo, request)
    task = request.meta.task_id
    original = runner.compile
    monkeypatch.setattr(runner, "compile", Mock(side_effect=InfrastructureError("unavailable")))
    assert dispatcher(repo, runner).run_once()
    assert repo.get_task(task)["status"] == "running"
    assert repo.get_execution(task)["stage"] == "retry_wait"
    assert not dispatcher(repo, runner).run_once()
    monkeypatch.setattr(runner, "compile", original)
    ready_retry(repo, task)
    assert dispatcher(Repository(repo.root), runner).run_once()
    assert repo.get_task(task)["status"] == "completed"
    baselines = [r for r in repo.list_results(task) if r.WhichOneof("subject") == "baseline"]
    assert [r.attempt for r in baselines] == [1, 2]


def test_dependency_outage_exhausts_bounded_attempts(submitted):
    repo, runner, request = submitted
    enqueue(repo, request)
    task = request.meta.task_id
    worker = dispatcher(repo, runner)
    worker.runner_factory = Mock(side_effect=InfrastructureError("secret credentials"))
    for attempt in range(1, 4):
        ready_retry(repo, task)
        assert worker.run_once()
        assert repo.get_execution(task)["attempts"] == attempt
    execution = repo.get_execution(task)
    assert execution["finished"] and execution["error_code"] == "InfrastructureError"
    assert repo.get_task(task)["status"] == "failed"
    assert "secret" not in json.dumps(execution)


def test_dispatcher_does_not_compete_with_existing_orchestrator(submitted):
    repo, runner, request = submitted
    enqueue(repo, request)
    with repo.pipeline_lock():
        assert not dispatcher(repo, runner).run_once()
    assert repo.get_execution(request.meta.task_id)["attempts"] == 0


def test_wrong_output_is_rejected_and_cannot_be_selected(submitted):
    repo, runner, request = submitted
    runner.fail_kind = "baseline"
    enqueue(repo, request)
    dispatcher(repo, runner).run_once()
    task = request.meta.task_id
    assert repo.get_task(task)["status"] == "failed"
    assert repo.get_execution(task)["stage"] == "failed"
    assert not repo.list_proposals(task) and repo.get_decision(task) is None


def test_live_rpc_submission_automatically_completes_without_cli(submitted):
    repo, runner, request = submitted
    stop = threading.Event()
    thread = threading.Thread(target=dispatcher(repo, runner).run, args=(stop,), daemon=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        rpc.add_CoordinatorServiceServicer_to_server(CoordinatorService(repo), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        thread.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                client = rpc.CoordinatorServiceStub(channel)
                client.SubmitSubmission(request, timeout=3)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    result = client.GetOptimizationResult(
                        pb.OptimizationQuery(task_id=request.meta.task_id), timeout=3
                    )
                    if result.stage == "completed":
                        break
                    time.sleep(0.05)
                assert result.status == "completed" and result.stage == "completed"
                assert result.selected_source.bundle.sha256
                assert json.loads(result.report_json)["status"] == "selected"
        finally:
            stop.set()
            thread.join(timeout=5)
            server.stop(0).wait(timeout=3)


def test_intake_only_mode_does_not_enqueue_legacy_submissions(submitted):
    repo, _, request = submitted
    CoordinatorService(repo, execution_enabled=False).SubmitSubmission(request, Mock())
    assert repo.get_execution(request.meta.task_id) is None


def test_committed_final_decision_wins_over_late_transport_failure(submitted):
    repo, runner, request = submitted
    enqueue(repo, request)
    dispatcher(repo, runner).run_once()
    task = request.meta.task_id
    with repo._transaction() as db:
        db.execute("UPDATE submission_executions SET finished=0 WHERE task_id=?", (task,))
    repo.finish_execution(task, error_code="DeliveryError", retry=True)
    execution = repo.get_execution(task)
    assert execution["stage"] == "completed" and execution["finished"]
    assert execution["error_code"] is None and execution["available_at_ms"] == 0


def test_unconfirmed_cleanup_prevents_resumed_execution(submitted, monkeypatch):
    repo, runner, request = submitted
    enqueue(repo, request)
    worker = dispatcher(repo, runner)
    path = worker.watchdogs(request.meta.task_id) / "owned"
    path.mkdir(parents=True)
    (path / "lease.json").write_text(
        json.dumps(
            {
                "container": "compiler-baseline-" + "a" * 32,
                "token": "owned-token",
            }
        )
    )
    remove = Mock(return_value="ownership_mismatch")
    monkeypatch.setattr("compiler_optimizer.runtime.dispatch.remove_owned", remove)
    worker.runner_factory = Mock()
    worker.run_once()
    assert remove.called and not worker.runner_factory.called
    assert repo.get_execution(request.meta.task_id)["stage"] == "retry_wait"


def test_only_this_submissions_watchdogs_are_cleaned(submitted, monkeypatch):
    repo, runner, request = submitted
    enqueue(repo, request)
    unrelated = repo.root / "watchdogs" / "other-task"
    unrelated.mkdir(parents=True)
    (unrelated / "lease.json").write_text("unrelated legacy lease")
    remove = Mock()
    monkeypatch.setattr("compiler_optimizer.runtime.dispatch.remove_owned", remove)
    dispatcher(repo, runner).run_once()
    assert not remove.called
    assert repo.get_execution(request.meta.task_id)["stage"] == "completed"


def test_existing_submission_preserves_local_agents_after_rpc_deployment(submitted):
    from dataclasses import replace

    repo, runner, request = submitted
    config = default_workflow()
    config.pop("agent_backend")  # A task accepted before agent RPC configuration existed.
    config["agents"] = ["performance"]
    repo.save_submission(request, execution=config)
    owner = dispatcher(repo, runner)
    owner.settings = replace(owner.settings, agent_backend="rpc", agent_timeout_seconds=0.05)
    assert owner.run_once()
    assert repo.get_task(request.meta.task_id)["status"] == "completed"
    assert not repo.list_agent_rpc_calls(request.meta.task_id)
