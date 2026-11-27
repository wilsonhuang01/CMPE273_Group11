# Evaluation and local release

With Python 3.11+ and Docker Desktop/Engine running:

```sh
./project up
./project demo
./project evaluate
./project status
./project test
./project package
```

`up` creates the local Python environment, installs the package if needed, builds
and starts the Compose services, builds the **native** CPU toolchain, and checks
application and monitoring readiness. The first startup needs network access for
Python packages and container images. Allow several minutes and sufficient Docker
memory to compile sanitizer builds (1 GiB per compiler container). Default ports
are in `.env.example`; optional `.env` assignments are parsed literally, never
executed as shell code. Existing environment variables take precedence. RabbitMQ
and Redis must be reachable on the host ports used by the host-side CLI. Run
`./project down` to stop services while preserving volumes and evidence.

Optimization is driven by the host CLI and its local two-worker recovery pool.
Compose provides the control plane, broker, cache and monitoring; application
containers do not receive the Docker socket or execute submissions themselves.
The local demo demonstrates logical worker recovery on this machine, not a remote
multi-host worker fleet.

The demo/evaluation commands include startup; `--no-start` reuses a healthy stack
and existing native image. `./project evaluate --trials 3` runs the expanded Action P protocol without
reusing decisions. Every run gets a new directory under `var/`.
An explicit existing `--output` is rejected; failed or noisy runs remain available
instead of silently resampling. JSON structured logs go to stderr. For example:

```sh
./project demo --no-start --output var/demo/my-run 2>var/demo-my-run.log
./project evaluate --no-start --output var/evaluate/my-run 2>var/evaluation-my-run.log
```

For implementation status and topic guides, see the [docs index](README.md).
The recorded release completed **388 unit/service tests and 43 Docker tests**,
with one native-amd64 AddressSanitizer test skipped on the ARM64 development machine.
These counts describe the recorded release, not every future source revision.

## Expanded comparison protocol (action P)

`./project evaluate` now defaults to the expanded protocol. Run it against an
already prepared native toolchain and healthy RabbitMQ/Redis services:

```sh
./project evaluate --no-start --trials 3 --size 250000 --output var/evaluate/my-action-p-run
```

The full protocol produces 36 configuration results plus their verification and
measurement evidence; allow tens of minutes on Docker Desktop.

The equivalent module command accepts `--target linux-aarch64` or
`--target linux-x86_64` and `--repetitions 15`. The expanded protocol requires
three to five trials and all three families. `--size` is the number of integers
per benchmark input; it is bounded at one million. The original two-size
comparison remains available through
`python -m compiler_optimizer.evaluation evaluate --legacy --trials 1`.

The fixed corpus contains C17 array reduction, C17 32-bin histogram, and C++17
sorting with an order-sensitive checksum. Python computes expected outputs
independently. Each family has empty, small and benchmark training cases, plus
two held-out inputs of lengths 31 and 257 generated with separate seeds. Held-out
inputs and expected outputs are excluded from generator requests and revision
feedback. They are recorded in the evidence for independent reproduction.
Each trial uses a fresh seed, baseline checkpoint and optimizer state.

Four configurations run in a deterministic shuffled order for every family/trial:

- **Original:** execute the unmodified reference, including one warmup, 15 measured
  samples and the final held-out check. It generates no candidates. Reference and
  baseline preparation costs are recorded separately from optimization cost.
- **Single:** one Performance generator, with three isolated candidate slots.
  C variants use 4/64/256 KiB stdio buffers. C++ variants progressively disable
  stdio synchronization, untie input and request a stream buffer; the standard
  library may ignore that final buffer request.
- **Multi:** one Performance, Memory and Architecture candidate in a single
  cohort. Memory rules stream the C computations or remove the C++ sorting copy;
  Architecture rules unroll reduction, express the histogram bucket mask, or use
  an explicit sorting comparator. The compiler may already implement an equivalent
  optimization; no benefit is assumed.
- **Debate:** one Performance generator with at most three rounds. It starts from
  the first single-generator candidate, receives only actionable correctness or
  review feedback, and can propose the next buffering/storage variants. Benchmark
  noise never feeds revisions. A valid initial proposal can stop after one round;
  unused rounds are not filled artificially.

Each optimizer has the same hard cap of **three candidate slots, two workers,
256 sandbox invocations and 1,800 reserved sandbox seconds**, including retries
and final checks. These are equal maximum budgets, not requirements to consume
equal work. All use the same verification, sanitizer, independent review,
benchmark-planning and selection gates. The family alpha is .05, corrected over
three candidate slots; benchmark alpha is .05/3. Minimum sample time, noise,
native-execution and regression gates remain unchanged. Sandbox reservations are
charged before invocation and persisted in each arm's cost ledger.

