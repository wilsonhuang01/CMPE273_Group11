"""Performance patch generation, durable orchestration, broker delivery, and coordinator results."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import grpc
import pytest
from test_benchmark import environment
from test_verification import scenario as scenario

from compiler_optimizer.agents.performance import ExamplePerformanceBackend
from compiler_optimizer.coordinator.pipeline import PipelineCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.proposal_queue import DeliveryError, RabbitProposalQueue
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.messaging import encode_event
from compiler_optimizer.shared.patches import apply_patch, parse_patch
from compiler_optimizer.shared.recovery import DeadLetterResult, RetryDelivery
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import StateError


class LocalBroker:
    """Transport fake only; real storage, agent, verification, benchmarking and selection run."""

    def __init__(self, repository):
        self.repository = repository
        self.events = []
        self.fail = None

    def exchange(self, proposal, process, on_published):
        self.events.append("publish")
        if self.fail == "publish":
            raise DeliveryError("broker down")
        on_published()
        result = process(proposal)
        assert self.repository.pipeline_report(proposal.meta.task_id) is not None
        if self.fail == "ack":
            raise DeliveryError("ack lost")
        self.events.append("ack")
        return result


@pytest.fixture
def workflow(scenario):
    repository, runner, task, _, _ = scenario
    original_execute = runner.execute
    runner.benchmark_environment = lambda cpu: environment()
    runner.pipeline_mode = "good"

    def execute(binary, case, package, *, build_profile="normal", cpu_id=None):
        result = original_execute(binary, case, package, build_profile=build_profile)
        role = runner.binaries[binary]
        result.wall_seconds = 1.0 if role == "baseline" else 0.8
        result.sandbox = {"cpu_id": cpu_id, "watchdog": {"state": "removed"}}
        if runner.pipeline_mode == "wrong" and role == "candidate":
            result.stdout = b"incorrect\n"
        return result

    runner.execute = execute
    broker = LocalBroker(repository)
    coordinator = PipelineCoordinator(repository, runner, broker)
    return repository, runner, task, broker, coordinator


def test_complete_milestone_selects_only_measured_verified_gain(workflow):
    repository, runner, task, broker, coordinator = workflow
    report = coordinator.run(task)
    assert broker.events == ["publish", "ack"]
    assert report["status"] == "completed"
    assert report["candidate_selected"]
    assert report["verification"] == "passed"
    assert report["benchmark"]["analysis"]["qualified"]
    assert report["rationale"] and report["transformation"] == "stdio-input-buffer-64k"
    assert repository.pipeline_report(task)[1] == report
    assert repository.get_pipeline(task)["published"]
    before = list(runner.events)
    assert coordinator.run(task) == report
    assert before == runner.events
    assert broker.events == ["publish", "ack"]


@pytest.mark.parametrize("condition", ["wrong", "short", "emulated", "reference-ub"])
def test_rejected_or_unconvincing_candidate_returns_safe_result(workflow, condition):
    repository, runner, task, broker, coordinator = workflow
    if condition == "wrong":
        runner.pipeline_mode = "wrong"
    elif condition == "reference-ub":
        runner.mode = "baseline-ub"
    elif condition == "emulated":
        runner.benchmark_environment = lambda cpu: {
            **environment(),
            "native_amd64": False,
            "engine_architecture": "aarch64",
        }
    else:
        from compiler_optimizer.shared.benchmarking import BenchmarkPlan

        report = coordinator.run(task, benchmark_plan=BenchmarkPlan(minimum_sample_seconds=2.0))
    if condition != "short":
        report = coordinator.run(task)
    assert not report["candidate_selected"]
    assert report["status"] == ("failed" if condition == "reference-ub" else "completed")
    assert broker.events[-1] == "ack"
    if condition == "wrong":
        assert report["benchmark"] is None
        assert report["failure"]["reason"] == "OUTPUT_MISMATCH"
    if condition == "reference-ub":
        assert report["selected_source"] is None
    else:
        assert (
            report["selected_source"]["sha256"]
            == repository.get_task(task)["submission"].source.bundle.sha256
        )


def test_broker_outage_resumes_exact_patch_without_regenerating(workflow):
    repository, runner, task, broker, coordinator = workflow
    backend = Mock(wraps=ExamplePerformanceBackend())
    broker.fail = "publish"
    with pytest.raises(DeliveryError):
        coordinator.run(task, backend)
    assert repository.get_pipeline(task)["phase"] == "queued"
    assert not repository.get_pipeline(task)["published"]
    assert backend.generate.call_count == 1
    broker.fail = None
    assert coordinator.run(task)["status"] == "completed"
    assert backend.generate.call_count == 1


def test_lost_ack_does_not_repeat_execution(workflow):
    repository, runner, task, broker, coordinator = workflow
    broker.fail = "ack"
    with pytest.raises(DeliveryError):
        coordinator.run(task)
    before = list(runner.events)
    report = coordinator.run(task)
    assert report["status"] == "completed"
    assert before == runner.events


def test_resume_after_decision_before_final_report(workflow, monkeypatch):
    repository, runner, task, broker, coordinator = workflow
    finish = repository.finish_pipeline
    monkeypatch.setattr(repository, "finish_pipeline", Mock(side_effect=OSError("owner stopped")))
    with pytest.raises(OSError):
        coordinator.run(task)
    assert repository.get_task(task)["status"] == "completed"
    assert repository.pipeline_report(task) is None
    before = list(runner.events)
    monkeypatch.setattr(repository, "finish_pipeline", finish)
    assert coordinator.run(task)["candidate_selected"]
    assert before == runner.events


def test_final_report_cannot_misrepresent_committed_evidence(workflow, monkeypatch):
    repository, _, task, _, coordinator = workflow
    finish = repository.finish_pipeline
    captured = []

    def interrupt(task_id, ref):
        captured.append(ref)
        raise OSError("interrupted before report commit")

    monkeypatch.setattr(repository, "finish_pipeline", interrupt)
    with pytest.raises(OSError):
        coordinator.run(task)
    original = json.loads(repository.artifacts.get(captured[0]))
    for key, value in (
        ("rationale", "made up"),
        ("benchmark", {}),
        ("correctness_report", {}),
        ("candidate_selected", False),
    ):
        forged = {**original, key: value}
        with pytest.raises(ContractError):
            finish(task, repository.artifacts.put(json.dumps(forged).encode()))
    assert repository.pipeline_report(task) is None
    monkeypatch.setattr(repository, "finish_pipeline", finish)
    assert coordinator.run(task) == original


def test_resume_reuses_verification_before_interrupted_benchmark(workflow, monkeypatch):
    from compiler_optimizer.workers import pipeline

    repository, runner, task, broker, coordinator = workflow
    benchmark = pipeline.run_benchmark
    monkeypatch.setattr(pipeline, "run_benchmark", Mock(side_effect=OSError("owner stopped")))
    with pytest.raises(OSError):
        coordinator.run(task)
    compile_count = sum(event[0] == "compile" for event in runner.events)
    monkeypatch.setattr(pipeline, "run_benchmark", benchmark)
    assert coordinator.run(task)["candidate_selected"]
    assert compile_count == sum(event[0] == "compile" for event in runner.events)


def test_resume_cannot_change_saved_plan(workflow):
    from compiler_optimizer.shared.benchmarking import BenchmarkPlan

    repository, runner, task, broker, coordinator = workflow
    broker.fail = "publish"
    with pytest.raises(DeliveryError):
        coordinator.run(task)
    with pytest.raises(ContractError, match="stored patch and plans"):
        coordinator.run(task, benchmark_plan=BenchmarkPlan(seed=1))


def test_concurrent_local_owners_are_rejected(workflow):
    repository, _, task, _, coordinator = workflow
    with repository.pipeline_lock(), pytest.raises(StateError, match="another pipeline"):
        coordinator.run(task)


def test_corrupt_patch_blocks_delivery_execution(workflow):
    from compiler_optimizer.storage.errors import ArtifactError

    repository, runner, task, broker, coordinator = workflow
    broker.fail = "publish"
    with pytest.raises(DeliveryError):
        coordinator.run(task)
    ref = repository.get_pipeline(task)["configuration"]["patch"]
    (repository.artifacts.root / ref["key"]).write_bytes(b"corrupt")
    before = list(runner.events)
    broker.fail = None
    with pytest.raises(ArtifactError):
        coordinator.run(task)
    assert runner.events == before
    assert repository.get_task(task)["status"] == "running"


def patch():
    return {
        "schema_version": 1,
        "parent_source_sha256": "a" * 64,
        "rationale": "Reason",
        "transformation": "unit-change",
        "edits": [{"path": "main.c", "old": "a", "new": "b"}],
    }


@pytest.mark.parametrize(
    "kind",
    [
        "parent",
        "fixture",
        "traversal",
        "ambiguous",
        "missing",
        "unknown",
        "empty",
        "duplicate",
        "unchanged",
        "oversized",
    ],
)
def test_unsafe_or_ambiguous_patch_is_rejected(kind):
    value = patch()
    sources = {"main.c": b"a"}
    if kind == "parent":
        value["parent_source_sha256"] = "b" * 64
    elif kind in {"fixture", "traversal"}:
        value["edits"][0]["path"] = "fixtures.in" if kind == "fixture" else "../main.c"
    elif kind == "ambiguous":
        sources["main.c"] = b"aa"
    elif kind == "missing":
        value["edits"][0]["old"] = "x"
    elif kind == "unknown":
        value["compiler_flags"] = ["-ffast-math"]
    elif kind == "empty":
        value["edits"] = []
    elif kind == "unchanged":
        value["edits"][0]["new"] = "a"
    elif kind == "oversized":
        value["rationale"] = "x" * (1024 * 1024)
    data = json.dumps(value).encode()
    if kind == "duplicate":
        data = data.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')
    with pytest.raises(ContractError):
        apply_patch(data, sources, "a" * 64)


def test_patch_applies_without_mutating_original():
    sources = {"main.c": b"a", "header.h": b"unchanged"}
    parsed, changed = apply_patch(json.dumps(patch()).encode(), sources, "a" * 64)
    assert changed == {"main.c": b"b", "header.h": b"unchanged"}
    assert sources["main.c"] == b"a"
    assert parsed == parse_patch(json.dumps(patch()).encode())


def test_bad_agent_output_never_reaches_broker(workflow):
    repository, runner, task, broker, coordinator = workflow
    backend = Mock()
    backend.generate.return_value = b'{"instructions": "run this shell script"}'
    with pytest.raises(ContractError):
        coordinator.run(task, backend)
    assert repository.get_pipeline(task) is None
    assert repository.get_task(task)["status"] == "running"
    assert broker.events == []


@pytest.mark.sqlite_only
def test_schema_two_migration_keeps_task_history(workflow):
    repository, _, task, _, _ = workflow
    history = repository.history(task)
    with sqlite3.connect(repository.database) as db:
        for table in (
            "dispatch_claims",
            "debate_events",
            "debate_slots",
            "debate_runs",
            "agent_rpc_calls",
            "measurement_claims",
            "cohort_work",
            "cohort_runs",
            "measurement_windows",
            "recovery_outbox",
            "recovery_blacklist",
            "recovery_attempts",
            "recovery_jobs",
            "recovery_workers",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DROP TABLE selection_progress")
        db.execute("DROP TABLE selection_sessions")
        db.execute("DROP TABLE agent_iterations")
        db.execute("DROP TABLE pipeline_runs")
        db.execute("DROP TABLE submission_executions")
        db.execute("DROP TABLE fleet_jobs")
        db.execute("DROP TABLE fleet_workers")
        db.execute("PRAGMA user_version=2")
    reopened = Repository(repository.root)
    assert reopened.history(task) == history
    assert reopened.get_pipeline(task) is None
    with sqlite3.connect(repository.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 13


def broker_fixture(proposal):
    event = encode_event(proposal)
    properties = SimpleNamespace(
        content_type=event.content_type,
        delivery_mode=2,
        type=event.message_type,
        message_id=event.message_id,
        correlation_id=event.correlation_id,
    )
    connection = Mock()
    channel = connection.channel.return_value
    channel.basic_publish.return_value = None
    channel.basic_get.return_value = (SimpleNamespace(delivery_tag=1), properties, event.body)
    transport = RabbitProposalQueue(Settings.from_env("coordinator", {}))
    transport._connect = Mock(return_value=connection)
    return transport, connection, channel, properties, event


def stored_proposal(workflow):
    repository, _, task, broker, coordinator = workflow
    broker.fail = "publish"
    with pytest.raises(DeliveryError):
        coordinator.run(task)
    proposal_id = repository.get_pipeline(task)["proposal_id"]
    return next(p for p in repository.list_proposals(task) if p.proposal_id == proposal_id)


def test_real_transport_contract_confirms_before_processing_and_acks_after_commit(workflow):
    proposal = stored_proposal(workflow)
    transport, connection, channel, _, event = broker_fixture(proposal)
    order = []
    channel.basic_publish.side_effect = lambda **kw: order.append("confirm")
    channel.basic_ack.side_effect = lambda **kw: order.append("ack")
    process_thread = []

    def process(incoming):
        assert incoming == proposal
        process_thread.append(threading.get_ident())
        order.append("commit")
        return "result"

    assert transport.exchange(proposal, process, lambda: order.append("outbox")) == "result"
    assert order == ["confirm", "outbox", "commit", "ack"]
    assert process_thread[0] != threading.get_ident()
    kwargs = channel.basic_publish.call_args.kwargs
    assert kwargs["mandatory"] and kwargs["body"] == event.body
    assert kwargs["routing_key"].endswith(proposal.proposal_id)
    assert channel.queue_delete.called


def test_transport_services_heartbeats_during_worker_execution(workflow):
    proposal = stored_proposal(workflow)
    transport, connection, channel, _, _ = broker_fixture(proposal)
    heartbeat = threading.Event()
    connection.process_data_events.side_effect = lambda **kw: heartbeat.set()

    def process(_):
        assert heartbeat.wait(timeout=2)
        return "committed"

    assert transport.exchange(proposal, process, lambda: None) == "committed"
    assert connection.process_data_events.called


def test_transport_deadletters_poison_and_requeues_uncommitted_failure(workflow):
    proposal = stored_proposal(workflow)
    transport, connection, channel, props, event = broker_fixture(proposal)
    channel.basic_get.side_effect = [
        (SimpleNamespace(delivery_tag=1), props, b"bad"),
        (SimpleNamespace(delivery_tag=2), props, event.body),
    ]

    def process(_):
        raise RuntimeError("worker interrupted")

    with pytest.raises(RuntimeError):
        transport.exchange(proposal, process, lambda: None)
    assert [call.kwargs["requeue"] for call in channel.basic_nack.call_args_list] == [False, True]
    assert not channel.basic_ack.called


def test_transport_does_not_claim_unconfirmed_publication(workflow):
    proposal = stored_proposal(workflow)
    transport, connection, channel, _, _ = broker_fixture(proposal)
    channel.basic_publish.return_value = False
    callback = Mock()
    with pytest.raises(DeliveryError):
        transport.exchange(proposal, callback, callback)
    assert not callback.called


def test_transport_confirms_delayed_retry_and_deadletter_before_ack(workflow):
    proposal = stored_proposal(workflow)
    transport, _, channel, _, _ = broker_fixture(proposal)
    order = []
    channel.basic_publish.side_effect = lambda **kw: order.append(
        "retry" if kw["properties"].expiration else "publish"
    )
    channel.basic_ack.side_effect = lambda **kw: order.append("ack")
    calls = [0]

    def process(_):
        calls[0] += 1
        if calls[0] == 1:
            raise RetryDelivery(10, "retry-id", lambda: order.append("retry-committed"))
        return DeadLetterResult(
            "excluded",
            "confirmed_program_crash",
            "terminal-id",
            lambda: order.append("deadletter-committed"),
        )

    assert transport.exchange(proposal, process, lambda: None) == "excluded"
    assert order == [
        "publish",
        "retry",
        "retry-committed",
        "ack",
        "publish",
        "deadletter-committed",
        "ack",
    ]
    retry = channel.basic_publish.call_args_list[1].kwargs
    assert retry["properties"].expiration == "10"
    assert retry["properties"].delivery_mode == 2 and retry["mandatory"]
    assert retry["properties"].headers["recovery-event-id"] == "retry-id"


def test_failed_retry_confirmation_leaves_original_delivery_unacknowledged(workflow):
    proposal = stored_proposal(workflow)
    transport, _, channel, _, _ = broker_fixture(proposal)
    channel.basic_publish.side_effect = [True, False]

    def process(_):
        raise RetryDelivery(10, "retry-id")

    with pytest.raises(DeliveryError):
        transport.exchange(proposal, process, lambda: None)
    assert not channel.basic_ack.called


def test_coordinator_returns_final_result_over_grpc(workflow):
    repository, _, task, _, coordinator = workflow
    expected = coordinator.run(task)
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        rpc.add_CoordinatorServiceServicer_to_server(CoordinatorService(repository), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                client = rpc.CoordinatorServiceStub(channel)
                result = client.GetOptimizationResult(pb.OptimizationQuery(task_id=task), timeout=3)
                assert json.loads(result.report_json) == expected
                assert result.status == "completed" and result.stage == "completed"
                assert result.selected_source.bundle.sha256 == expected["selected_source"]["sha256"]
                with pytest.raises(grpc.RpcError) as error:
                    client.GetOptimizationResult(pb.OptimizationQuery(task_id="bad"), timeout=3)
                assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        finally:
            server.stop(0).wait()
