# Generalized native CPU transformations (action I)

Default Performance, Memory and Architecture services now use bounded syntax-aware
rules from `agents/rules.py`. They do not select programs by source hash or filename
and need no model credentials. The old `Example*Backend` classes remain available
for reproducible legacy evaluation recipes, but are no longer the service or
coordinator defaults. Configured action G models and operator replay patches still
use the same checked proposal interfaces.

## Supported rule subset

Rules accept one UTF-8 C17/C++17 translation unit, at most 128 KiB, with no local
headers. Leading direct system includes may name `stdio.h`, `stdlib.h`, `stdint.h`,
`stddef.h` or `limits.h`. Comments and whitespace are tokenized rather than used as
transformation anchors. Macros, conditional preprocessing, custom includes, target
attributes, volatility and alias promises cause explicit abstention. This is a
small grammar, not a general C/C++ compiler or a semantic equivalence prover.

- **Input buffering (Performance):** a complete bounded integer echo, scalar sum
  or buffered-sum `main` using C stdio. The parser checks the complete function and
  its input validation. It adds a static 64 KiB stdin buffer before the first I/O
  operation. Existing buffer configuration, arbitrary I/O wrappers and unrecognized
  side effects abstain. Supported C-style source also compiles as C++17.
- **Streaming memory (Memory):** a bounded count, a checked `malloc` array of
  `long long`, a complete checked input loop, then a sum loop and print. Identifiers,
  decimal bounds, formatting and C/C++ allocation casts can vary. The rule proves
  `count_max * max(abs(value_min), abs(value_max)) <= LLONG_MAX`. It retains the
  original allocation request and NULL failure branch, frees the untouched array,
  then streams values into a scalar. This reduces retained/touched storage; it
  does **not** remove the initial O(count) allocation request or establish a peak
  virtual-memory bound of O(1). RSS savings require actual measurement.
- **Independent accumulators (Architecture):** a checked scalar integer sum with
  explicit count/value bounds and a representable bound for every intermediate
  and final sum. Two independent streams preserve input validation and output.
  Signed reassociation without these bounds is unsupported.
- **Loop unrolling (Performance):** a complete top-level unsigned pointwise kernel
  with an unsigned count/index, direct array access, and a constant `+`, `*`, `^`,
  `&` or `|` operation. The parser accepts optional `const` on the input pointer and
  preserves the signature. Two lanes execute in original order, including for
  partially overlapping arrays. `n-i` guards avoid index wrap; a scalar tail
  handles small and odd lengths. Unsuffixed constants must fit signed int; larger
  unsigned constants require `U`.
- **Guarded vectorization candidate (Architecture):** the same pointwise grammar.
  An equal-input/output-pointer guard exposes four independent in-place lanes.
  Other pointer relationships retain the original loop, including partial overlap.
  No relational comparison of unrelated pointers, `restrict`, alignment assertion,
  SIMD pragma or new flags is introduced. The fixed compiler decides whether to
  vectorize; this candidate does not guarantee SIMD instructions or speedup.

A pointwise kernel can coexist with an unchanged `int main(void)` or `int main()`
driver. Other top-level declarations, reductions with unchecked overflow,
floating-point reassociation, data-dependent indexing, shifted source accesses,
typedef/template/operator-heavy kernels and unknown side effects are outside the
rule grammar. Unsupported input raises `TransformationAbstained`; RPC preserves
that result, and the cohort records `generation_abstained` evidence while retaining
its reserved candidate slot. No candidate or measurement is invented for abstention.

For example, both native profiles support this kernel grammar:

```c
void scale(const unsigned int *input, unsigned int *output, unsigned int length) {
    for (unsigned int index = 0; index < length; ++index) {
        output[index] = input[index] * 3U;
    }
}
```

Proof conditions concern valid executions of this checked grammar under the
controlled Linux ABI (32-bit int, 64-bit size_t/long long). They are not permission
to use invalid pointers, out-of-bounds buffers or inputs outside explicit checks.
The normal compiler, fixed/generated tests, sanitizer checks, Critic review and
benchmark qualification remain mandatory.

Portable Performance and Memory rules also admit the controlled RV64GC/LP64D
profile for correctness validation under QEMU. Its scalar ABI satisfies the same
rule preconditions. Architecture rules remain native ARM64/x86-64 only. RISC-V
candidates still pass verification and reviews, but cannot qualify benchmarks or
displace the baseline through performance selection.

## Versioned source contracts

Rules emit structured patch schema v2: the v1 fields plus a required `contract`.
For portable C on ARM64:

```json
{
  "schema_version": 1,
  "mode": "portable",
  "target": "linux-aarch64",
  "language": "c17",
  "required_features": []
}
```

This is a declaration inside the patch, not a change to compiler settings.
Its target and language must match the immutable assignment. Portable patches
declare no ISA requirements. Target-specific source uses `mode=baseline-intrinsics`
and explicit baseline features: `neon` on ARMv8-A or `sse2` on generic x86-64.
The supported intrinsic/header vocabulary is deliberately small and versioned in
`shared/transformations.py`. Unknown intrinsics, AVX, RISC-V vectors, new macros,
assembly, target attributes, pragmas and alias/alignment assumptions are rejected.
The rule backends themselves generate portable scalar source only.

Legacy schema v1 portable replay patches remain accepted. Intrinsic markers or
headers require the explicit v2 contract; declaring a feature unavailable to the
assigned target fails validation. Model replies can supply the same optional
contract; the service still constructs the parent-source binding itself. Contract
checks run at generation, RPC receipt and coordinator storage/evaluation boundaries.
They conservatively recognize unsupported constructs and do not replace compiler
or semantic verification for arbitrary model/operator source.

## Native execution and examples

The standalone pipeline and cohort commands now detect the Docker engine's native
CPU when `--target` is omitted. ARM Macs select `linux-aarch64`/`cmpe273-arm:18`;
Intel Macs select `linux-x86_64`/`cmpe273-clang:18`. Explicit `--target` and `--image`
remain available, including when resuming work assigned to another profile. Fixed
flags stay `-march=armv8-a` or `-march=x86-64` with generic tuning. Emulated execution
never becomes eligible for native performance claims through this change.

Five complete fixture packages live in `examples/cpu-transformations`: bounded
integer echo, buffered totals in C and C++, unsigned scaling in C and unsigned XOR
in C++. Pointwise fixtures test separate buffers, identical pointers, both partial
overlap directions, unsigned wraparound, empty input and scalar tails.

```sh
.venv/bin/python -m compiler_optimizer.coordinator.round \
  --package examples/cpu-transformations/unsigned-scale --state var/scale

.venv/bin/python -m compiler_optimizer.coordinator.pipeline \
  --package examples/cpu-transformations/buffered-total-cpp \
  --agent memory --state var/streaming-cpp
```

Use the existing broker/recovery setup and build the native compiler image first
(`./project up` does this). With tiny examples, reliable gains are generally not
established; retaining the baseline is expected and remains an acceptance outcome.
Tests never substitute source-level estimates for measured runtime or RSS.

```sh
.venv/bin/python -m pytest tests/test_transformations.py
TRANSFORMATION_INTEGRATION_TESTS=1 WORKER_TARGET=linux-aarch64 \
  COMPILER_IMAGE=cmpe273-arm:18 .venv/bin/python -m pytest tests/test_transformations_docker.py
```

Use the x86-64 target/image on a native amd64 worker. Tests cover assignment/contract
binding for both targets; native hardware coverage is reported separately from
compile/correctness checks on an emulated target.
