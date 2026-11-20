# Coordinator selection

The coordinator collects a frozen cohort's reviews and measured results, excludes
failed/ineligible candidates, ranks reliable weighted improvements, and commits
one immutable decision. The original program is selected when none qualifies.
An invalid reference fails the task and produces no verified selection.

## Run a cohort

Start the [local infrastructure](local-infrastructure.md), activate the development
environment and build the x86-64 image for this manual example.
`./project up` selects the native toolchain for the complete local stack; this
example explicitly builds x86-64 because the default Architecture starter requires it.

```sh
docker compose --profile toolchain build compiler
python -m compiler_optimizer.coordinator.round \
  --package examples/sum-array --agents performance,architecture \
  --policy examples/selection-policy.json \
  --verification-plan examples/sum-array.verification.json \
  --state var/selection
```

Each proposal travels through confirmed durable RabbitMQ delivery, the existing
verification/sanitizer stage, required reviews, Benchmark Agent planning, and
normal-binary measurements. The consumer acknowledges each committed candidate
evaluation. Selection waits for every declared member; the first successful
proposal does not complete the task. Processing is serial under the existing
local state-directory lock.

The role list defaults to Performance, Memory and Architecture. Starter backends
are deterministic and support exact bundled examples. On original sum-array,
Memory intentionally refuses to invent a memory optimization for an already
streaming implementation; that attempt appears as `generation_failed`. Choose
appropriate roles/workloads or provide `--patch memory=response.json` (repeatable
for other roles). Patch bytes are frozen as artifacts, so a changed file cannot
silently change a resumed cohort. Trusted Python integrations can supply
`CohortCoordinator(..., backends={...}, review_backends={...})`; an unfinished
custom generator needs its adapter again on resume.

Resume by task ID without changing the frozen settings:

```sh
python -m compiler_optimizer.coordinator.round --task-id TASK_ID --state var/selection
```

The single-proposal `coordinator.pipeline` command also applies weighted selection
to new workflows and accepts `--selection-policy examples/selection-policy.json`.
Older pending workflows retain their original selection contract. New single
workflows store policy intent atomically with the proposal before freezing the
cohort, closing the interruption window between those operations.

Exit 0 means a candidate or the baseline was selected; exit 1 means reference
failure; exit 2 means invalid input or an operational interruption. A cached result
returns without generation, execution, publication or rescoring. A resumed cohort
reuses committed evaluations and measurements. Invalid generation/review outputs
exclude that candidate; transient storage/broker/provider exceptions remain
resumable. There is no unbounded automatic retry or policy search.

## Policy and voting

`examples/selection-policy.json` defines explicit defaults:

- Performance weight **0.70** and memory weight **0.30**. Finite nonnegative weights
  must sum to one (within 1e-12); zero allows a single-metric policy.
- Minimum weighted gain **0.01** (one percentage point) on every workload.
- Family significance **alpha = 0.05**, corrected across all planned candidates
  and workloads. Failed, unsupported and duplicate candidates still count in this
  correction; their removal cannot relax the threshold.
- Six decimal places for deterministic score ties, at most six planned candidates.
  The bundled runner assigns one candidate per requested generating role.

Unknown/duplicate fields, nonfinite/bool numeric values and out-of-range limits
are rejected. Policy JSON is capped at 16 KiB. Freeze happens before any candidate
verification job or measurement. Role assignments, verification plan and normalized
measurement settings are frozen together. All candidates use the same baseline,
compiler, tests, generated-input policy, sampling counts, seed, CPU request and
measurement thresholds. Memory plans retain their memory objective; the others
retain their runtime objective.

Voting is unanimous **2/2** among the named Correctness and Critic roles:

- Each role contributes exactly one bound verdict for that proposal's exact
  verification job/attempt. Duplicates never create additional votes.
- `reject`, `abstain`, missing or invalid review evidence blocks that candidate.
- Generators do not vote for their own proposals. Benchmark Agent supplies work
  specifications and measurements, not an extra discretionary vote.
- Reviews cannot override failed tests, sanitizer failures, resource violations,
  unsafe-reference evidence or measurement gates. Two approving roles do not
  imply independent model providers or a formal equivalence proof.

Generation remains limited to two attempts per task/generating role. To support
multiple candidates, reviewers now have two attempts **per proposal**, and Benchmark
Agent has one planning attempt **per proposal**. Their global iteration sequence
still preserves audit order. Completed identical operations are cached. The
cohort cap bounds total work; backend responses cannot raise these limits.

## Eligibility and scoring

A candidate must have current, stored correctness evidence, both approving
reviews, exact source/patch/compiler bindings, and frozen Benchmark Agent work.
Its role-specific benchmark must independently qualify under the existing noise,
duration, memory/runtime regression, native-worker and environment gates. Failed,
incomplete, missing or corrupted evidence cannot become a positive score. Known
non-infrastructure verification failure disqualifies that source snapshot even
if another job previously passed. A benchmark execution failure also disqualifies
the source. Infrastructure-only failure does not supply successful measurements.

