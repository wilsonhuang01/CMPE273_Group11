"""Bounded affine tiling, native lowering and independent kernel output checks."""

import json

from compiler_optimizer.shared.kernels import Kernel
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.docker_runner import InfrastructureError
from compiler_optimizer.workers.llvm_ir import encode
from compiler_optimizer.workers.llvm_policy import TRIPLES
from compiler_optimizer.workers.mlir_policy import PIPELINES
from compiler_optimizer.workers.package import package_from_files


def run_kernel(kernel: Kernel, runner, artifacts):
    """Return a content-addressed report only after every element matches the oracle.

    This dedicated path is not a source proposal or a performance qualification.
    The caller's artifact store retains generated IR, all stages, logs and binary.
    """
    require(isinstance(kernel, Kernel), "a typed kernel is required")
    require(runner.native_execution and runner.target in TRIPLES, "native CPU required")
    compiler = runner.compiler("c17")
    expected = ("\n".join(map(str, kernel.expected())) + "\n").encode()
    manifest = {
        "schema_version": 1,
        "name": "mlir-" + kernel.kind,
        "language": "c17",
        "sources": ["main.c"],
        "tests": [{"id": "oracle", "stdin": "input", "expected_stdout": "expected", "args": []}],
        "benchmark_test_ids": ["oracle"],
        "limits": {
            "compile_wall_seconds": 30,
            "compile_memory_mib": 1024,
            "run_wall_seconds": 2,
            "run_memory_mib": 256,
            "output_kib": 256,
        },
    }
    package = package_from_files(
        {
            "submission.json": json.dumps(manifest).encode(),
            "main.c": kernel.driver(),
            "input": b"",
            "expected": expected,
        }
    )
    stages = []

    def checked(name, inputs, invocation):
        row = {
            "stage": name,
            "inputs": [reference(artifacts.put(x)) for x in inputs],
            "invocation": invocation.log(),
        }
        if invocation.failure or not invocation.binary:
            artifacts.put(encode(row))
            raise InfrastructureError("MLIR kernel stage failed: " + name)
        row["output"] = reference(artifacts.put(invocation.binary))
        stages.append(row)
        return invocation.binary

    original = kernel.mlir()
    tiled = checked("tile", [original], runner.mlir_stage(package, "tile", original))
    require(
        tiled.count(b"affine.for") > original.count(b"affine.for"),
        "tiling produced no additional affine loop",
    )
    lowered = checked("lower", [tiled], runner.mlir_stage(package, "lower", tiled))
    llvm = checked("translate", [lowered], runner.mlir_stage(package, "translate", lowered))
    driver = checked(
        "driver",
        [kernel.driver()],
        runner.llvm_stage(package, "emit", pipeline_id="scalar-v1", source="main.c"),
    )
    linked = checked(
        "link",
        [llvm, driver],
        runner.llvm_stage(package, "link", pipeline_id="scalar-v1", modules=[llvm, driver]),
    )
    checked(
        "verify",
        [linked],
        runner.llvm_stage(package, "verify", pipeline_id="scalar-v1", modules=[linked]),
    )
    binary = checked(
        "native",
        [linked],
        runner.llvm_stage(package, "lower", pipeline_id="scalar-v1", modules=[linked]),
    )
    result = runner.execute(binary, package.manifest.tests[0], package)
    report = {
        "schema_version": 1,
        "recipes": PIPELINES,
        "kernel": vars(kernel),
        "compiler": {
            "image": compiler.image_digest,
            "version": compiler.version,
            "target": compiler.target,
        },
        "original": reference(artifacts.put(original)),
        "stages": stages,
        "execution": result.log(),
        "expected": reference(artifacts.put(expected)),
        "passed": result.failure is None and result.stdout == expected,
    }
    ref = artifacts.put(encode(report))
    require(report["passed"], "MLIR kernel failed independent output oracle")
    return ref


def main():
    import argparse

    from compiler_optimizer.storage.artifacts import ArtifactStore

    parser = argparse.ArgumentParser(
        description="Transform and independently check a typed MLIR kernel"
    )
    parser.add_argument("kind", choices=("add", "scale"))
    parser.add_argument("--size", type=int, default=17)
    parser.add_argument("--factor", type=int, default=3)
    parser.add_argument("--target", choices=tuple(TRIPLES), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--artifacts", required=True)
    args = parser.parse_args()
    kernel = Kernel(args.kind, args.size, args.factor)
    from compiler_optimizer.workers.docker_runner import DockerRunner

    runner = DockerRunner(args.image, target=args.target)
    ref = run_kernel(kernel, runner, ArtifactStore(args.artifacts))
    print(json.dumps(reference(ref), sort_keys=True))


if __name__ == "__main__":
    main()
