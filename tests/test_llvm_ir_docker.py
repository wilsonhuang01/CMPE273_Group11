"""Real LLVM transformation, native execution, and existing cohort evidence gates."""

import json
import os
from pathlib import Path

import pytest

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.llvm_ir import IRPassBackend, validate_artifacts
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("LLVM_INTEGRATION_TESTS") != "1", reason="requires native LLVM 18 sandbox"
    ),
]


@pytest.fixture
def runner(tmp_path):
    target = os.environ.get("WORKER_TARGET", "linux-x86_64")
    return DockerRunner(
        os.environ.get("COMPILER_IMAGE", DEFAULT_IMAGES[target]),
        target=target,
        watchdog_directory=tmp_path / "watchdogs",
    )


def package():
    return read_package(Path(__file__).parents[1] / "examples/cpu-transformations/unsigned-scale")


class Broker:
    def exchange(self, proposal, consume, published):
        published()
        return consume(proposal)


def test_real_ir_enters_native_verification_reviews_benchmarks_and_selection(tmp_path, runner):
    repo = Repository(tmp_path / "state")
    coordinator = CohortCoordinator(
        repo, runner, Broker(), concurrency=1, backends={"performance": IRPassBackend("scalar-v1")}
    )
    report = coordinator.optimize(
        package(),
        agents=["performance"],
        benchmark_plan=BenchmarkPlan(warmups=0, repetitions=7, minimum_sample_seconds=1.0),
    )
    task = coordinator.last_task_id
    assert report["status"] == "retained_baseline", report
    (proposal,) = repo.list_proposals(task)
    validate_artifacts(repo.artifacts, proposal)
    assert proposal.HasField("ir")
    assert len(repo.list_benchmarks(task)) == 1
    assert all(r.WhichOneof("outcome") == "success" for r in repo.list_results(task))
    result = repo.list_results(task)[-1]
    evidence = json.loads(repo.artifacts.get(result.correctness_report))
    assert evidence["ir"]["pipeline_id"] == "scalar-v1"
    builds = {c["phase"]: c["binary_sha256"] for c in evidence["checks"] if "binary_sha256" in c}
    assert set(builds) == {
        "baseline_compile",
        "candidate_compile",
        "baseline_sanitizer_compile",
        "candidate_sanitizer_compile",
    }
    # Completed cohort resumes without regenerating or resampling.
    assert coordinator.run(task) == report


def test_malformed_ir_and_compiler_timeout_fail_closed(runner):
    p = package()
    invalid = runner.llvm_stage(p, "verify", pipeline_id="scalar-v1", modules=[b"not LLVM bitcode"])
    assert invalid.failure == "nonzero_exit"
    assert invalid.binary is None
    # Exercise the actual compiler process-group deadline, not a mocked invocation.
    limits = type(p.manifest.limits)()
    limits.CopyFrom(p.manifest.limits)
    p.manifest.limits.compile_wall_seconds = 1
    payload = {
        "mode": "llvm",
        "stage": "emit",
        "target": runner.target,
        "build_profile": "normal",
        "language": p.manifest.language,
        "pipeline_id": "scalar-v1",
        "llvm_version": runner.version,
        "source": "main.c",
        "files": {"main.c": __import__("base64").b64encode(b"int main(void){return 0;}").decode()},
        "modules": [],
        "wall_seconds": 0.000001,
        "output_bytes": 4096,
    }
    timed = runner._invoke(payload, memory=limits.compile_memory_mib, compile_mode=True)
    assert timed.failure == "timeout"
    assert timed.binary is None


def test_cpp_multiple_translation_units_and_sroa_pipeline(tmp_path, runner):
    from uuid import uuid4

    from compiler_optimizer.shared.v1 import contracts_pb2 as pb
    from compiler_optimizer.workers.baseline import metadata, run_baseline
    from compiler_optimizer.workers.llvm_ir import IRPassAgent
    from compiler_optimizer.workers.package import package_from_files
    from compiler_optimizer.workers.verify import run_verification

    original = read_package(
        Path(__file__).parents[1] / "examples/cpu-transformations/buffered-total-cpp"
    )
    files = dict(original.files)
    files["main.cpp"] = (
        files["main.cpp"]
        .replace(b"int main(void)", b"long long identity(long long);\nint main(void)")
        .replace(b"total += samples[i]", b"total += identity(samples[i])")
    )
    files["helper.cpp"] = (
        b"long long identity(long long value) { long long local = value; return local + 0; }\n"
    )
    manifest = json.loads(files["submission.json"])
    manifest["sources"].append("helper.cpp")
    files["submission.json"] = json.dumps(manifest).encode()
    p = package_from_files(files)
    repo = Repository(tmp_path / "cpp-state")
    baseline = run_baseline(p, repo, runner)
    assert baseline.WhichOneof("outcome") == "success"
    submission = repo.get_task(baseline.meta.task_id)["submission"]
    request = pb.OptimizationRequest(
        meta=metadata(baseline.meta.task_id),
        proposal_id=str(uuid4()),
        role=pb.PERFORMANCE,
        manifest=submission.manifest,
        source=submission.source,
        compiler=submission.compiler,
    )
    repo.save_request(request)
    from compiler_optimizer.workers.verification_plan import VerificationPlan

    sanitizer = "address-undefined" if runner.native_amd64 else "undefined"
    proposal, _ = IRPassAgent(repo, runner, "sroa-v1", sanitizer).generate(request)
    repo.save_proposal(proposal)
    result = run_verification(
        repo,
        baseline.meta.task_id,
        proposal.proposal_id,
        runner,
        VerificationPlan(sanitizer=sanitizer),
    )
    assert result.WhichOneof("outcome") == "success", result
    for variant in proposal.ir.variants:
        report = json.loads(repo.artifacts.get(variant.stage_report))
        assert [r["stage"] for r in report["stages"]].count("emit") == 2
