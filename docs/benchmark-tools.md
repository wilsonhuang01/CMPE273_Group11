# Benchmark tools and bounded parallel execution (Action M)

The original single-thread policy remains the default. `parallel-2` is an explicit,
operator-selected native CPU profile: two OpenMP threads, a two-CPU quota and an
identical pinned CPU pair for both sides of every end-to-end comparison. A typed
`CompilerSettings.execution_profile` and the fixed `-fopenmp -DCO_THREADS=2` flags
bind the profile to the immutable baseline, proposals, verification and measurements.
Changing the profile requires a new baseline. Arbitrary submission flags and build
scripts remain unsupported.

## Build and use

The native compiler image installs Clang/LLVM 18, libomp 18 and Ubuntu's Google
Benchmark development package. The image's content digest pins all tool versions.
On ARM64:

```sh
docker build -f docker/compiler.Dockerfile --build-arg EXECUTION_TARGET=linux-aarch64 \
  -t cmpe273-action-m:18 .
python -m compiler_optimizer.workers.baseline examples/parallel-sum --state var \
  --image cmpe273-action-m:18 --target linux-aarch64 --execution-profile parallel-2
python -m compiler_optimizer.workers.verify TASK --candidate CANDIDATE --state var \
  --image cmpe273-action-m:18 --target linux-aarch64 --execution-profile parallel-2
python -m compiler_optimizer.workers.benchmark TASK --verification-job JOB --state var \
  --image cmpe273-action-m:18 --target linux-aarch64 --execution-profile parallel-2
```

`CANDIDATE` contains only the listed translation units and optional local headers.
For Intel build with `EXECUTION_TARGET=linux-x86_64` and use that target in each
command. The compiler image must be rebuilt for the parallel capability label. The Docker
VM needs at least three logical CPUs: two reserved workers plus the existing
one-CPU admission headroom.
The default single-thread profile uses no `--execution-profile` argument.

This first parallel profile supports local, native source submissions through the
standalone workflow. It does not enable parallel fleet dispatch, RISC-V emulation,
LLVM/MLIR proposals, nested OpenMP teams, GPU work or parallel libFuzzer campaigns.
It supports ordinary C/C++ threads as well as OpenMP. Two runnable worker threads
are the hard ceiling, not a promise of a twofold speedup.

## Bounds, fairness and accounting

Native parallel execution allows four container tasks total: the trusted supervisor,
the trusted measurement launcher and two submitted threads. The child receives a
second seccomp filter that permits thread clones and denies process forks. `clone3`
returns `ENOSYS` for libc fallback. The child filter rejects nonnative syscall ABIs,
including x86 compat32 and x32, before checking fork/clone calls. The outer profile
adds only `sched_setaffinity`
to the original native allowlist. Default single-thread PID/RLIMIT settings and
seccomp JSON remain unchanged. Root filesystem, UID/GID 10001, no network, capability
drops, tmpfs/memory/output bounds and external watchdog cleanup remain mandatory.

`OMP_NUM_THREADS=2`, `OMP_THREAD_LIMIT=2`, dynamic teams disabled, one active team
level, and disabled libomp hidden/monitor threads keep the runtime within the task
ceiling. The CPU-time limit accounts for two cores. The host admission gate reserves
two CPUs plus the declared memory, including inside nested work windows; it rejects
overcommit instead of silently running a wider job in a one-core reservation.

End-to-end benchmarking still reuses the exact normal binaries from successful
reference/expected-output and sanitizer verification. Mandatory reviews and the
Benchmark Agent remain in the workflow. Paired runs use the same image, limits,
thread profile, two-CPU quota and CPU pair. Raw evidence records the resource
identity and `wait4` CPU seconds summed across the process's threads, alongside wall
time and peak RSS. Statistical qualification rejects resource mismatches and changing
environments, and retains all existing duration/noise/memory/improvement gates.
A tiny workload can pass correctness and comparison while remaining unqualified.

## Supplementary Google Benchmark kernel adapter

After mandatory verification succeeds:

```sh
python -m compiler_optimizer.workers.measurement_tools kernel TASK PROPOSAL --state var \
  --image cmpe273-action-m:18 --target linux-aarch64 --execution-profile parallel-2 \
  --kernel sum-i64-v1
```

The controlled C++17 contracts export one of:

```cpp
extern "C" int64_t co_sum(const int64_t*, size_t);                  // sum-i64-v1
extern "C" int64_t co_dot(const int64_t*, const int64_t*, size_t);  // dot-i64-v1
```

