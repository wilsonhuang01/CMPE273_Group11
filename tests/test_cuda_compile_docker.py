"""Actual NVCC PTX generation on a CPU-only Docker worker; no NVIDIA device is requested."""

import os
from pathlib import Path

import pytest

from compiler_optimizer.storage.artifacts import ArtifactStore
from compiler_optimizer.workers.cuda_compile import CUDACompileRunner, compile_artifacts

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("CUDA_COMPILE_TESTS") != "1",
        reason="build compiler-cuda and set CUDA_COMPILE_TESTS=1",
    ),
]


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    return CUDACompileRunner(watchdog_directory=tmp_path_factory.mktemp("cuda-compile-watchdogs"))


def test_real_cuda_compiles_to_ptx_without_gpu_execution(tmp_path, runner):
    code = (Path(__file__).resolve().parents[1] / "examples/cuda/vector_add.cu").read_bytes()
    invocation = runner.compile_ptx(code)
    assert invocation.failure is None, invocation.log()
    assert b".target sm_75" in invocation.binary and b".entry vector_add" in invocation.binary
    assert invocation.sandbox["watchdog"]["state"] == "removed"
    assert "--ptx" in invocation.command and "--gpu-architecture=compute_75" in invocation.command


def test_real_cuda_syntax_error_is_rejected_and_unqualified(tmp_path, runner):
    report = compile_artifacts(
        runner, b"__global__ void bad( { this is invalid; }", ArtifactStore(tmp_path / "artifacts")
    )
    assert report["status"] == "rejected" and report["ptx"] is None
    assert not report["correctness_verified"] and not report["execution_enabled"]
    assert not report["performance_qualified"]
