# Specialized agents

The [cohort coordinator](coordinator-selection.md) implements multi-candidate weighted selection.
Generation budgets remain per task; review and Benchmark planning budgets are
now per proposal so each cohort member can be evaluated independently.

The existing Performance workflow now supports Memory and Architecture candidate
generators, required Correctness and Critic reviewers, and a Benchmark Agent that
organizes measurements. All six roles are implemented and covered by role-specific
and end-to-end tests. Default backends are deterministic rules/evidence checks. They do not call
an external LLM unless [model-backed agents](model-agents.md) are configured. Trusted Python backend adapters can supply generated patches or
reviews; their structured outputs pass the same validation and acceptance gates.

## Run a generator

Start RabbitMQ/Docker as described in the [pipeline guide](performance-pipeline.md).
The Performance Agent remains the default. The single-proposal pipeline below
accepts one generator; the [cohort workflow](coordinator-selection.md) supports
multiple generators with [automatic recovery](fault-recovery.md):

```sh
python -m compiler_optimizer.coordinator.pipeline \
  --package examples/sum-array-buffered --agent memory --state var/memory \
  --verification-plan examples/sum-array.verification.json \
  --benchmark-plan examples/sum-array.memory-benchmark.json

python -m compiler_optimizer.coordinator.pipeline \
  --package examples/sum-array --agent architecture --state var/architecture \
  --verification-plan examples/sum-array.verification.json
```

Correctness, Critic and Benchmark agents run automatically. Review rejection or
abstention keeps the verified baseline and prevents measurements. Reference failure
fails the task. Approval never replaces fixed/generated tests, sanitizer checks or
statistical qualification. The repository repeats these checks at measurement
ingestion and candidate selection, including when called outside the coordinator.

The tiny fixtures validate wiring, not memory/performance gains. For meaningful
Memory Agent measurements, use the native ARM64/x86-64
[evaluation workflow](evaluation.md). For the standalone x86-64 pipeline on an
idle native amd64 worker, generate a larger input:

```sh
python tools/create_memory_workload.py --output var/memory-workload --count 1000000
python -m compiler_optimizer.coordinator.pipeline \
  --package var/memory-workload --agent memory --state var/memory-large \
  --verification-plan examples/sum-array.verification.json
```

The generator refuses existing output directories and caps inputs at one million
integers. Memory mode defaults to the v2 memory plan, so its plan argument is
optional. The standalone command above uses x86-64 and cannot qualify when
emulated on ARM. Native ARM64 measurements use the target-aware cohort/evaluation
workflow and can qualify.

## Inputs, outputs and iteration limits

The operator-owned contracts live in `shared/agent_roles.py`. Limits apply per
task/generating role or proposal/reviewing role, not per process. The original
single-proposal workflow delivers one candidate; the cohort runner collects
several. These limits bound backend/planning attempts and do not introduce automatic debate or
revision loops.

- **Performance:** assignment, immutable parent source and compiler/manifest in;
  a source-only exact-text patch, rationale and versioned `Proposal` out. Two
  generation attempts. Its existing example rule increases stdio buffering.
- **Memory:** the same assignment/source contract in; a memory-oriented patch and
  rationale out. Two attempts. The starter recognizes the exact buffered-array
  example and replaces retained storage with a bounded integer streaming reduction,
  removing O(count) array storage. It states the input/overflow and baseline-memory
  assumptions explicitly. Unsupported sources require a patch backend.
- **Architecture:** assignment, source and fixed target/compiler in; a portable
  source patch and rationale out. Two attempts. The starter recognizes original
  sum-array and exposes independent accumulators. It cannot change flags, target,
  tests or fixtures, and makes no unmeasured speedup claim. No CUDA, RISC-V, SIMD
  intrinsics, native-CPU flags or architecture-specific assembly are introduced.
- **Correctness:** proposal, stored job/result, fixed/generated coverage, log,
  plan/input hashes and correctness report in; a bound `approve`, `reject` or
  `abstain` review with findings out. Two review attempts. It revalidates evidence
  before consulting a backend/cache. A failed test or sanitizer run cannot receive
  correctness approval, including from a custom backend.
- **Critic:** proposal, baseline/candidate source, rationale and verification
  context in; an independent bound review out. Two attempts. The default flags
  newly introduced process/network/file operations, nondeterminism, inline assembly
  and unchecked compiler/alias assumptions. It ignores names inside ordinary C
  comments/literals and warns about unsupported certainty claims. This is a
  conservative source heuristic, not a complete C/C++ parser or a proof of safety;
  macros/obfuscation and semantic mistakes still require tests, sandbox limits and
  appropriate human review. It never executes candidate code.
