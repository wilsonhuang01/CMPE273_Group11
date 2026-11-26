# Bounded MLIR kernels

Action K provides an explicit kernel experiment path. The versioned `Kernel`
contract accepts elementwise signed i32 array addition and scaling, lengths
1–4096 and scale factors −16–16. Inputs are deterministic signed integer
sequences whose bounds prevent overflow. Three distinct arrays avoid alias
assumptions. Unsupported contracts are rejected before invoking the compiler.
This is not an arbitrary C/C++ or user-supplied MLIR frontend.

The image installs MLIR 18 with LLVM/Clang 18. Each MLIR tool checks its version
against the assigned compiler; the resolved image digest is recorded. Trusted
recipes tile affine loops by four, lower to LLVM dialect and translate to LLVM
IR. Existing LLVM sandbox stages compile the C ABI driver, link, verify and lower
to native ARM64 or x86-64. All stages use the compiler memory, process, output and
watchdog limits. No user-supplied commands or pass recipes are accepted.

Run from the project root (use `linux-x86_64` on native x86 Docker):

```sh
docker build --build-arg EXECUTION_TARGET=linux-aarch64 \
  -f docker/compiler.Dockerfile -t cmpe273-mlir-test:18 .
.venv/bin/python -m compiler_optimizer.workers.mlir add --size 17 \
  --target linux-aarch64 --image cmpe273-mlir-test:18 --artifacts /tmp/mlir-add
.venv/bin/python -m compiler_optimizer.workers.mlir scale --size 31 --factor -2 \
  --target linux-aarch64 --image cmpe273-mlir-test:18 --artifacts /tmp/mlir-scale
```

The command prints a content-addressed report reference. Artifacts include original,
tiled and lowered MLIR, translated LLVM IR, driver and linked bitcode, the native
binary, recipes, compiler logs and execution evidence. An independent Python
oracle checks every output element. Compiler errors and mismatches fail closed;
failed execution reports are retained. This separate path does not enter source
proposal selection or claim performance improvements. Arbitrary kernels and
floating point are outside the contract.

```sh
.venv/bin/pytest tests/test_mlir.py -q
MLIR_INTEGRATION_TESTS=1 WORKER_TARGET=linux-aarch64 \
  COMPILER_IMAGE=cmpe273-mlir-test:18 .venv/bin/pytest tests/test_mlir.py -q
```

Native ARM64 integration tests cover both kernels, tiled tails and length
boundaries. x86-64 requires a separate native engine for validation.
