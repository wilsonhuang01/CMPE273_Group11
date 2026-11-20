"""Actual multi-candidate RabbitMQ/Docker cohort and coordinator result retrieval."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc
import pytest

from compiler_optimizer.coordinator.round import CohortCoordinator
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
        reason="requires Docker and local RabbitMQ",
    ),
]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_real_cohort_collects_both_candidates_and_retains_original_with_rpc_result(tmp_path):
    repo = Repository(tmp_path / "state")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    coordinator = CohortCoordinator(
        repo, runner, RabbitProposalQueue(Settings.from_env("coordinator"))
    )
    audit = coordinator.optimize(
        read_package(EXAMPLES / "sum-array"),
        agents=["performance", "architecture"],
        verification_plan=VerificationPlan(generator=ArrayGenerator(273, 1, 1, 0, 8, -100, 100)),
        benchmark_plan=BenchmarkPlan(warmups=0, repetitions=7, minimum_sample_seconds=1.0),
    )
    assert audit["status"] == "retained_baseline", audit
    assert len(audit["candidates"]) == 2
    assert len(repo.list_benchmarks(audit["task_id"])) == 2
    assert all(
        c["votes"] == {"correctness": "approve", "critic": "approve"} for c in audit["candidates"]
    )
    assert (
        audit["selected_source"]["sha256"]
        == repo.get_task(audit["task_id"])["submission"].source.bundle.sha256
    )
    assert coordinator.run(audit["task_id"]) == audit
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        rpc.add_CoordinatorServiceServicer_to_server(CoordinatorService(repo), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                result = rpc.CoordinatorServiceStub(channel).GetOptimizationResult(
                    pb.OptimizationQuery(task_id=audit["task_id"]), timeout=5
                )
                assert result.status == "completed" and result.stage == "completed"
                assert result.selected_source.version == 1
                assert json.loads(result.report_json) == audit
        finally:
            server.stop(0).wait()
