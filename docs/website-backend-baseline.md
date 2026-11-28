# Local website backend baseline (W03)

This guide validates the backend required by the [local website specification](website-demo.md)
and [browser API contract](website-api.md). Run commands from the project root
(the directory containing the `project` executable). Python 3.11+ and a running
Docker engine with free disk space are required. No AWS account or model credentials
are needed for the default deterministic workflow.

## Reproduce the local run

In terminal 1, start the infrastructure, build the native compiler and run a
separate host coordinator with dedicated state:

```sh
mkdir -p var/website-demo/w03
STATE_DIRECTORY=var/website-demo/w03/coordinator MODEL_PROVIDER=none ./project serve
```

Keep this process running. `./project serve --no-start` is available when the
infrastructure and current native compiler image have already been checked.
Keep the same `STATE_DIRECTORY=var/website-demo/w03/coordinator` override when
restarting this run; `--no-start` does not select its state directory.
Do not use it to bypass failed dependency or toolchain checks. The host endpoint
is `127.0.0.1:50052`; its readiness endpoint is
`http://127.0.0.1:18083/readyz`. Require HTTP 200, `infrastructure_ready: true`,
`execution_enabled: true` and healthy RabbitMQ, Redis and storage checks. Readiness
alone does not establish native compiler capability; also verify the compiler
profile and the real task result. Compose port 50051 remains intake/infrastructure
mode with execution and transfer disabled and separate state.

In terminal 2, submit the bundled example with a persistent retry identity:

```sh
mkdir -p var/website-demo/w03/client
./project client submit examples/sum-array \
  --request-file var/website-demo/w03/client/request.json
```

Copy the printed task ID. Read status until `completed` or `failed`, then download
through the same RPC service:

```sh
./project client status TASK_ID
./project client download TASK_ID --output var/website-demo/w03/client/results
```

For bounded automatic waiting, repeat the submit command with the **same** request
file and add `--wait --wait-seconds 600`. Expiring the wait leaves the task running.
The receipt must retain the original task ID. Reopening status or retrying a read
must not create a new task. To deliberately start another run, use a new request
file. Each download destination must not already exist.

The client downloads `result.json`, `report.json`, `artifacts.json`, selected source
when available and raw task-owned artifacts. Existing client code verifies task
references, offsets, sizes and SHA-256 before publishing the result directory.
Inspect these downloaded files, not coordinator SQLite/CAS directories. The
request file holds stable metadata and a package digest, not source or credentials.

## Interpret readiness and failures

A failed `./project serve` is an infrastructure startup failure. Do not submit to
another port as a fallback or call it a program rejection. Check the failing
Compose service and compiler build output, resolve the dependency, and retry.

A submitted task can be accepted and then fail its baseline checks. The current
RPC reports lifecycle `status: failed`, `stage: failed`, with
`report_json.reason: baseline_rejected` and an available failure category. W02 maps
that evidence to browser `rejected`; no separate RPC `rejected` status exists.
Failure diagnostics remain downloadable even when selected source is absent.
Candidate rejection is distinct and can still lead to a successful task retaining
the original program.

An unavailable endpoint returns gRPC `UNAVAILABLE` and CLI exit code 1; an unknown
canonical task ID returns `NOT_FOUND`. Invalid task syntax is rejected by the
client before an RPC. These errors do not establish a task-level failure. Keep
accepted identities when retrying after a transient connection error.

Only native `linux-aarch64` or `linux-x86_64` profiles can qualify CPU measurements.
Model inference, GPU execution and cloud connectivity are outside this baseline.
The default task policy is frozen by the server; the browser cannot choose weaker
verification or measurement policies. Show only checks and measurements present in
task-scoped evidence. In particular, do not infer generated/fuzz tests from the
existence of a verification stage.

## Recorded validation — November 27, 2026 (America/Los_Angeles)

The [observed evidence record](evidence/website-demo-w03.json) contains UTC timestamps,
source revision, exact compiler/image identity, readiness, receipts, lifecycle
observations, result references and failure probes. It is a historical local run,
separate from W02's synthetic fixtures. Raw RPC downloads remain under
`var/website-demo/w03/client/`; the committed record contains their verified
identities and relevant summaries. Recheck readiness and native capability before
using this record to configure a deployment.

