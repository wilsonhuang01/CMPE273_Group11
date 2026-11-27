# Local website demo specification (W01)

Defined November 27, 2026. This specifies the first local website milestone;
the browser UI and Python API are planned in
[W02–W11](../backlog/website-demo.html). It does not record a successful live
website run. W03 will establish the reproducible backend baseline.

## Initial example and execution mode

The initial allowlist contains one example: **`sum-array`**, labelled “Integer
array sum (C17)”, from [its manifest](../examples/sum-array/submission.json) and
[source](../examples/sum-array/main.c). Preview `main.c` as escaped, read-only
text. Describe its input as a count from 0 to 100,000 followed by that many
integers in [-1,000,000, 1,000,000], and its output as their sum. Its bundled
`empty` and `mixed` tests establish the starter submission; candidate verification
adds the coordinator's frozen checks. Only `mixed` is a manifest benchmark input.
No second example or C++ demo is required for this milestone, although the
backend supports a bounded C17/C++17 subset.

Use plain HTML/CSS/JavaScript in `web/` and a Python FastAPI adapter in
`src/compiler_optimizer/web/`. Serve assets and the browser API from one origin.
The adapter uses the existing `OptimizerClient` and task-scoped transfer RPCs;
the coordinator owns execution, recovery and selection. The local endpoint is
`127.0.0.1:50052` (`./project serve`), as described in
[automatic execution](automatic-execution.md). Compose port 50051 is intake-only
by default. The adapter never reads coordinator SQLite/CAS directories or invokes
the compiler directly. These web directories and commands remain future work.

Offer only the deployment's available **native Linux CPU** target: `linux-aarch64`
on an ARM64 Docker engine or `linux-x86_64` on an x86-64 engine, after backend
capability validation. Show the resolved target and qualification status before
submission; an unavailable target disables Run with a reason. Do not infer
readiness from the browser's architecture. Native ARM64 is the recorded evaluation
platform; an emulated amd64 container on Apple Silicon cannot qualify speedups.
See [architecture limits](architectures.md).

The default demo uses deterministic rules/evidence checks (`MODEL_PROVIDER=none`)
and must say “Deterministic rules; no live model calls.” Replay means recorded
model replies with no live inference. Bedrock is an optional operator-configured
mode, requiring credentials and readiness; label it model-backed only when
task-scoped provenance supports that claim. Missing provenance displays
“Backend identity unavailable.” The browser cannot enable providers or receive
credentials. All modes retain the same verification and selection gates; see
[model-backed agents](model-agents.md).

## Screens and states

1. **Choose and preview.** Select the bundled example, read its description and
   source, inspect target/backend capability information, and activate Run.
   Show loading, ready, empty/unavailable catalog and backend-unavailable states.
   Do not accept source edits, arbitrary paths or package uploads.
2. **Submit.** Persist a stable request identity before sending. Disable duplicate
   clicks while pending. An accepted receipt leads to a task permalink displaying
   the task ID. An uncertain receipt offers reconciliation using the same identity;
   it must not silently create a new run. Invalid example/target requests display
   an actionable rejection, distinct from service unavailability.
3. **Follow the task.** Show queued and running states, task ID and only the stage
   supplied by the backend. If stage detail is absent, say it is unavailable.
   Refresh/reopening the permalink resumes reads of the same task. Disconnection
   or polling timeout preserves that ID and shows the last known state as stale;
   Retry retries the read. Unknown-task, rejected and failed states explain the
   available reason. Never invent percentages, agent activity or recovery events.
4. **Inspect the result.** A completed run shows either “Candidate selected” or
   “Original retained”, supported by the final decision and evidence. Baseline
   retention is a successful, valid outcome, expected for this tiny workload when
   reliable gain cannot be established. Distinguish baseline/submission rejection
   and exhausted infrastructure failures from candidate rejection: a rejected
   candidate can still lead to a completed baseline-retained task.
5. **Download.** Offer selected source (including the original when retained) and
   the report/evidence bundle only when registered artifacts exist. Show pending,
   downloading, verified success, unavailable and failed-transfer states. A failed
   task can expose diagnostics without selected source. Retry keeps the task ID.
   An explicit New run returns to submission with a fresh request identity.

Every screen must work by keyboard and on a narrow screen. Announce changing
status and errors as text; color alone must not convey state. Source is rendered
as text, and downloads are attachments with existing size/hash checks. Reloading
or a coordinator restart must not automatically resubmit work.

## Required result information

The result view must include these semantic fields; W02 defines their versioned
browser names, error codes and representative fixtures:

- Identity and lifecycle: task ID, example, target, execution status, available
  stage, and failure/rejection reason. Keep connection state separate from task
  state and final selection outcome.
- Decision: candidate selected or baseline retained, selected source identity/hash,
  decision rationale and available proposal/reviewer outcomes with rejection or
  exclusion reasons.
- Correctness: available fixed/generated differential and sanitizer outcomes,
  review decisions, and links to task-scoped evidence. Say which checks ran;
  passing tests does not prove equivalence for every input.
- Measurements: baseline and candidate runtime and peak-memory statistics with
  units, available repetitions/uncertainty, and reported gain/qualification decision.
  Include target/native-or-emulated context, worker/hardware and compiler/image
  identity when recorded. Never substitute zero for a missing value, calculate
  a claim from incomparable measurements, or promise a speedup.
- Provenance and artifacts: available rule/replay/model identity, final report,
  selected source and evidence download availability. Missing evidence is labelled
  unavailable; a task status alone cannot support detailed measurement claims.

The existing [`OptimizationResult`](../src/compiler_optimizer/shared/v1/contracts.proto)
provides `task_id`, `status`, `stage`, `report_json`, optional `report` and optional
`selected_source`. Detailed decisions, checks and measurements require the
task-scoped report/artifact contents, not extra fields presumed to exist on that
RPC. W02 must map real backend states (including rejection summaries) to browser
states and record any necessary extensions. No live per-agent timeline is required.

## Scope and acceptance boundary

The milestone is complete when a user can choose `sum-array`, preview its source,
submit once, reopen its task, inspect a truthful terminal decision and evidence,
and download verified selected source or available failure diagnostics. A retained
original satisfies the successful journey. W03 records backend readiness; W11
validates the browser journey, including disconnection, rejection, failure,
baseline retention and unavailable measurements.

Package upload is optional W08 work. Arbitrary/general C/C++ optimization, source
editing, custom compiler flags, real GPU execution, emulated performance claims,
RISC-V/CUDA/LLVM/MLIR browser workflows, cancellation controls, live model setup,
multi-user tenancy and AWS deployment are outside this initial journey. Existing
CLI capabilities do not imply browser support. Historical recordings, when added,
must be explicitly labelled with their environment/revision and kept separate from
live tasks. W12–W14 handle AWS connectivity and compatibility after deployment
handoff; this specification requires no AWS account.