Ranking uses the same original baseline binary and measurement environment for
all admitted comparisons. The first eligible candidate in declared cohort order
establishes that comparison identity; another CPU/engine/boot/governor or changed
baseline binary is excluded rather than compared across machines. Environment
load averages remain diagnostic. Identical source snapshots have one ranking
place and no additional votes.

For each measured baseline/candidate pair:

```text
performance_gain = 1 - candidate_seconds / baseline_seconds
memory_gain      = 1 - candidate_peak_RSS / baseline_peak_RSS
weighted_gain    = performance_weight * performance_gain + memory_weight * memory_gain
```

Negative gains penalize regressions; metrics are not clipped. Warmups are excluded,
all measured pairs are kept, and each workload receives equal weight. The point
score is the mean across workloads of their median paired weighted gains. Runtime
and memory are combined within each pair before taking the median, preserving
their observed tradeoff rather than combining unrelated best samples.

Reliability uses an exact one-sided sign test of weighted gains strictly above
the configured minimum, plus an order-statistic lower confidence bound on their
population median. With `n` pairs and `w` wins, the p-value is
`sum(comb(n,j), j=w..n) / 2**n`. Per-workload alpha is
`policy.alpha / (planned_candidate_count * workload_count)`. Choose the largest
rank `k` satisfying `sum(comb(n,j), j=0..k-1) / 2**n <= per_workload_alpha`; the
gain at sorted rank `k` is the lower bound. Every workload's bound must exist and
exceed the minimum, and its p-value must pass. Ties at the minimum count against
acceptance. Too few samples for the declared cohort/workloads retain the baseline.

The ranking's primary score is the mean of these per-workload lower bounds.
Consequently a large but uncertain gain cannot outrank a smaller well-supported
gain merely through its point estimate. Statistical interpretation assumes
independent pairs under stable execution conditions; it applies to the declared
workloads/cohort, not universally to every input or repeated experimental search.
The original role-specific benchmark policy remains an additional hard gate.

## Tie breakers

Compare, in this order:

1. Higher mean weighted lower confidence bound.
2. Higher weighted point score.
3. Higher worst-workload weighted lower bound.
4. Lower worst-workload peak-memory ratio (candidate maximum / baseline maximum).
5. Lower worst-workload median-runtime ratio.
6. Lexicographically smaller source SHA-256, then proposal UUID.

Numeric keys are rounded to `tie_digits` decimal places with decimal half-even
rounding before comparison. This defines transitive tie buckets; it does not use
a nontransitive pairwise epsilon. The audit retains full-precision scores. Source
hash/UUID fallbacks make exact ties reproducible without rewarding role names,
arrival order or author votes. Baseline score is zero, and no candidate lacking
reliable positive weighted gain can displace it.

## Durability and results

Schema v5 adds immutable selection sessions and per-candidate evaluation
checkpoints, migrating existing v1–v4 data transactionally. Policy/cohort changes
conflict after freezing. Only one completed benchmark batch per candidate is
allowed; a repeat is rejected before execution, with a second ingestion guard.
[Fault recovery](fault-recovery.md) permits bounded retries of infrastructure-failed batches under an active
[recovery lease](fault-recovery.md). Completed, noisy or program-failed batches
cannot be replaced, and retries preserve the frozen plan and successful verification.
Once evaluated, a candidate cannot receive new verification jobs/results to
replace its evidence. An interrupted uncommitted stage may be retried under the
earlier watchdog/iteration limits; this is not exactly-once distributed execution.

The repository independently recollects and validates evidence and recomputes
ranking inside the same SQLite transaction that commits the decision, numeric
scores, task completion/history and content-addressed selection audit. A caller
cannot provide a different winner, arbitrary scores or a late policy. Failure
rolls all database changes back together; artifact publication can leave an
unreferenced blob, as in the existing storage design. Failed references produce
an immutable blocked audit and no successful decision.

The returned audit lists policy/weights, voting and tie rules, all candidates,
votes, exclusions, measured/weighted statistics, evaluation/benchmark artifact
references, ranking and selected source/job. `repo.selection_report(task_id)`
returns its artifact reference and JSON. `GetOptimizationResult` serves it through
the [local coordinator API](performance-pipeline.md#retrieve-results-through-the-coordinator)
against the same state directory. RPC `status` reflects task lifecycle
(`completed`/`failed`); cohort audit status describes the choice
(`selected`, `retained_baseline`, `blocked`). New single-pipeline reports include
the same audit under `selection`.

```sh
python -m compiler_optimizer.coordinator.results --state var/selection --port 50052
python -m pytest tests/test_selection.py
PIPELINE_INTEGRATION_TESTS=1 python -m pytest tests/test_selection_docker.py
```

Unit coverage includes multiple weighted winners, veto/abstention and failed
candidates, unsupported generation, confidence correction, ties, environment
mismatch, source deduplication, immutable policy/measurements, interrupted delivery,
atomic rollback and prevention of direct selection bypass. The opt-in test runs
two candidates through actual RabbitMQ/Docker and retrieves the audited baseline
fallback over gRPC. Native-only ASan remains a separate worker requirement.
