import json
import os

import pytest

from compiler_optimizer.shared.kernels import Kernel
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.storage.artifacts import ArtifactStore
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.mlir import run_kernel


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "arbitrary-c", "size": 8},
        {"kind": "add", "size": 0},
        {"kind": "scale", "size": 4097},
        {"kind": "scale", "size": True},
        {"kind": "scale", "size": 8, "factor": 100},
        {"kind": "add", "size": 8, "schema_version": 2},
    ],
)
def test_unsupported_contract(fields):
    with pytest.raises(ContractError):
        Kernel(**fields)


def test_independent_oracles_and_typed_ir():
    assert Kernel("add", 3).expected() == [-63, -61, -59]
    assert Kernel("scale", 3, -2).expected() == [96, 94, 92]
    assert b"arith.addi" in Kernel("add", 3).mlir()
    assert b"arith.muli" in Kernel("scale", 3).mlir()


@pytest.mark.parametrize("failure", ["timeout", "nonzero_exit", "oom"])
def test_failed_compiler_cannot_produce_success(tmp_path, failure):
    from compiler_optimizer.shared.v1 import contracts_pb2 as pb
    from compiler_optimizer.workers.docker_runner import InfrastructureError, Invocation

    class FailedRunner:
        native_execution = True
        target = "linux-aarch64"

        def compiler(self, language):
            return pb.CompilerSettings()

        def mlir_stage(self, *args):
            return Invocation(b"", b"failed", 0.01, 0, -1, failure=failure)

    with pytest.raises(InfrastructureError, match="stage failed: tile"):
        run_kernel(Kernel("add", 17), FailedRunner(), ArtifactStore(tmp_path / "artifacts"))


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("MLIR_INTEGRATION_TESTS") != "1", reason="requires MLIR 18 image"
)
@pytest.mark.parametrize("kind,size", [("add", 17), ("scale", 31), ("add", 1), ("scale", 4096)])
def test_real_tiling_native_execution_and_oracle(tmp_path, kind, size):
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-mlir-test:18"),
        target=os.environ.get("WORKER_TARGET", "linux-aarch64"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    store = ArtifactStore(tmp_path / "artifacts")
    report = json.loads(store.get(run_kernel(Kernel(kind, size), runner, store)))
    assert report["passed"]
    from compiler_optimizer.shared.v1 import contracts_pb2 as pb

    original = store.get(pb.ArtifactRef(**report["original"]))
    tiled = store.get(pb.ArtifactRef(**report["stages"][0]["output"]))
    assert tiled.count(b"affine.for") > original.count(b"affine.for")
