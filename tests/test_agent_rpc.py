"""Real role processes, unshared artifacts, strict evidence gates and bounded failure."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
from test_agents import make_workflow
from test_pipeline import LocalBroker

from compiler_optimizer.agents.rpc_client import AgentClient, AgentUnavailable
from compiler_optimizer.agents.rpc_service import AgentService
from compiler_optimizer.coordinator.pipeline import PipelineCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.security import BearerAuth
from compiler_optimizer.shared.agent_roles import ROLES
from compiler_optimizer.shared.agents_rpc import canonical, payload, sha
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.messaging import encode_message
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage.errors import StateError

TOKEN = "agent-rpc-test-" + "a" * 32


def free_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


class Endpoints(dict):
    pass


@pytest.fixture(scope="module")
def agents(tmp_path_factory):
    root = tmp_path_factory.mktemp("agent-processes")
    processes, endpoints, logs = [], Endpoints(), []
    launches = {}
    try:
        for spec in ROLES.values():
            endpoint = f"127.0.0.1:{free_port()}"
            endpoints[spec.name] = endpoint
            log = (root / (spec.name + ".log")).open("w")
            logs.append(log)
            env = dict(
                os.environ,
                STATE_DIRECTORY=str(root / spec.name),
                GRPC_HOST="127.0.0.1",
                GRPC_PORT=endpoint.rpartition(":")[2],
                HEALTH_HOST="127.0.0.1",
                HEALTH_PORT=str(free_port()),
                AGENT_API_TOKEN=TOKEN,
                EXECUTION_ENABLED="false",
                TRANSFER_ENABLED="false",
            )
            env.pop("RPC_TLS_CERT", None)
            env.pop("RPC_TLS_KEY", None)
            env.pop("AGENT_DEV_TLS", None)
            process = subprocess.Popen(
                [sys.executable, "-m", "compiler_optimizer.runtime", "--role", spec.name],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            processes.append(process)
            launches[spec.name] = (process, env, log)
            with grpc.insecure_channel(endpoint) as channel:
                try:
                    grpc.channel_ready_future(channel).result(timeout=10)
                except BaseException:
                    pytest.fail((root / (spec.name + ".log")).read_text())

        def restart(name):
            process, env, log = launches[name]
            process.terminate()
            process.wait(timeout=8)
            replacement = subprocess.Popen(
                [sys.executable, "-m", "compiler_optimizer.runtime", "--role", name],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            processes.append(replacement)
            launches[name] = (replacement, env, log)
            with grpc.insecure_channel(endpoints[name]) as channel:
                grpc.channel_ready_future(channel).result(timeout=10)

        endpoints.restart = restart
        yield (
            root,
            replace(
                Settings("coordinator"),
                agent_backend="rpc",
                agent_token=TOKEN,
                agent_endpoints=json.dumps(endpoints),
            ),
            endpoints,
        )
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()


def pipeline(tmp_path, settings, *, example="sum-array"):
    repo, runner, task, _ = make_workflow(tmp_path, example)
    client = AgentClient(repo, settings)
    coordinator = PipelineCoordinator(repo, runner, LocalBroker(repo), agent_client=client)
    return repo, runner, task, coordinator, client


@pytest.mark.parametrize(
    "role,example",
    [("performance", "sum-array"), ("architecture", "sum-array"), ("memory", "sum-array-buffered")],
)
def test_full_pipeline_uses_independent_generation_review_and_planner_processes(
    tmp_path, agents, role, example
):
    root, settings, _ = agents
    repo, runner, task, coordinator, client = pipeline(tmp_path, settings, example=example)
    client.check_health()
    report = coordinator.run(task, agent=role)
    assert report["verification"] == "passed", report
    assert all(r["verdict"] == "approve" for r in report["reviews"].values())
    assert report["benchmark"] is not None and report["benchmark_work"] is not None
    calls = repo.list_agent_rpc_calls(task)
    assert {r["role"] for r in calls} == {
        getattr(pb, role.upper()),
        pb.CORRECTNESS,
        pb.CRITIC,
        pb.BENCHMARK,
    }
    assert all(r["status"] == "completed" for r in calls)
    identities = set()
    for call in calls:
        with repo._transaction(write=False) as db:
            receipt = json.loads(repo.artifacts.get(repo._ref(db, call["receipt_key"])))
        identities.add(receipt["provenance"]["service_id"])
    assert len(identities) == 4
    for spec in ROLES.values():
        assert (root / spec.name / "agent.sqlite3").exists()
        assert not (root / spec.name / "state.sqlite3").exists()
    proposal = repo.list_proposals(task)[0]
    assert proposal.HasField("agent_provenance") and proposal.HasField("structured_patch")
    before = list(runner.events), len(calls)
    assert coordinator.run(task) == report
    assert before == (runner.events, len(repo.list_agent_rpc_calls(task)))


def test_failed_automated_verification_cannot_be_overridden_by_remote_reviews(tmp_path, agents):
    _, settings, _ = agents
    repo, runner, task, coordinator, _ = pipeline(tmp_path, settings)
    runner.mode = "candidate-ub"
    report = coordinator.run(task)
    assert not report["candidate_selected"] and report["benchmark"] is None
    assert report["reviews"]["correctness"]["verdict"] == "reject"
    assert not any(c["role"] == pb.BENCHMARK for c in repo.list_agent_rpc_calls(task))


def test_stopped_generator_consumes_one_durable_attempt_without_execution(tmp_path, agents):
    _, settings, endpoints = agents
    endpoints = {**endpoints, "performance": f"127.0.0.1:{free_port()}"}
    settings = replace(settings, agent_endpoints=json.dumps(endpoints), agent_timeout_seconds=0.1)
    repo, runner, task, coordinator, _ = pipeline(tmp_path, settings)
    before = list(runner.events)
    with pytest.raises(AgentUnavailable):
        coordinator.run(task)
    assert before == runner.events and not repo.list_proposals(task)
    assert repo.list_agent_iterations(task)[0]["status"] == "failed"
    assert repo.list_agent_rpc_calls(task)[0]["status"] == "failed"
    with pytest.raises(AgentUnavailable):
        coordinator.run(task)
    with pytest.raises(StateError, match="iteration limit"):
        coordinator.run(task)
    assert len(repo.list_agent_rpc_calls(task)) == 2


def test_stopped_reviewer_cannot_reach_measurement(tmp_path, agents):
    _, settings, endpoints = agents
    settings = replace(
        settings,
        agent_endpoints=json.dumps({**endpoints, "correctness": f"127.0.0.1:{free_port()}"}),
        agent_timeout_seconds=0.1,
    )
    repo, _, task, coordinator, _ = pipeline(tmp_path, settings)
    with pytest.raises(AgentUnavailable):
        coordinator.run(task)
    assert not repo.list_benchmarks(task) and repo.get_decision(task) is None
    reviews = [r for r in repo.list_agent_iterations(task) if r["role"] == pb.CORRECTNESS]
    assert [r["status"] for r in reviews] == ["failed"]


def test_service_rejects_unauthorized_artifact_uploads(agents):
    _, _, endpoints = agents
    with grpc.insecure_channel(endpoints["performance"]) as channel:
        with pytest.raises(grpc.RpcError) as caught:
            rpc.AgentServiceStub(channel).PutArtifact(iter(()), timeout=1)
    assert caught.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_generation_response_is_identical_across_rpc_redelivery_and_service_restart(
    tmp_path, agents
):
    _, settings, endpoints = agents
    repo, _, task, coordinator, _ = pipeline(tmp_path, settings)
    coordinator.run(task)
    request = repo.list_requests(task)[0]
    proposal = repo.list_proposals(task)[0]
    with grpc.insecure_channel(endpoints["performance"]) as channel:
        stub = rpc.AgentServiceStub(channel)
        result = stub.GenerateProposal(
            request, timeout=2, metadata=(("authorization", "Bearer " + TOKEN),)
        )
        assert encode_message(result) == encode_message(proposal)
        changed = pb.OptimizationRequest()
        changed.CopyFrom(request)
        changed.meta.message_id = str(uuid4())
        with pytest.raises(grpc.RpcError) as caught:
            stub.GenerateProposal(
                changed, timeout=2, metadata=(("authorization", "Bearer " + TOKEN),)
            )
        assert caught.value.code() == grpc.StatusCode.ALREADY_EXISTS
    endpoints.restart("performance")
    with grpc.insecure_channel(endpoints["performance"]) as channel:
        restored = rpc.AgentServiceStub(channel).GenerateProposal(
            request, timeout=2, metadata=(("authorization", "Bearer " + TOKEN),)
        )
    assert encode_message(restored) == encode_message(proposal)


@pytest.fixture
def local_agent(tmp_path):
    handles = []

    def start(role, backend, service_type=AgentService):
        service = service_type(tmp_path / ("service-" + str(len(handles))), role, backend=backend)
        executor = ThreadPoolExecutor(max_workers=2)
        server = grpc.server(executor, interceptors=(BearerAuth(TOKEN),))
        rpc.add_AgentServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        handles.append((server, executor, service))
        return f"127.0.0.1:{port}", service

    yield start
    for server, executor, service in handles:
        server.stop(0).wait()
        executor.shutdown(wait=True)
        service.close()


def test_slow_agent_deadline_consumes_attempt_and_never_commits_late_output(
    tmp_path, agents, local_agent
):
    from compiler_optimizer.agents.performance import ExamplePerformanceBackend

    class Slow:
        def __init__(self):
            self.finished = threading.Event()

        def generate(self, request, sources):
            time.sleep(0.2)
            self.finished.set()
            return ExamplePerformanceBackend().generate(request, sources)

    backend = Slow()
    endpoint, service = local_agent(pb.PERFORMANCE, backend)
    _, settings, endpoints = agents
    settings = replace(
        settings,
        agent_timeout_seconds=0.1,
        agent_endpoints=json.dumps({**endpoints, "performance": endpoint}),
    )
    repo, runner, task, coordinator, _ = pipeline(tmp_path / "coordinator", settings)
    before = list(runner.events)
    with pytest.raises(AgentUnavailable, match="DEADLINE_EXCEEDED"):
        coordinator.run(task)
    assert backend.finished.wait(2)
    limit = time.monotonic() + 2
    while time.monotonic() < limit:
        with service.store.transaction() as db:
            row = db.execute("SELECT state,response FROM operations").fetchone()
        if row["state"] == "failed":
            break
        time.sleep(0.01)
    assert row["state"] == "failed" and row["response"] is None
    assert not repo.list_proposals(task) and not repo.list_benchmarks(task)
    assert before == runner.events
    assert repo.list_agent_iterations(task)[0]["status"] == "failed"


def test_remote_replay_patch_preserves_provenance_and_validation(tmp_path, agents):
    from compiler_optimizer.agents.performance import ExamplePerformanceBackend, FilePatchBackend
    from compiler_optimizer.workers.baseline import metadata

    _, settings, _ = agents
    repo, _, task, coordinator, _ = pipeline(tmp_path, settings)
    submission = repo.get_task(task)["submission"]
    request = pb.OptimizationRequest(
        meta=metadata(task),
        proposal_id=str(uuid4()),
        role=pb.PERFORMANCE,
        source=submission.source,
        manifest=submission.manifest,
        compiler=submission.compiler,
    )
    patch = ExamplePerformanceBackend().generate(
        request, {"main.c": (Path(__file__).parents[1] / "examples/sum-array/main.c").read_bytes()}
    )
    path = tmp_path / "operator-patch.json"
    path.write_bytes(patch)
    report = coordinator.run(task, FilePatchBackend(path))
    assert report["verification"] == "passed"
    assert repo.list_proposals(task)[0].agent_provenance.backend == "replay-v1"


def test_service_rechecks_correctness_evidence_and_rejects_forged_bindings(tmp_path, agents):
    _, settings, endpoints = agents
    repo, _, task, coordinator, client = pipeline(tmp_path, settings)
    coordinator.run(task)
    proposal = repo.list_proposals(task)[0]
    result = next(r for r in repo.list_results(task) if r.WhichOneof("subject") == "proposal_id")
    request, _ = client._assessment(pb.CORRECTNESS, proposal, result)
    # A fresh operation identifier cannot rebind a stored correctness report to another job.
    request.job.job_id = request.verification.job_id = str(uuid4())
    with grpc.insecure_channel(endpoints["correctness"]) as channel:
        with pytest.raises(grpc.RpcError) as caught:
            rpc.AgentServiceStub(channel).Review(
                request, timeout=2, metadata=(("authorization", "Bearer " + TOKEN),)
            )
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_client_rejects_forged_benchmark_plan_with_matching_transport_hash(
    tmp_path, agents, local_agent
):
    class BadPlanner(AgentService):
        def PlanBenchmark(self, request, context):
            response = super().PlanBenchmark(request, context)
            record = json.loads(response.record_json)
            record["measured_pairs_per_case"] = 1
            response.record_json = canonical(record).decode()
            response.provenance.response_sha256 = sha(payload(response))
            return response

    # Replace only planning: even a valid service provenance hash cannot change the frozen work.
    _, settings, endpoints = agents
    service = BadPlanner(tmp_path / "bad-planner", pb.BENCHMARK)
    executor = ThreadPoolExecutor(max_workers=2)
    server = grpc.server(executor)
    rpc.add_AgentServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        settings = replace(
            settings, agent_endpoints=json.dumps({**endpoints, "benchmark": f"127.0.0.1:{port}"})
        )
        repo, _, task, coordinator, _ = pipeline(tmp_path / "coordinator", settings)
        with pytest.raises(ContractError, match="frozen policy"):
            coordinator.run(task)
        assert not repo.list_benchmarks(task) and repo.get_decision(task) is None
    finally:
        server.stop(0).wait()
        executor.shutdown(wait=True)
        service.close()


@pytest.mark.parametrize(
    "environment",
    [
        {"AGENT_BACKEND": "unknown"},
        {"AGENT_TIMEOUT_SECONDS": "nan"},
        {"AGENT_TIMEOUT_SECONDS": "121"},
        {"AGENT_TLS": "yes"},
        {"AGENT_ENDPOINTS": "{}"},
        {"GRPC_HOST": "0.0.0.0"},
    ],
)
def test_agent_configuration_rejects_invalid_settings(environment):
    with pytest.raises(ValueError):
        Settings.from_env("performance", environment)


def test_concurrent_cohort_uses_rpc_clients_without_duplicate_reviews(tmp_path, agents):
    from test_concurrency import concurrent_setup

    from compiler_optimizer.coordinator.round import CohortCoordinator

    repo, runner, _, task, broker, recovery, _, _ = concurrent_setup(tmp_path)
    _, settings, _ = agents
    coordinator = CohortCoordinator(
        repo, runner, broker, recovery=recovery, agent_client=AgentClient(repo, settings)
    )
    audit = coordinator.run(task, agents=["performance", "architecture"])
    assert audit["status"] == "selected", audit
    assert len(repo.list_benchmarks(task)) == 2
    calls = repo.list_agent_rpc_calls(task)
    assert len(calls) == 8 and all(c["status"] == "completed" for c in calls)
    assert coordinator.run(task) == audit
    assert len(repo.list_agent_rpc_calls(task)) == 8


def test_even_authenticated_agent_cannot_approve_failed_tests(tmp_path, agents, local_agent):
    class DishonestReviewer(AgentService):
        def Review(self, request, context):
            response = super().Review(request, context)
            record = json.loads(response.record_json)
            record.update(verdict="approve", findings=[])
            response.record_json = canonical(record).decode()
            response.provenance.response_sha256 = sha(payload(response))
            return response

    endpoint, _ = local_agent(pb.CORRECTNESS, None, service_type=DishonestReviewer)
    _, settings, endpoints = agents
    settings = replace(settings, agent_endpoints=json.dumps({**endpoints, "correctness": endpoint}))
    repo, runner, task, coordinator, _ = pipeline(tmp_path / "coordinator", settings)
    runner.mode = "candidate-ub"
    with pytest.raises(ContractError, match="failed automated tests"):
        coordinator.run(task)
    assert repo.get_decision(task) is None and not repo.list_benchmarks(task)


@pytest.mark.parametrize("available", [True, False])
def test_submission_dispatcher_uses_frozen_rpc_mode_and_preserves_acceptance(
    tmp_path, agents, available
):
    from test_selection import CohortBroker, MeasuredRunner

    from compiler_optimizer.runtime.dispatch import SubmissionDispatcher
    from compiler_optimizer.shared.workflow import default_workflow
    from compiler_optimizer.storage import Repository
    from compiler_optimizer.workers.baseline import metadata
    from compiler_optimizer.workers.package import read_package

    repo = Repository(tmp_path / "coordinator")
    package = read_package(Path(__file__).parents[1] / "examples/sum-array")
    runner = MeasuredRunner(package)
    task = str(uuid4())
    submission = pb.Submission(
        meta=metadata(task),
        manifest=package.manifest,
        package=repo.artifacts.put(package.package_bundle),
        compiler=runner.compiler("c17"),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(package.source_bundle)),
    )
    repo.save_submission(
        submission,
        execution={**default_workflow(), "agents": ["performance"], "agent_backend": "rpc"},
    )
    _, settings, endpoints = agents
    if not available:
        settings = replace(
            settings,
            agent_timeout_seconds=0.1,
            agent_endpoints=json.dumps({**endpoints, "performance": f"127.0.0.1:{free_port()}"}),
        )
    owner = SubmissionDispatcher(
        repo,
        settings,
        runner_factory=lambda _: runner,
        broker_factory=lambda: CohortBroker(repo),
        recovery_factory=lambda *_: None,
    )
    assert owner.run_once()
    assert repo.get_task(task)["status"] == "completed"
    calls = repo.list_agent_rpc_calls(task)
    assert calls
    if available:
        assert len(calls) == 4 and all(c["status"] == "completed" for c in calls)
        assert repo.selection_report(task)[1]["status"] == "selected"
    else:
        assert [c["status"] for c in calls] == ["failed"]
        assert not repo.list_benchmarks(task)
        assert repo.selection_report(task)[1]["status"] == "retained_baseline"
