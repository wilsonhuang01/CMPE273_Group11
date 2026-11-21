# Initial CPU workload and submission contract

Status: implemented CPU submission contract. Package intake, sandboxed baseline
execution, generated proposals, mandatory correctness checks, repeated benchmarking,
weighted selection and local worker recovery are implemented. Start with the
[docs index](README.md) for current progress and the [evaluation guide](evaluation.md)
for the complete local demo. The host [transfer service](artifact-transfer.md) accepts
package uploads; independent remote worker dispatch remains deferred.

## Supported programs

- A deterministic, single-process, single-threaded command-line executable with
  one `main` function, written in C17 or C++17.
- Controlled Linux x86-64 or ARM64 worker profiles. Baseline and candidate
  measurements use the same worker context and compiler configuration.
  RISC-V/QEMU adds correctness-only validation; see [Architecture support](architectures.md).
- One or more `.c` files for C17, or `.cpp` files for C++17, plus local `.h`/`.hpp`
  headers. Mixed-language builds are deferred.
- Input through stdin and optional command-line arguments; results through stdout.
  Successful executions return exit code zero. Stderr is captured for diagnostics.
- Only the C/C++ standard libraries and the system math library. No downloaded
  dependencies, custom build scripts, network requests, or external services.
- Exact textual results for the initial workload. Floating-point computation is
  allowed only when its required output is deterministic and matches byte for byte;
  the [verification plan](verification.md) can explicitly enable numeric tolerances
  for candidate verification. Baseline fixture intake remains exact-byte.

Initial examples should exercise integer array reductions, sorting, searching,
and bounded integer matrix operations. Programs must avoid signed integer overflow,
uninitialized memory, and other undefined behavior for every supplied input.

CUDA/GPU execution, architecture-specific intrinsics, assembly, OpenMP,
threads, subprocesses, interactive input, filesystem-based output, and programs
that depend on clocks, randomness, environment variables, or external files are
outside the CPU submission contract. RISC-V/QEMU validation and offline CUDA-to-PTX
compilation are separate implemented paths; GPU execution is not implemented.
MLIR passes and user-supplied LLVM IR remain deferred.

## Submission package

A user supplies a directory containing `submission.json`, source/header files,
and input/expected-output fixtures. The [transfer API](artifact-transfer.md)
transports these contents as a bounded snapshot and stores them centrally.
Local directory ingestion remains available through the [baseline CLI](baseline.md)
and host coordinator commands.

All paths are relative to the submission root. Reject absolute paths, `..`
components, symlinks, and references outside that root. Accept only regular files
and directories. Limit each package to 100 files and 10 MiB total file contents;
count the manifest and fixtures in both limits.

The manifest has these required fields:

- `schema_version`: integer `1`.
- `name`: 1–64 characters from ASCII letters, digits, `_`, and `-`.
- `language`: `c17` or `cpp17`.
- `sources`: a nonempty list of distinct source paths, in compiler argument order.
  Every source must have the extension for the selected language. Headers are
  packaged separately and included by source files; do not list them as sources.
- `tests`: 1–100 test objects. Each has a unique `id` (same character rules as
  `name`), `stdin` fixture path, `expected_stdout` fixture path, and `args` array
  of strings. An empty fixture represents empty input/output. Arguments are passed
  directly to the executable without shell expansion; limit them to 32 strings,
  each at most 1,024 UTF-8 bytes with no NUL characters.
- `benchmark_test_ids`: a nonempty list of distinct IDs from `tests`. These inputs
  must pass correctness checks before being used for timing.
- `limits`: the complete resource-limit object described below.

Reject missing or unknown fields, invalid field types (including booleans used as
integers), missing fixture/source files, duplicate IDs, and invalid references.
These validation rules apply to all objects, including individual tests and limits.
The [example manifest](../examples/sum-array/submission.json) shows every field.

## Controlled build instructions

The worker owns the build command. Users select the language and source files;
they cannot supply commands, compiler options, linker options, or environment
overrides. The baseline worker image uses Clang/Clang++ 18 and its standard library
dependencies; the resolved immutable image digest and compiler version accompany results.
The initial implementation caps compiled executables at 16 MiB.

