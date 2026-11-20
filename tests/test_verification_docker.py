"""Real candidate differential, sanitizer, and valid-input fuzz checks in Docker."""

import json
import os
import shutil
from pathlib import Path

import pytest

from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.verification_plan import ArrayGenerator, VerificationPlan
from compiler_optimizer.workers.verify import register_candidate, run_verification

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("BASELINE_DOCKER_TESTS") != "1",
        reason="requires opt-in real Docker verification",
    ),
]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    return DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path_factory.mktemp("verification-watchdogs"),
    )


def prepare(tmp_path, runner, source):
    repo = Repository(tmp_path / "state")
    baseline = run_baseline(read_package(EXAMPLES / "sum-array"), repo, runner)
    assert baseline.WhichOneof("outcome") == "success", baseline.failure
    directory = tmp_path / "candidate"
    directory.mkdir()
    (directory / "main.c").write_bytes(source)
    proposal = register_candidate(repo, baseline.meta.task_id, directory)
    return repo, baseline.meta.task_id, proposal


def test_real_optimized_proposal_passes_differential_sanitizer_and_generated_cases(
    tmp_path, runner
):
    repo, task, proposal = prepare(
        tmp_path, runner, (EXAMPLES / "sum-array-optimized/main.c").read_bytes()
    )
    plan = VerificationPlan(generator=ArrayGenerator(273, 1, 2, 0, 8, -100, 100))
    result = run_verification(repo, task, proposal, runner, plan)
    assert result.WhichOneof("outcome") == "success", result.failure
    assert len(result.success.passed_test_ids) == 5
    assert repo.get_task(task)["status"] == "running"


def test_real_generated_input_exposes_candidate_that_passes_fixed_fixtures(tmp_path, runner):
    source = (
        (EXAMPLES / "sum-array/main.c")
        .read_bytes()
        .replace(b'printf("%lld\\n", sum);', b'if (count > 6) sum++; printf("%lld\\n", sum);')
    )
    assert b"count > 6" in source
    repo, task, proposal = prepare(tmp_path, runner, source)
    plan = VerificationPlan(generator=ArrayGenerator(273, 0, 2, 0, 8, -100, 100))
    result = run_verification(repo, task, proposal, runner, plan)
    assert result.failure.reason == pb.OUTPUT_MISMATCH, result.failure
    assert result.failure.test_id == "fuzz-0001"
    assert repo.get_task(task)["status"] == "running"


def test_real_undefined_behavior_disqualifies_otherwise_correct_candidate(tmp_path, runner):
    source = (
        (EXAMPLES / "sum-array/main.c")
        .read_bytes()
        .replace(
            b"int main(void) {",
            b"int main(void) { volatile int maximum=2147483647; "
            b"volatile int overflow=maximum+1; (void)overflow;",
        )
    )
    repo, task, proposal = prepare(tmp_path, runner, source)
    result = run_verification(repo, task, proposal, runner)
    assert result.failure.reason == pb.SANITIZER_FAILURE, result.failure
    assert repo.get_task(task)["status"] == "running"


def test_real_address_sanitizer_profile_on_native_worker(tmp_path, runner):
    if not runner.native_amd64:
        pytest.skip("AddressSanitizer profile requires a native amd64 Docker worker")
    repo, task, proposal = prepare(
        tmp_path, runner, (EXAMPLES / "sum-array-optimized/main.c").read_bytes()
    )
    result = run_verification(
        repo, task, proposal, runner, VerificationPlan(sanitizer="address-undefined")
    )
    assert result.WhichOneof("outcome") == "success", result.failure


def test_real_cpp17_candidate_with_ubsan(tmp_path, runner):
    directory = Path(shutil.copytree(EXAMPLES / "sum-array", tmp_path / "original"))
    manifest = json.loads((directory / "submission.json").read_text())
    manifest.update(language="cpp17", sources=["main.cpp"])
    (directory / "submission.json").write_text(json.dumps(manifest))
    (directory / "main.c").unlink()
    code = (
        b"#include <iostream>\nint main() { int n,x; long long total=0; std::cin>>n; "
        b"while(n--){std::cin>>x; total+=x;} std::cout<<total<<std::endl; }\n"
    )
    (directory / "main.cpp").write_bytes(code)
    repo = Repository(tmp_path / "state")
    baseline = run_baseline(read_package(directory), repo, runner)
    assert baseline.WhichOneof("outcome") == "success", baseline.failure
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "main.cpp").write_bytes(code + b"\n// equivalent candidate\n")
    proposal = register_candidate(repo, baseline.meta.task_id, candidate)
    result = run_verification(repo, baseline.meta.task_id, proposal, runner)
    assert result.WhichOneof("outcome") == "success", result.failure
