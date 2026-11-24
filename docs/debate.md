# Bounded proposal revision rounds (action H)

Debate is opt-in. A rejected proposal can receive Correctness/Critic findings and
automated failure evidence, produce a revised patch and pass through the full
verification/review/measurement pipeline again. Accepted candidates are not
revised in response to timing noise or benchmark scores.

## Run

Create an operator-owned policy file, for example `var/debate-policy.json`:

```json
{
  "schema_version": 1,
  "max_rounds": 2,
  "max_agent_calls": 32,
  "max_seconds": 1800,
  "heldout_cases": [{"stdin": "7\n1 2 3 4 5 6 7\n", "args": []}]
}
```

Choose held-out inputs valid for your program's declared domain. They must differ
from the fixed/generated search verification inputs; duplicate held-out inputs
are rejected. The example is suitable for the repository's integer-array sum
program. Policies allow 1–6 rounds, 1–128 agent operations, 1–86,400 seconds and
1–32 held-out inputs, with bounded input and argument sizes.

Run with the existing broker, trusted compiler image and configured agents:

```sh
.venv/bin/python -m compiler_optimizer.coordinator.round \
  --package examples/sum-array --state var/debate \
  --agents performance --debate-policy var/debate-policy.json
```

Use the existing `--target`, `--image`, verification/benchmark/selection policy,
worker/recovery and RPC configuration options as needed. `--task-id TASK_ID`
resumes the frozen session without passing policies again. `--cancel --task-id`
uses the existing durable cohort cancellation mechanism. Resume uses the original
custom backends, local/RPC transport and recovery controller.

For automatic submissions, set `DEBATE_POLICY_JSON` on the coordinator to the
policy JSON (or add `debate_policy` to its operator-owned workflow configuration).
The accepted submission freezes that policy. It is never selected by client or
model output. In `.env`, enclose the entire JSON value in single quotes so the
project CLI preserves JSON quotes. Compose forwards this setting to the
coordinator only. An unset value retains the original one-round cohort behavior.

For live models, enable action G's [model agent configuration](model-agents.md)
and use RPC. The model receives criticism inside untrusted-evidence framing.
Deterministic starter generators may repeat their original patch; this stops as
no progress. Custom backends receive `OptimizationRequest.revision_context_json`.

## Frozen search and lineage

All candidate slots for every role/round are declared before any candidate is
evaluated. The existing six-candidate selection limit applies to the total:
three roles can run two rounds, or one role can run up to six. Selection's paired
statistics divide alpha across **all reserved slots**, including unused rounds,
and benchmark cases. Verification settings, benchmark settings, compiler, target,
required reviewers and selection weights remain frozen.

Each child has a durable logical parent proposal ID and source hash. Its feedback
contains the parent's exact patch, bound reviews and automated failure evidence.
Patches are **rebased against the immutable original source**, so the existing
independent original-versus-candidate tests, Critic checks and measurements stay
valid. The child's `SourceVersion` therefore names the original baseline as its
patch parent; the debate lineage table records the preceding logical revision.
The generator must include any retained parent changes in its complete patch.

Identical source snapshots stop before repeated verification or measurements.
Repeated unchanged actionable criticism also stops the next revision. A parent
with no generated patch or no usable criticism stops its chain. Benchmark scores
and held-out inputs/results never enter revision feedback. Oversized feedback is
not silently truncated; the chain stops.

## Durable budgets and final evaluation

SQLite schema version 12 adds `debate_runs`, `debate_slots` and `debate_events`.
`Repository.debate_transcript(task_id)` returns source lineage, immutable revision
requests, reviewer findings, verification evidence, completion and stop records.
Associated artifacts are task-scoped and included by normal artifact transfer.

Each uncached agent operation atomically consumes the debate's shared call budget.
Generation, both reviewers and Benchmark planning count; failed/interrupted
attempts retain their charge. Cached operations are free. The original per-operation
attempt limits and action G's separate provider call/token budgets still apply.
The absolute deadline survives restart; RPC timeouts use the remaining debate
time. No new stages start after the limit is observed. Active local sandbox work
drains under its existing compile/run/watchdog limits, so the deadline is an
admission limit rather than a promise to kill a host process at an exact instant.

Completed measurements are reused through existing immutable progress and
one-shot measurement claims. Restarts never create a new measurement budget for
the same source. A lost broker acknowledgement reuses the committed result.

After search, only the provisional winner receives the frozen held-out corpus.
The trusted runner compares its output to the original baseline with the frozen
output comparator. The final record is attached as `heldout-evaluation`; it is
required before selection can commit a candidate. No optimization/benchmark gain
is inferred from these correctness checks. If the final check fails, times out or
is interrupted, the baseline is retained. The final candidate and one-shot claim
are durable: there is no retry or next-candidate search on the held-out set.

The corpus is withheld from generator/reviewer prompts during this task, not a
secret from the operator or authenticated task owner. Keep a separate corpus for
later scientific evaluation across many tasks; repeated human reuse of a held-out
set can still cause overfitting.