Guard only the ordinary `main` with `#ifndef CO_BENCHMARK`; keep the same kernel
function in both builds. `examples/parallel-sum` demonstrates the sum contract.
The worker supplies the reserved driver and links Google Benchmark; submissions
cannot choose the adapter, flags or harness commands. Deterministic arrays and a
Python-computed integer oracle check every result, including measured iterations.
Google Benchmark receives fixed iteration/repetition counts and reports real-time
and process CPU-time samples (including worker threads) in nanoseconds. Identical policies reproduce the workload,
seed and sample count; physical timing values naturally vary. Very short kernels
can report zero CPU time at the library timer's resolution; reports explicitly set
`cpu_time_resolution_limited` in that case and retain the original samples. Wall
time must remain positive, and negative/nonfinite sample timings are rejected.
Derived statistics such as the coefficient of variation of three zeros can be
nonfinite; normalized reports replace those values with null and list their paths
in `nonfinite_fields`. The original Google Benchmark output is retained separately
as `raw_google_benchmark`.

`KernelBenchmark` bounds arrays to 16–65,536 elements, iterations to 1–1,024,
repetitions to 3–11 and explicit warmup calls to 0–32. Defaults are 4,096 elements,
64 iterations, five repetitions, two warmups and seed 273. The package's compiler,
run deadline, memory and output limits still apply. Google Benchmark's runtime uses
a separate bounded harness budget of six container tasks, thread-only children,
the runner's one- or two-CPU quota, and its own policy digest. The default program
sandbox is not relaxed to accommodate the benchmark library.

Reports persist the policy, source, oracle driver, binary, build log, immutable image,
environments, raw Google Benchmark JSON and invocation evidence. These are
**supplementary kernel measurements**, with `performance_qualification: false`.
They cannot manufacture a successful correctness result or enter the repository's
end-to-end qualification path. Correctness remains bounded testing, not a proof
that source has identical behavior under all preprocessor configurations.

A repository-owned CMake fixture is also available when Google Benchmark >=1.8
and OpenMP development packages are installed:

```sh
cmake -S . -B build -DBUILD_KERNEL_BENCHMARKS=ON
cmake --build build --target kernel_sum_benchmark
```

## Optional Linux perf counters

```sh
python -m compiler_optimizer.workers.measurement_tools perf TASK PROPOSAL --state var \
  --image cmpe273-action-m:18 --target linux-aarch64 --execution-profile parallel-2
```

This separate, exclusive measurement pass runs both exact verified normal binaries
against declared benchmark inputs, rechecks their output, and collects only
user-space per-process events, including newly created threads. Before exec, the
unprivileged child attempts `perf_event_open` for cycles, instructions, task-clock,
context switches and page faults. Successfully opened handles are transferred to
the trusted launcher. Kernel/hypervisor execution is excluded; counting starts on
exec. Read handles are not exposed to the submitted binary.

No `--privileged`, host perf configuration changes or `CAP_PERFMON` grants are used.
The optional perf profile adds only `perf_event_open` to the selected syscall
allowlist and records that profile's digest. Docker VM kernels or host policy may
deny all events. Denied/unsupported/unscheduled events report `status: unavailable`,
`count: null` and an errno; they never masquerade as zero. Available events retain
raw counts and enabled/running nanoseconds so multiplexing is visible. They are not
silently scaled or used for qualification. Package energy is probed separately through read-only powercap files. When the VM
exposes readable package domains, reports retain microjoules, joules, domain identity
and the elapsed interval, accounting for at most one counter wrap. Its scope is
explicitly `package-wide`: it includes other VM activity and launcher overhead and
cannot be attributed to one process. Missing or denied domains are explicitly
unavailable. No host mount or capability is added to obtain energy readings.

Perf reports are supplementary artifacts with `performance_qualification: false`.
Instrumented runs never replace normal end-to-end timing samples.

The [Google Benchmark guide](https://github.com/google/benchmark/blob/main/docs/user_guide.md)
describes fixed iterations, repetitions and JSON output; the
[Linux perf_event_open reference](https://www.kernel.org/pub/linux/docs/man-pages/book/man-pages-6.13.pdf)
describes event inheritance, enable-on-exec and availability constraints.

## Validation

Unit tests cover strict kernel bounds, Google Benchmark sample parsing, available
and unavailable perf results, resource-policy identity, same-resource comparisons
and CPU admission. Native Docker tests cover actual two-thread OpenMP execution,
reference/sanitizer gates, paired CPU-pinned comparison, Google Benchmark reports,
perf probing, denied process creation, the hard thread ceiling and timeout cleanup.
CI runs both suites with its native x86-64 compiler image.