For the default x86-64 C17 profile, the command shape is:

```text
clang -std=c17 -O2 -march=x86-64 -mtune=generic -Wall -Wextra -I /work/src <sources...> -lm -o /work/build/program
```

For C++17, use `clang++` and `-std=c++17` with the same remaining options. Resolve
source paths beneath `/work/src` and invoke the compiler with an argument vector,
never a shell command. Run both baseline and proposal builds inside sandboxes.
Keep compiler options identical when comparing source transformations; proposals
may change source code but cannot change tests, limits, or build settings.

## Correctness and baseline acceptance

1. Validate the package before compiling or generating proposals.
2. Compile the original source. A compile failure or compile resource violation
   rejects the submission before optimization.
3. Run each supplied test in a fresh sandbox, feeding the exact stdin bytes and
   arguments. Require exit code zero and byte-for-byte expected stdout, including
   whitespace and trailing newlines. Capture stderr without comparing its contents.
4. Reject a baseline that crashes, exceeds a limit, or fails any test. Record the
   failing test ID and a diagnostic reason.
5. Run every candidate against the same expected outputs and compare its stdout
   with the recorded baseline outputs. Any failure disqualifies that candidate.
6. Benchmark only accepted baselines and candidates on the selected test IDs.
   Score performance and memory under the [selection policy](coordinator-selection.md),
   retaining the baseline unless a candidate passes reviews and demonstrates a
   reliable improvement. Repetition counts and significance thresholds belong
   to the [benchmark policy](benchmarking.md).

Supplied tests are required but do not establish equivalence for all inputs.
[Candidate verification](verification.md) now adds sanitizer checks, randomized
differential tests, and structured fuzzing under an explicit valid-input domain.

## Requested limits and worker policy

All values are positive integers. Every `limits` field is required. The example
uses the default values below. Users may request lower values; reject values above
the policy maximum instead of silently changing them.

- `compile_wall_seconds`: default 30, maximum 60, per compiler invocation.
- `compile_memory_mib`: default 1024, maximum 2048, for the compiler sandbox.
- `run_wall_seconds`: default 2, maximum 10, per test or benchmark invocation.
- `run_memory_mib`: default 256, maximum 512, for the execution sandbox.
- `output_kib`: default 64, maximum 1024, combined stdout and stderr per compile
  or execution invocation. Kill the invocation when it exceeds this limit.

Worker-controlled settings cannot be overridden by submissions: at most one CPU
core of quota per sandbox, no network, an unprivileged user, dropped capabilities,
read-only source and root filesystem, and a writable 64 MiB temporary filesystem.
Account temporary memory against the sandbox memory limit. Compilation permits up
to 64 processes/threads and a separate writable 128 MiB build filesystem. Execution
permits one untrusted program process/thread plus trusted supervision processes
(three total PIDs for native measurement v2; see [sandbox policy](sandbox.md)),
has no writable build filesystem, and receives only
the executable and selected fixture input. The worker streams and caps output
outside the sandbox and enforces wall deadlines with an external watchdog.

Terminate and remove the entire sandbox on a deadline or resource violation.
Report validation errors, compilation failures, output mismatches, nonzero exits,
signal terminations, timeouts, memory exhaustion, and output-limit violations
separately where the worker has evidence; preserve an unknown failure category
when it cannot reliably determine the cause. Deterministic program failures must
not trigger unlimited retries.

## Implemented CPU pipeline acceptance criteria

- Accept and validate the example package without executing user-defined commands.
- Compile the baseline and pass every supplied test on a controlled Linux CPU worker.
- Generate one source-change proposal and run it through the same build and tests.
- Reject a deliberately incorrect proposal and terminate a deliberately hanging
  proposal within the configured deadline.
- Return the selected source (or the unchanged baseline), test outcomes, timing
  and memory measurements, resource failures, source hashes, compiler configuration,
  and worker hardware information.

These paths are implemented and exercised by unit/service and real Docker tests.
The tiny example validates submission shape and correctness; the larger deterministic
workloads and recorded results are documented in [Evaluation and release](evaluation.md).
