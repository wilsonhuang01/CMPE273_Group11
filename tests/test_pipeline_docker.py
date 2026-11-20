"""First complete milestone against actual RabbitMQ, Clang, sandboxes, and coordinator RPC."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc
import pytest

from compiler_optimizer.coordinator.pipeline import PipelineCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.verification_plan import ArrayGenerator, VerificationPlan

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("PIPELINE_INTEGRATION_TESTS") != "1",
        reason="set PIPELINE_INTEGRATION_TESTS=1 with Docker and local RabbitMQ running",
    ),
]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    return DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path_factory.mktemp("pipeline-watchdogs"),
    )


def coordinator(tmp_path, runner):
    repository = Repository(tmp_path / "state")
    transport = RabbitProposalQueue(Settings.from_env("coordinator"))
    return repository, PipelineCoordinator(repository, runner, transport)


def test_real_agent_broker_verification_benchmark_and_coordinator_result(tmp_path, runner):
    repository, pipeline = coordinator(tmp_path, runner)
    report = pipeline.optimize(
        read_package(EXAMPLES / "sum-array"),
        verification_plan=VerificationPlan(generator=ArrayGenerator(273, 1, 1, 0, 8, -100, 100)),
        benchmark_plan=BenchmarkPlan(warmups=1, repetitions=7, minimum_sample_seconds=1.0),
    )
    assert report["status"] == "completed", report
    assert report["verification"] == "passed", report["failure"]
    assert report["benchmark"] is not None
    assert not report["candidate_selected"]  # Tiny workload and/or emulation cannot qualify.
    task = report["task_id"]
    assert repository.get_pipeline(task)["phase"] == "completed"
    assert repository.get_pipeline(task)["published"] == 1
    counts = len(repository.list_jobs(task)), len(repository.list_benchmarks(task))
    assert pipeline.run(task) == report
    assert counts == (len(repository.list_jobs(task)), len(repository.list_benchmarks(task)))
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        rpc.add_CoordinatorServiceServicer_to_server(CoordinatorService(repository), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                result = rpc.CoordinatorServiceStub(channel).GetOptimizationResult(
                    pb.OptimizationQuery(task_id=task), timeout=5
                )
                assert json.loads(result.report_json) == report
                assert result.selected_source.version == 1
                assert result.report.sha256
        finally:
            server.stop(0).wait()


class BadPatch:
    def __init__(self, mode):
        self.mode = mode

    def generate(self, request, sources):
        old, new = (
            ('printf("%lld\\n", sum);', 'printf("%lld\\n", sum + 1);')
            if self.mode == "wrong"
            else ("int main(void) {", "int main(void) { for (;;) {}")
        )
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "rationale": "Deliberately unsafe integration-test candidate",
                "transformation": "test-" + self.mode,
                "edits": [{"path": "main.c", "old": old, "new": new}],
            }
        ).encode()


@pytest.mark.parametrize("mode,reason", [("wrong", "OUTPUT_MISMATCH"), ("hang", "TIMEOUT")])
def test_real_bad_and_hanging_agent_proposals_return_baseline(tmp_path, runner, mode, reason):
    repository, pipeline = coordinator(tmp_path, runner)
    report = pipeline.optimize(read_package(EXAMPLES / "sum-array"), BadPatch(mode))
    assert report["status"] == "completed", report
    assert report["verification"] == "rejected"
    assert report["failure"]["reason"] == reason
    assert not report["candidate_selected"]
    assert report["benchmark"] is None
    assert not repository.list_benchmarks(report["task_id"])
    assert repository.pipeline_report(report["task_id"])[1] == report
