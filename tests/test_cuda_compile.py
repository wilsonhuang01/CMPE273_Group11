"""Compile-only artifacts never imply CUDA execution, correctness or optimization gains."""

from types import SimpleNamespace

import pytest

from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.storage.artifacts import ArtifactStore
from compiler_optimizer.workers.cuda_compile import CUDACompileRunner, compile_artifacts
from compiler_optimizer.workers.docker_runner import InfrastructureError, Invocation


def test_compile_only_runner_refuses_execution_and_invalid_sources():
    runner = object.__new__(CUDACompileRunner)
    for value in (b"", b"x" * (1024**2 + 1), "source"):
        with pytest.raises(ContractError):
            runner.compile_ptx(value)
    for action in (runner.execute, runner.compile, runner.compiler):
        with pytest.raises(InfrastructureError):
            action("untrusted")


def test_compile_only_output_requires_controlled_ptx_and_is_never_qualified(tmp_path):
    runner = object.__new__(CUDACompileRunner)
    runner._invoke = lambda *a, **kw: Invocation(b"", b"", 0.1, 1024, 0, binary=None)
    with pytest.raises(InfrastructureError):
        runner.compile_ptx(b"invalid fake output")
    runner = SimpleNamespace(
        image="sha256:" + "a" * 64,
        cuda_version="13.0.88",
        compile_ptx=lambda source: Invocation(
            b"", b"", 0.1, 1024, 0, binary=b".version 9.0\n.target sm_75\n"
        ),
    )
    store = ArtifactStore(tmp_path / "artifacts")
    result = compile_artifacts(runner, b"kernel source", store)
    assert result["kind"] == "cuda-ptx-compile-only" and result["status"] == "compiled"
    assert not result["execution_enabled"] and not result["correctness_verified"]
    assert not result["performance_qualified"]
    assert result["source"]["sha256"] == result["source_sha256"]
    assert result["ptx"] and result["log"] and result["report"]