These are frozen **deterministic rule backends**. Every configuration has an
external-model call/token cap of zero. No credentials or paid model calls are
needed. Results describe these rule catalogs and control flows, not language-model
quality, learned optimization or general multi-agent superiority. Dollar cost is
unavailable, rather than estimated as zero infrastructure cost.

The protocol, candidate catalog, schedule, workload/holdout hashes and policies
are written to `protocol.json` before probing Docker or viewing measurements.
A digest binds the report to that declaration. The runner rejects output reuse
and preserves partial reports on interruption/failure. After selection, every arm
gets a final independent Python-oracle held-out check. A failure retains the
original; it cannot select a runner-up using held-out scores. Debate also retains
its built-in final differential check. Successful candidates are not promoted by
this evaluator unless the coordinator's original gates have accepted them.

Native x86-64 or ARM64 measurement v2 is mandatory. Original and candidate
measurements must share image/CPU/engine/boot/execution identities. Each report
records actual image digest and hardware identity; raw artifacts retain compiler
flags, verification and benchmark environments. This is local single-host
validation, with no emulated, PTX-only, GPU, cloud or physical-host claims.

`report.json`, `summary.csv`, `uncertainty.csv` and `report.html` contain runtime,
peak RSS, correctness failures, generated/verified proposals, fallback, recovery
attempts and duration, optimization wall time, sandbox calls and model usage.
The report includes both individual trials and equal-family means. A fixed-seed
95% percentile bootstrap resamples whole **trial blocks**, preserving pairing
across families/configurations; individual timing samples are not treated as
independent experiments. With only three trials, intervals are descriptive and
coarse. Paired multi-minus-single and debate-minus-single speedup differences are
reported without an automatic superiority claim. Recovery duration is a lower
bound if an interrupted attempt lacks a completion timestamp.

Source code is frozen in `implementation.zip`; SQLite/CAS evidence, raw sample
pairs, cost ledgers, audits and held-out receipts stay beside the report. The
recorded native run is linked from [benchmark results](../benchmarks/README.md).

## Legacy comparison protocol

The reference is a deterministic buffered C17 integer reduction. Workloads have
250,000 and 750,000 integers in [-1000,1000], generated with seed 160273. Expected
stdout is computed independently in Python. Both optimizer arms fork the same
quiescent baseline SQLite checkpoint and immutable artifacts, using identical input
bytes, compiler/image digest, CPU quota, CPU affinity and resource limits. The
baseline must pass tests before either arm starts. The original does no optimization;
its setup cost is separately recorded rather than charged to one optimizer.

Each arm has **three candidate slots, two workers, at most 256 total sandbox
invocations and 1,800 reserved sandbox seconds** per workload/trial. The shared
thread-safe ledger charges each requested wall limit before starting work, including
compilation, metadata probes, rejected candidates and retries. Exhaustion fails
closed; unused budget is not charged as actual work. Container compile limits are
30 s/1,024 MiB; execution limits are 2 s/256 MiB, one CPU and 64 KiB output.
Orchestration and Docker provisioning are included in elapsed optimization time;
Docker capability probes outside the sandbox are not sandbox invocations.

* **Single-agent optimizer:** the Performance Agent makes three isolated proposals,
  using 4 KiB, 64 KiB and 256 KiB stdio buffers. The best eligible result wins.
* **Multi-agent optimizer:** Performance (64 KiB buffering), Memory (streaming
  reduction), and Architecture (portable loop unrolling) each propose once in one
  coordinator cohort. Correctness and Critic reviews and Benchmark Agent planning
  remain active in both arms. These reviewers are shared safety infrastructure;
  “single” means one proposal-generating role, not absence of verification agents.

The catalogs are frozen handwritten rule backends, not external language models.
They test the orchestration and the value of this particular candidate diversity;
they cannot establish general multi-agent superiority or LLM optimization quality.
Costs report zero external model calls; dollar/token costs are unavailable, not
estimated as free compute. Agent iteration counts are journaled by role.