The run used Python 3.13.7 on macOS ARM64, a Linux/aarch64 Docker engine and the
native `linux-aarch64` profile with Clang 18.1.3. Operator settings were
`MODEL_PROVIDER=none`, local execution and local agent backends. The exact startup
command was the terminal-1 command above; submission used the non-waiting CLI and
status was polled about every two seconds. All client inspection used RPC or
files downloaded by RPC; no coordinator database or CAS directory was read.

The first startup failed before host intake: Docker's VM filesystem had no free
space, causing agent certificate copies to fail with ENOSPC. The native compiler
build also failed during package installation. After clearing unused Docker build
cache, the same startup command completed all Compose readiness and monitoring
checks and built the current native image. Application volumes were retained.
This infrastructure incident did not create or reject an optimizer task.

The fresh task **`b5c5ddbd-29b6-4e81-bb9c-e0cf3dc1cc44`** progressed through:

- `accepted / queued`
- `running / baseline`
- `running / optimizing`
- `completed / completed`

Its final report is schema version 1, `status: retained_baseline`,
`reason: no_reliable_improvement`. Performance and Architecture candidates passed
supplied differential fixtures and undefined-behavior sanitizer checks, received
Correctness/Critic approval, and were excluded as `benchmark_not_qualified`.
Memory generation abstained because this source lacks the includes required by
its streaming rule. The selected `main.c` exactly matches the bundled original.
The client downloaded 69 catalog entries (51 distinct, hash/size-verified objects).
Repeating submission preserved the task ID and accepted message ID; a new receipt
has its own receipt message ID and timestamp.

The successful status response exposed `task_id`, `status`, `stage`, `report_json`,
`report` and `selected_source`. Detailed checks, reviews, paired runtime/memory
statistics, uncertainty and compiler context were read from downloaded task-owned
reports. Artifact sizes in protobuf JSON are strings and must be parsed safely.
Native capability was verified, but neither candidate qualified for selection.
No general speedup or C/C++ optimization claim follows from this tiny example.

Two integration limits matter for W04:

- The default verification plan in this run had no generated or coverage-guided
  fuzz cases. Only the supplied `empty`/`mixed` checks and UBSan evidence support
  the correctness display. Reviewer summary wording does not add tests that are
  absent from the actual check list.
- Both generated proposals omitted `agent_provenance`. The operator configured
  deterministic local rules, but task-scoped model/provider identity is unavailable.
  Follow W02's `backend: unavailable` fallback when task evidence cannot establish
  identity; do not synthesize live model activity from service configuration.

A second accepted task, **`ea8826fb-d499-4c36-a344-4241dc58b746`**, used a copy of
`sum-array` with `tests/mixed.out` replaced by deliberately incorrect text. It
terminated as `failed / failed`, with `reason: baseline_rejected` and
`failure: OUTPUT_MISMATCH` in `report_json`. Four verified diagnostic objects were
downloaded; no selected source was present. This establishes W02's rejected-state
mapping without changing the trusted bundled example. To repeat that probe:

```sh
.venv/bin/python - <<'PY'
import shutil
from pathlib import Path
package = Path('var/website-demo/w03/client/rejected-package')
shutil.copytree('examples/sum-array', package)
(package / 'tests/mixed.out').write_text('intentionally incorrect expected output\n')
PY
./project client submit var/website-demo/w03/client/rejected-package \
  --request-file var/website-demo/w03/client/rejected-request.json --wait
```

Use that receipt's task ID with `status` and `download` as above. Existing probe
paths must be reused for receipt recovery or replaced with new paths for an
intentional fresh run. The existing output directories are never overwritten.

The other recorded probes were:

```sh
./project client status ffffffff-ffff-4fff-8fff-ffffffffffff
./project client --endpoint 127.0.0.1:59999 --timeout 1 status TASK_ID
./project client status invalid-task-id
```

They returned CLI exit code 1 with `NOT_FOUND`, `UNAVAILABLE` and client-side
`ValueError`, respectively. Port 59999 was unused during validation. These remain
separate from the accepted task's baseline rejection. Browser routes, additional
targets, real GPU execution, live model calls and AWS remain unvalidated here.
