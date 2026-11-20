"""Real pinned paired measurements; emulation must never qualify performance gains."""

import json
import os
from pathlib import Path

import pytest

from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.benchmark import run_benchmark
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.verify import register_candidate, run_verification

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("BASELINE_DOCKER_TESTS") != "1", reason="requires opt-in real Docker"
    ),
]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_real_paired_benchmark_records_pinning_and_retains_tiny_workload(tmp_path):
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    repo = Repository(tmp_path / "state")
    baseline = run_baseline(read_package(EXAMPLES / "sum-array"), repo, runner)
    assert baseline.WhichOneof("outcome") == "success", baseline.failure
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "main.c").write_bytes((EXAMPLES / "sum-array-optimized/main.c").read_bytes())
    task = baseline.meta.task_id
    proposal = register_candidate(repo, task, candidate)
    verified = run_verification(repo, task, proposal, runner)
    assert verified.WhichOneof("outcome") == "success", verified.failure
    report = run_benchmark(
        repo,
        task,
        verified.job_id,
        runner,
        BenchmarkPlan(warmups=1, repetitions=7, minimum_sample_seconds=1.0),
    )
    raw = json.loads(repo.artifacts.get(pb.ArtifactRef(**report["raw"])))
    assert raw["status"] == "completed", raw.get("error")
    assert not report["analysis"]["qualified"]
    assert repo.get_task(task)["status"] == "running"
    cpu = raw["environment_before"]["cpu_id"]
    for case in raw["cases"].values():
        assert len(case["warmups"]) == 1
        assert len(case["pairs"]) == 7
        for pair in case["pairs"]:
            assert all(
                sample["cpu_id"] == cpu and sample["watchdog_removed"]
                for sample in pair["samples"].values()
            )
    if not runner.native_amd64:
        assert "emulated_worker" in report["analysis"]["reasons"]
