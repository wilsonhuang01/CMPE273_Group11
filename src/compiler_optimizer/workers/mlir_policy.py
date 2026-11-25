"""Fixed MLIR 18 recipes. Input is generated from a typed kernel, never user IR."""

PIPELINES = {
    "tile": ("/usr/bin/mlir-opt-18", "--affine-loop-tile=tile-size=4"),
    "lower": (
        "/usr/bin/mlir-opt-18",
        "--lower-affine",
        "--convert-scf-to-cf",
        "--convert-arith-to-llvm",
        "--finalize-memref-to-llvm",
        "--convert-func-to-llvm",
        "--reconcile-unrealized-casts",
    ),
    "translate": ("/usr/bin/mlir-translate-18", "--mlir-to-llvmir"),
}