- **Benchmark:** verified/review-approved proposal, exact normal binaries,
  original package/manifest and frozen benchmark plan in; a deterministic versioned
  measurement-work record out. One planning attempt. The record identifies binaries,
  workloads, objective, warmups, pairs, limits, CPU requirements and maximum invocation
  count/wall budget. Workers execute that plan; the agent cannot weaken it or invent
  performance results. Failed tests/reviews cannot create measurement work.

Failed and interrupted attempts count toward the limit. Completed identical work
is cached and reused without consuming another iteration; changing its inputs
conflicts. Exhausted budgets require a new task; there is no hidden reset or
unbounded automatic retry. Each backend invocation is an iteration, not each
benchmark sample. Benchmark execution remains bounded by the frozen work and the
existing per-invocation watchdogs. Legacy single-proposal resumption is documented
in the [pipeline guide](performance-pipeline.md). Frozen cohort workflows retry
only infrastructure-failed batches under a [recovery lease](fault-recovery.md);
completed or noisy batches cannot be resampled.

Generation retains the existing 1 MiB patch, 32-edit, 16 KiB rationale and 10 MiB
package bounds. Reviews are at most 64 KiB, with a 4 KiB summary and 64 bounded
findings. They bind task, proposal/source hash, exact verification result digest,
job/attempt and correctness artifact. Invalid/mismatched output fails closed and
consumes its attempt. All backend implementations are trusted application code;
source text and generated responses remain untrusted data.

## Backend extension points

Generators implement `PatchBackend.generate(request, sources) -> bytes` using the
[structured patch schema](performance-pipeline.md). `--patch response.json` works
with any of the three generating roles and does not bypass role/objective checks.

Correctness backends implement `review(context, correctness_evidence)`; Critic
backends implement `review(context, baseline_sources, candidate_sources, rationale)`.
Return only `verdict`, `summary` and `findings`; identity/evidence fields are added
and checked by the agent. A finding has `code`, `severity` (`info`, `warning`,
`error`) and `message`. Approval cannot contain blocking findings. Rejection needs
an error; abstention also blocks progression. Configure trusted Python adapters via
`PipelineCoordinator(..., review_backends={"correctness": backend, "critic": backend})`.
Reviewers cannot generate runnable candidates or alter test/benchmark policy.

## Memory qualification

Existing v1 runtime plans and their reports remain readable. V2 adds `objective`
(`runtime` or `memory`) and `maximum_runtime_regression` (default 5%). Generator
roles fix their objective: Memory uses memory; Performance/Architecture use runtime.
The plan is validated before spending a generation iteration.

For memory qualification, paired gain is `1 - candidate_RSS / baseline_RSS`.
The minimum gain defaults to 5%. All workloads must have a significant paired
sign test and a median-gain lower bound above that threshold. A one-sided upper
bound on median paired runtime regression, plus the ratio of runtime medians,
must stay within the runtime budget. Runtime and memory relative MAD checks,
minimum duration, peak-memory regression, environment stability and native-worker
gates still apply. Alpha is divided across both claims and every workload
(`alpha / (2 * workload_count)`). No samples are discarded. Statistical claims
assume independent pairs on a stable worker and concern these measured workloads.

Peak child RSS includes executable/runtime overhead and can mask small heap
savings. The pipeline reports inconclusive/tied results and retains the baseline
rather than substituting source-level allocation estimates for measurements.

## Persistence, results and compatibility

Schema v4 transactionally adds `agent_iterations` to v1/v2/v3 databases. Each
iteration preserves immutable input/output artifact links, role, operation ID,
sequence and started/completed/failed state. Inspect `repo.list_agent_iterations`
and `repo.list_artifacts`. Review and work outputs also appear in final coordinator
JSON and `GetOptimizationResult`.

New pipeline configuration/report schema v2 requires both reviews and frozen
measurement work. Existing queued schema v1 workflows resume under their original
contract; committed old reports remain unchanged. Source/compiler/test contracts
are unchanged. Broker queues use the generating role prefix, with the same durable
confirmed/acknowledged delivery behavior of the single-proposal pipeline. Cohort
workflows add worker leases, bounded retries and scoped quarantine. Multi-host scheduling,
provider authentication and additional optimization rounds remain future work.

```sh
python -m pytest tests/test_agents.py tests/test_pipeline.py tests/test_benchmark.py
PIPELINE_INTEGRATION_TESTS=1 python -m pytest tests/test_agents_docker.py
```

Tests cover role-specific generation and objectives, persistent budgets, review
context swaps/corruption, false approvals, abstention, risky changes that pass
tests, repository bypass attempts, frozen measurement work and legacy resumption.
Real RabbitMQ/Docker tests exercise each of the five roles.
