"""Native compiler/sanitizer evidence for the supported C/C++ transformation subset."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from compiler_optimizer.agents.architecture import ArchitectureAgent
from compiler_optimizer.agents.memory import MemoryAgent
from compiler_optimizer.agents.performance import PerformanceAgent
from compiler_optimizer.agents.rules import (
    ArchitectureRuleBackend,
    MemoryRuleBackend,
    PerformanceRuleBackend,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import metadata, run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES
from compiler_optimizer.workers.verify import run_verification

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("TRANSFORMATION_INTEGRATION_TESTS") != "1",
        reason="requires a native CPU compiler image",
    ),
]

CASES = [
    ("bounded-echo", PerformanceAgent, PerformanceRuleBackend),
    ("buffered-total", MemoryAgent, MemoryRuleBackend),
    ("buffered-total-cpp", MemoryAgent, MemoryRuleBackend),
    ("unsigned-scale", PerformanceAgent, PerformanceRuleBackend),
    ("unsigned-scale", ArchitectureAgent, ArchitectureRuleBackend),
    ("unsigned-xor", ArchitectureAgent, ArchitectureRuleBackend),
]


@pytest.mark.parametrize("name,agent_type,backend_type", CASES)
def test_native_cpu_candidate_preserves_fixed_tests_and_sanitizers(
    tmp_path, name, agent_type, backend_type
):
    target = os.environ.get("WORKER_TARGET", "linux-x86_64")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", DEFAULT_IMAGES[target]),
        target=target,
        watchdog_directory=tmp_path / "watchdogs",
    )
    repo = Repository(tmp_path / "state")
    package = read_package(Path(__file__).parents[1] / "examples/cpu-transformations" / name)
    baseline = run_baseline(package, repo, runner)
    assert baseline.WhichOneof("outcome") == "success", baseline
    submission = repo.get_task(baseline.meta.task_id)["submission"]
    request = pb.OptimizationRequest(
        meta=metadata(baseline.meta.task_id),
        proposal_id=str(uuid4()),
        role=backend_type.role,
        manifest=submission.manifest,
        source=submission.source,
        compiler=submission.compiler,
    )
    repo.save_request(request)
    proposal, patch = agent_type(repo.artifacts, backend_type()).generate(request)
    repo.save_proposal(proposal)
    result = run_verification(repo, baseline.meta.task_id, proposal.proposal_id, runner)
    assert result.WhichOneof("outcome") == "success", result
    assert proposal.compiler == submission.compiler
    assert repo.artifacts.get(patch)


def test_new_native_cohort_retains_baseline_when_measurements_do_not_qualify(tmp_path):
    from compiler_optimizer.coordinator.round import CohortCoordinator
    from compiler_optimizer.shared.benchmarking import BenchmarkPlan

    class Broker:
        def exchange(self, proposal, consume, published):
            published()
            return consume(proposal)

    target = os.environ.get("WORKER_TARGET", "linux-x86_64")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", DEFAULT_IMAGES[target]),
        target=target,
        watchdog_directory=tmp_path / "watchdogs",
    )
    repo = Repository(tmp_path / "state")
    coordinator = CohortCoordinator(repo, runner, Broker(), concurrency=1)
    report = coordinator.optimize(
        read_package(Path(__file__).parents[1] / "examples/cpu-transformations/unsigned-scale"),
        benchmark_plan=BenchmarkPlan(warmups=0, repetitions=7, minimum_sample_seconds=1.0),
    )
    task = coordinator.last_task_id
    assert report["status"] == "retained_baseline", report
    assert len(repo.list_proposals(task)) == 2  # Memory abstains on pointwise kernels.
    assert len(repo.list_benchmarks(task)) == 2
    assert all(r.WhichOneof("outcome") == "success" for r in repo.list_results(task))
    before = len(repo.list_jobs(task)), len(repo.list_benchmarks(task))
    assert coordinator.run(task) == report
    assert before == (len(repo.list_jobs(task)), len(repo.list_benchmarks(task)))
