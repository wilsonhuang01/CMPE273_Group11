# Reproducible benchmarking

[Specialized agents](specialized-agents.md) use v2 memory-objective plans, a bounded runtime
regression gate, required reviewer evidence and frozen Benchmark Agent work for new
pipeline workflows. Existing v1 runtime plans preserve the policy documented here.

Run benchmarking after a proposal passes correctness verification. The standalone
CLI example below uses the default x86-64 worker; use the
[target-aware cohort/evaluation workflow](evaluation.md) for native ARM64. Use the task ID
and verification job ID printed by the baseline/verification commands, with the
same state directory and immutable compiler image:

```sh
python -m compiler_optimizer.workers.benchmark TASK_ID \
  --verification-job JOB_ID --state var/baselines \
  --plan examples/sum-array.benchmark.json
```

The plan is optional; its default values match the example. `--attempt` defaults
to 1. Exit codes are 0 for completed measurements (including retaining the
baseline), 1 for persisted execution/infrastructure failures, and 2 for invalid
requests or unavailable prerequisites. Output includes the run ID, decision,
per-workload statistics, and rejection reasons. No final task decision is made
by this standalone CLI. The [cohort coordinator](coordinator-selection.md)
organizes measurement work through the Benchmark Agent and uses the results for
weighted selection, with [bounded infrastructure retries](fault-recovery.md).

## Controlled execution

The worker revalidates successful, current correctness evidence before executing
anything. It loads the exact normal baseline and candidate binaries recorded by
that verification job, verifies their content hashes and task/job ownership, and
does not recompile them. Sanitized binaries are excluded from timing. Compiler
image digest, Clang version, language, target, and flags are recorded.

Every manifest `benchmark_test_ids` workload runs serially in fresh containers,
under the original manifest's time, memory and output limits and the existing
[sandbox policy and watchdog](sandbox.md). Both programs receive a one-CPU quota
and the same explicit CPU affinity. `cpu_id: null` chooses the lowest CPU allowed
by the worker's affinity; an explicit ID must be available. Independent watchdogs
must confirm container removal for every sample.

The worker records the Docker engine identity/architecture, kernel boot identity,
CPU model and feature hash, available CPUs, selected CPU, frequency governor when
available, clock resolution, and load averages before and after the run. Identity,
affinity, governor and clock changes reject qualification. Load averages are
diagnostic. Native Linux x86-64 and ARM64 profiles can qualify performance results.
The target/native claim must match the verified compiler and Docker engine.
Emulated x86-64 on ARM cannot qualify; RISC-V/QEMU is correctness-only and does not
run qualification batches. Use an idle native worker and see [evaluation](evaluation.md)
for the recorded ARM64 comparison and its shared-VM limitations.

Default policy uses two warmup pairs and fifteen measured pairs per workload.
Baseline/candidate order alternates AB/BA, with starting order derived from the
seed and test ID using SHA-256. Warmups use the same isolation and output checks
but are excluded from statistics. All measured samples are retained; no outliers
are trimmed. Plans allow 0–10 warmup pairs and 7–101 measured pairs, with an
overall cap of 4096 invocations. The seed controls order, not operating-system
scheduling, caches or frequency scaling. Fresh containers make these whole-program
measurements; this is not an in-process microbenchmark or a guaranteed warm-cache run.

Each invocation records supervised child elapsed seconds and peak child RSS in
bytes, exit status, output hashes, profile, affinity and cleanup confirmation.
Elapsed time includes child launch, execution and supervision through child exit;
it excludes Docker setup, image inspection and artifact I/O. Peak RSS measures the
supervised process, not the full container. Native measurement v2 excludes the
Python pre-exec RSS floor using a protected C `wait4` launcher; evaluations require
that version. See [measurement policy](sandbox.md#native-rss-measurement-v2). Output is checked against the original
fixture and paired baseline using the same exact/numeric comparator that passed
verification. Failure details retain the bounded invocation log.

## Qualification policy

Every selected workload must pass all gates:

- Both median durations meet `minimum_sample_seconds` (default 10 ms) and at least
  100 clock ticks. The tiny sum-array fixtures often fail this gate intentionally;
  use longer deterministic workloads for performance evaluation.
- Each program's median absolute deviation divided by its median runtime is at
  most `maximum_relative_mad` (default 0.10).
- Candidate median RSS and maximum RSS each stay within
  `maximum_memory_regression` (default 5%) of their corresponding baseline values.
- A paired gain is `1 - candidate_seconds / baseline_seconds`. Count gains strictly
  above `minimum_relative_improvement` (default 5%). With `n` pairs and `w` wins,
  the exact one-sided sign-test p-value is `sum(comb(n,j), j=w..n) / 2**n`.
  Ties at the threshold count against acceptance.
- Use per-workload significance `a = alpha / workload_count` (Bonferroni correction,
  default overall alpha 0.05). For sorted paired gains, choose the largest rank `k`
  satisfying `sum(comb(n,j), j=0..k-1) / 2**n <= a`. The gain at rank `k` is a
  one-sided lower confidence bound for the population median paired gain. It must
  exist and exceed the minimum gain, and the sign-test p-value must be at most `a`.

The statistical interpretation assumes independent pairs from stable execution
conditions. Noise checks and alternating order help detect or reduce interference
but do not prove independence. These are conservative eligibility criteria for
the declared workloads and plan, not a universal speedup guarantee. Very small
repetition counts or many workloads can leave no eligible confidence-bound rank;
the worker then retains the baseline. Predeclare policy and workloads before
measurement; repeatedly rerunning until a favorable result inflates false positives.

Reports use `eligible_candidate` only if all gates pass. Otherwise they use
`retain_baseline`, with global and per-workload reasons. Correctness failure in the
candidate permanently disqualifies that task/source snapshot from selection;
reference failure atomically marks the task failed. Infrastructure failure and
noisy/unconvincing measurements preserve the running task in the standalone API.
Within a frozen cohort, only infrastructure-failed batches can retry under a
[recovery lease](fault-recovery.md); completed or noisy batches cannot be resampled.

## Persistence and selection

The benchmark migration introduced schema v2 (the current repository schema is v8)
and adds immutable
`benchmark_runs` records linked to the verification result. Versioned JSON plan,
raw measurements (at most 64 MiB), and derived report are content-addressed
artifacts owned by the benchmark run ID. `repo.list_benchmarks(task_id)` returns
report keys and eligibility; `repo.list_artifacts(task_id)` supplies full references.
Original provisional baseline/verification measurements remain available.

`record_benchmark(report_ref)` revalidates correctness, exact binary bindings,
artifact integrity and statistics before committing evidence and invalidation.
Identical run IDs/redeliveries deduplicate; changed contents conflict. Worker
reports still require a trusted authenticated producer at the future service
boundary; storage cannot prove honest execution from JSON alone.

`select_result` requires the latest benchmark for that verification job/attempt to
qualify, revalidates its blobs and recomputes analysis, and rejects any candidate
source with a recorded benchmark correctness failure. Baseline fallback remains
available for a valid running task. Benchmarking does not complete the task or
implement review voting or final ranking; those are implemented by the
[selection coordinator](coordinator-selection.md).

```sh
python -m pytest tests/test_benchmark.py
BASELINE_DOCKER_TESTS=1 python -m pytest tests/test_benchmark_docker.py
```

Unit checks cover statistical gates, strict policies, incomplete/unsafe samples,
failure handling, binary reuse, report tampering/corruption, final selection and
v1 migration. The Docker test checks actual CPU pinning, all paired measurements,
cleanup and conservative retention on a tiny workload; it makes no timing-based
speedup assertion on shared CI hardware.