Arm order is deterministically shuffled per trial/workload (seed 273 + trial index
+ count), and arms execute sequentially. Each benchmark has one warmup and 15
alternating paired samples. Analysis requires >=10 ms samples, MAD <=10%, native
execution, stable environment identity and all existing correctness/sanitizer gates.
The single arm's per-proposal alpha is .05/3; multi uses the cohort's three-candidate
Bonferroni correction. Benchmark alpha is .05/3 in both. Performance/memory weights
are .5/.5, minimum reliable weighted gain is 1%, and regression limits remain 5%.
Role-specific gates (including Memory's runtime guard) remain enabled. Different
boot/CPU/engine/native identities invalidate comparisons across candidates/arms;
load averages are recorded but naturally vary. Unqualified/noisy improvements do
not replace the original. Voting and deterministic tie breakers follow the
[selection policy](coordinator-selection.md).

Native x86-64 and ARM64 images support performance qualification. On this Apple
Silicon machine, select `linux-aarch64`; x86-64 emulation, RISC-V/QEMU and offline
CUDA compilation are not used to claim CPU/GPU performance gains. Measurement v2
uses a trusted root-owned native `wait4` launcher that drops to UID/GID 10001 before
executing submitted code. Its protected usage file excludes Python's inherited
pre-exec RSS. The three-process container limit permits supervisor, launcher and
program; the untrusted program's RLIMIT_NPROC=1 still blocks fork. Images without
measurement v2 are rejected by the evaluation runner.

## Evidence and interpretation

Each completed comparison writes `report.json`, `summary.csv` and a standalone
`report.html`, plus raw SQLite/CAS state, candidate audits, benchmark pairs and
cost ledgers. `implementation.zip` freezes the package code used by that run, with
a Python-tree SHA256 in the report. Compiler digest and complete host identity are
in verification/benchmark artifacts; newer runs also include them at report level.
The committed [local result snapshot](../benchmarks/results/step16/report.html)
contains the actual development-machine results, not simulated values. The release
archive includes full raw evidence when `--evidence` is supplied.

Speedup is baseline median / selected-candidate median for matched pairs. Memory
change is candidate median RSS / baseline median RSS - 1; negative means reduction.
Retained originals are 1x and 0%. Original absolute time/RSS pool matched baseline
samples across the experiment and need not equal a selected candidate's paired
reference. Correctness failures count failed verification **attempts** and benchmark program/
output failures, excluding infrastructure/unknown failures; repeated crashes can
count more than once. Benchmark infrastructure/measurement failures are separately
listed even when no complete timing observation exists. Raw
failure reasons, benchmarks and recovery attempts remain available for inspection.
Actual optimization elapsed time, calls, reserved budgets, child CPU time and
reported sandbox wall time are separate fields. Missing timing is explicitly
counted; sums with missing observations are labeled lower bounds. No monetary
cost, GPU timing or isolated-host performance claim is inferred.

This is one trial on a shared Docker Desktop VM, with two workload sizes from one
program family. It is an engineering milestone, not a statistically significant
between-strategy comparison. Retaining the baseline under noise is an expected
successful safety outcome. Calibration used seed 273 and remains in separate local
run directories; the official comparison used the held-out seed above. Acceptance
thresholds were not relaxed after failed calibration attempts.

## Demonstration and failure recovery

`./project demo` runs four scenarios through real RabbitMQ, Redis, verification,
benchmarking and coordinator selection, with assertions stored in `demo.json`:

1. A streaming reduction with a 64 KiB input buffer passes correctness and is
   selected only if reliable measured gains qualify (31 pairs).
2. A controlled compiler transport disconnect fails the first attempt; bounded
   recovery retries on a different healthy worker, then verifies/benchmarks the
   proposal. This is injected infrastructure failure, not a claim of physical GPU,
   host or broker recovery.
3. An infinite-loop proposal times out on two independent workers and is
   quarantined using the source/transformation/context blacklist. The original stays.
4. An incorrect-output proposal is rejected by mandatory correctness tests.

The demo exits nonzero if any assertion fails, including insufficiently reliable
performance on a busy host; it never forces acceptance. Each scenario saves its
raw audit, worker IDs, attempt history, cost ledger and recovery receipt. The local
release demo passed all four. Broader worker-kill, duplicate-delivery, broker-loss,
compiler-error and memory-exhaustion tests remain in the opt-in Docker test suite.

## Packaging and validation

`./project package` builds the Python wheel and source distribution, then creates
`dist/compiler-optimizer-demo.tar.gz` and its `.sha256` file. It bundles a curated
source allowlist, documentation, monitoring configuration, tests, built Python
distributions and optional completed/passed evidence directories. It excludes
`.env` secrets, VCS state, caches, virtualenvs and symlinks. Failed/incomplete
evidence and existing release destinations are rejected. To include raw results,
pass the actual directories printed by your completed runs:

```sh
./project package --output dist/my-release.tar.gz \
  --evidence var/evaluate/YOUR_COMPLETED_RUN \
  --evidence var/demo/YOUR_PASSED_RUN
```

The recorded local release archive is `dist/compiler-optimizer-step16.tar.gz`,
with a matching `.sha256` file. It includes the original comparison and demo
evidence; documentation corrections made after that release do not rewrite it.

Container images are rebuilt from included Dockerfiles; no VM, Docker daemon or downloaded image is
embedded. After extracting the archive, run `./project up`.

For full local integration validation after building the native, x86-64, RISC-V
and offline CUDA images:

```sh
BASELINE_DOCKER_TESTS=1 PIPELINE_INTEGRATION_TESTS=1 RECOVERY_INTEGRATION_TESTS=1 \
RISCV_DOCKER_TESTS=1 CUDA_COMPILE_TESTS=1 EVALUATION_DOCKER_TESTS=1 \
.venv/bin/python -m pytest -q -m docker
```

`./project test` runs unit/service tests. These include RPC tests that bind loopback;
a restricted execution environment may need local-network permission.
