# Expanded correctness and bounded fuzzing (Action L)

Verification always runs supplied expected-output tests, immutable reference-output
comparisons and native UBSan/bounds checks. Generated oracle cases compare **both**
the reference and candidate against independent Python results. An invalid reference
fails the task. A rejected candidate cannot proceed to measurements or selection.
No submitted compiler or executable runs on the host.

## Deterministic stdin contracts

Set `generator` in an operator-owned verification plan to, for example:

```json
{"kind":"sort","seed":273,"cases":8,"max_size":8,"bound":100}
```

Supported kinds and exact contracts:

- `sort`: `n` followed by `n` signed integers; output ascending values.
- `search`: `n`, sorted integers, then a key; output the first zero-based index,
  or `-1`. Duplicate keys use the first occurrence.
- `matrix`: `n`, then two row-major square integer matrices; output their row-major
  product. The Python oracle uses arbitrary precision arithmetic.
- `reverse`: one ASCII line, including the empty line; output its reversal.
- `dot`: `n`, then two double vectors; output the dot product using a Python
  `math.fsum` oracle.
- `norm`: `n` and a double vector; output its Euclidean norm with `fsum` and `sqrt`.

All outputs end in a newline; array/matrix elements are space separated without a
trailing space. Defaults include empty, singleton, maximum-size alternating extrema,
zero/duplicate and seeded random cases. `max_size` is 1–16, `bound` 1–1000,
`cases` 1–32, and seeds are unsigned 32-bit integers. Floating inputs are bounded
multiples of 1/8. Use numeric comparison for floating kernels, for example:

```json
{
  "schema_version": 1,
  "comparison": {"mode":"numeric","absolute_tolerance":1e-10,"relative_tolerance":1e-12},
  "sanitizer": "undefined",
  "generator": {"kind":"norm","seed":273,"cases":8,"max_size":8,"bound":100}
}
```

Numeric comparison rejects NaNs, infinities, malformed tokens and token-count
changes. Tolerances are operator policy. These finite, bounded floating domains do
not test subnormals, rounding modes, transcendental functions or IEEE exceptional
values. Oracles apply only to these explicit contracts; they do not infer arbitrary
program semantics. Existing `integer_array` plans remain compatible.

Repository-owned C++17 fixtures for every domain are in `examples/correctness/`.
This is the controlled submission harness alternative to GoogleTest: fixed stdin,
fixed expected stdout, no submitted build scripts or arbitrary harness commands.

## Optional coverage gate: sort-library-v1

For native C++17 source proposals, add this optional field to the verification plan:

```json
"coverage": {"seed":273,"seconds":5,"runs":1000,"max_length":256,"minimize_seconds":2}
```

The source must export `extern "C" void co_sort(int *, size_t)` and guard its normal
`main` with `#ifndef CO_FUZZ`. The normal entry point must exercise the same function;
`examples/correctness/sort` is a complete example. The operator-owned adapter in
`workers/fuzzing.py` decodes bytes into bounded signed integers and compares with
`std::sort`, aborting on disagreement. It supplies `LLVMFuzzerTestOneInput`; source
cannot replace the adapter file. Compilation uses Clang 18 libFuzzer, UBSan/bounds,
LLVM source coverage and immutable trusted flags. Normal reference and sanitizer
checks finish before the coverage campaign. Any campaign failure rejects the
verification result before measurements. Stored successful evidence requires the
coverage policy and report digest when coverage was requested.

This adapter is deliberately limited to sorting libraries. Other domains use the
deterministic stdin/oracle contract. Arbitrary `main` programs, LLVM/MLIR proposals,
RISC-V emulation, GoogleTest build systems and concurrent submission execution are
not supported by this libFuzzer adapter. This does not claim universal fuzz coverage.

A supplementary campaign/replay against a proposal that already passed mandatory
verification is also available (it records a separate campaign result and does not
retroactively replace that earlier result):

```sh
python -m compiler_optimizer.workers.fuzzing TASK PROPOSAL --state var \
  --target linux-aarch64 --image cmpe273-fuzz-test:18 --seconds 5
python -m compiler_optimizer.workers.fuzzing TASK PROPOSAL --state var \
  --target linux-aarch64 --image cmpe273-fuzz-test:18 --replay failure.bin
```

For selection to depend on fuzzing, use the `coverage` verification-plan field.
Replay takes raw bytes, not textual integer stdin. Persisted artifact entries include
`key`, `sha256` and `size_bytes` for verified retrieval with `ArtifactStore.get`.

## Bounds, artifacts and replay

The separate harness policy permits eight container tasks for libFuzzer's runtime
thread and bounded minimizer subprocesses, bypassing the single-process RSS launcher.
Default native execution still has its original PID and RLIMIT_NPROC settings.
The harness seccomp profile additionally permits the process-local `setitimer` syscall
for libFuzzer's timeout; the default allowlist is unchanged. Both profiles retain private namespaces, read-only root,
no network, no host mounts, UID/GID 10001, one CPU, no swap, bounded tmpfs, output
pipes and external cleanup watchdog. Harness memory is fixed at 512 MiB;
libFuzzer RSS/malloc budgets are 384/128 MiB. Per-input timeout is one second.

Operator policy caps campaigns at 30 seconds and 100,000 runs; max input length is
4096 bytes and minimization gets at most five seconds plus two seconds of supervisor
grace. The whole invocation watchdog includes an eight-second startup/minimization
grace. Each campaign has fixed numeric seeds and immutable initial seed bytes
(truncated for smaller declared input lengths). Time-limited corpus evolution is
not guaranteed bit-for-bit reproducible across architectures or loads; replay bytes,
binary, image identity and seeds are persisted.

Reports retain compiler/harness/source/binary/seed hashes, the bounded run and
minimization diagnostics, coverage counters/log, final corpus, raw crash/timeout/OOM
inputs and any minimized reproducer. Successful runs also emit LLVM `.profraw`
coverage. Crash termination can prevent the profile flush; libFuzzer's coverage log
is retained in that case. Exported files total at most 1 MiB and 130 entries, with
failure inputs prioritized over corpus entries. Temporary writes are capped by
64 MiB tmpfs and RLIMIT_FSIZE. Minimization is best effort: on timeout or failure,
retain and replay the original input. A bounded run without a finding is evidence
of the declared campaign, not a proof of correctness.

## Docker-only verification

Build the sandbox and trusted test controller:

```sh
docker build -f docker/compiler.Dockerfile --build-arg EXECUTION_TARGET=linux-aarch64 \
  -t cmpe273-fuzz-test:18 .
docker build -f docker/action-l-tests.Dockerfile -t cmpe273-action-l-tests .
docker run --rm --network none --read-only --tmpfs /tmp:size=512m \
  -v "$PWD:/workspace:ro" cmpe273-action-l-tests \
  tests/test_domains.py tests/test_verification.py tests/test_sandbox.py tests/test_baseline.py \
  -q -p no:cacheprovider
docker run --rm --network none --read-only --tmpfs /tmp:size=512m \
  -v "$PWD:/workspace:ro" -v /var/run/docker.sock:/var/run/docker.sock \
  -e BASELINE_DOCKER_TESTS=1 -e COMPILER_IMAGE=cmpe273-fuzz-test:18 \
  -e COMPILER_TARGET=linux-aarch64 cmpe273-action-l-tests \
  tests/test_correctness_domains_docker.py -v -p no:cacheprovider
```

The trusted test controller receives the local daemon socket to create sibling
sandboxes. Submitted code never receives that socket or a repository mount.
On Intel use `EXECUTION_TARGET=linux-x86_64` and `COMPILER_TARGET=linux-x86_64`.
