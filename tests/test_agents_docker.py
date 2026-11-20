"""Real specialized-agent paths through RabbitMQ, verification, measurements and results."""

import os
from pathlib import Path

import pytest

from compiler_optimizer.coordinator.pipeline import PipelineCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
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


def test_real_memory_agent_streams_input_and_retains_unqualified_baseline(tmp_path):
    repository = Repository(tmp_path / "state")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    coordinator = PipelineCoordinator(
        repository, runner, RabbitProposalQueue(Settings.from_env("coordinator"))
    )
    report = coordinator.optimize(
        read_package(EXAMPLES / "sum-array-buffered"),
        agent="memory",
        verification_plan=VerificationPlan(generator=ArrayGenerator(273, 1, 1, 0, 8, -100, 100)),
        benchmark_plan=BenchmarkPlan(
            schema_version=2,
            objective="memory",
            warmups=0,
            repetitions=7,
            minimum_sample_seconds=1.0,
        ),
    )
    assert report["agent"] == "memory"
    assert report["verification"] == "passed", report["failure"]
    assert not report["candidate_selected"]
    assert report["benchmark"]["analysis"]["objective"] == "memory"
    assert repository.pipeline_report(report["task_id"])[1] == report


def test_real_architecture_agent_uses_generic_target_and_generated_checks(tmp_path):
    repository = Repository(tmp_path / "state")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    coordinator = PipelineCoordinator(
        repository, runner, RabbitProposalQueue(Settings.from_env("coordinator"))
    )
    report = coordinator.optimize(
        read_package(EXAMPLES / "sum-array"),
        agent="architecture",
        verification_plan=VerificationPlan(generator=ArrayGenerator(273, 1, 2, 0, 8, -100, 100)),
        benchmark_plan=BenchmarkPlan(warmups=0, repetitions=7, minimum_sample_seconds=1.0),
    )
    assert report["verification"] == "passed", report["failure"]
    assert report["agent"] == "architecture" and not report["candidate_selected"]
    proposal = repository.list_proposals(report["task_id"])[0]
    assert proposal.compiler.target == "linux-x86_64"
    assert "-march=x86-64" in proposal.compiler.flags


def test_real_correctness_reviewer_rejects_wrong_output_before_measurements(tmp_path):
    from test_pipeline_docker import BadPatch

    repository = Repository(tmp_path / "state")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    coordinator = PipelineCoordinator(
        repository, runner, RabbitProposalQueue(Settings.from_env("coordinator"))
    )
    report = coordinator.optimize(read_package(EXAMPLES / "sum-array"), BadPatch("wrong"))
    assert report["reviews"]["correctness"]["verdict"] == "reject"
    assert not report["reviews"]["correctness"]["automated_passed"]
    assert report["benchmark"] is None and not report["candidate_selected"]


def test_real_critic_rejects_unreachable_process_call_even_when_tests_pass(tmp_path):
    from test_agents import RiskyPatch

    repository = Repository(tmp_path / "state")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    coordinator = PipelineCoordinator(
        repository, runner, RabbitProposalQueue(Settings.from_env("coordinator"))
    )
    report = coordinator.optimize(read_package(EXAMPLES / "sum-array"), RiskyPatch())
    assert report["verification"] == "passed", report["failure"]
    assert report["reviews"]["critic"]["verdict"] == "reject"
    assert report["benchmark"] is None and not report["candidate_selected"]


def test_real_benchmark_agent_organizes_exact_normal_binary_measurements(tmp_path):
    import json

    from compiler_optimizer.shared.v1 import contracts_pb2 as pb

    repository = Repository(tmp_path / "state")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    coordinator = PipelineCoordinator(
        repository, runner, RabbitProposalQueue(Settings.from_env("coordinator"))
    )
    report = coordinator.optimize(
        read_package(EXAMPLES / "sum-array"),
        benchmark_plan=BenchmarkPlan(warmups=1, repetitions=7, minimum_sample_seconds=1.0),
    )
    work = report["benchmark_work"]
    assert work["maximum_invocations"] == 16
    assert work["binaries"] == report["benchmark"]["binaries"]
    assert work["plan"] == report["benchmark"]["plan"]
    raw = json.loads(repository.artifacts.get(pb.ArtifactRef(**report["benchmark"]["raw"])))
    assert raw["status"] == "completed"
    assert len(raw["cases"]["mixed"]["warmups"]) == 1
    assert len(raw["cases"]["mixed"]["pairs"]) == 7
    assert set(report["reviews"]) == {"correctness", "critic"}
