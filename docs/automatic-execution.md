# Automatic submission execution

Start the trusted host coordinator with Python 3.11+ and Docker running:

```sh
./project serve
```

This starts/checks the existing infrastructure and native compiler image, then
runs the coordinator in the foreground. Use `./project serve --no-start` to reuse
a healthy stack. Defaults are `127.0.0.1:50052` for gRPC, `127.0.0.1:18083` for
HTTP health/metrics, and `var/service` for state. `GRPC_PORT`, `HEALTH_PORT`,
`GRPC_HOST`, `HEALTH_HOST` and `STATE_DIRECTORY` override these values. The existing Compose
coordinator remains on port 50051, with its separate persistent volume and
`EXECUTION_ENABLED=false`: its image has no Docker CLI/socket. Submit automatic
work to the host endpoint on 50052. Neither its state nor previously submitted
Compose tasks are moved by this command.

The host coordinator is trusted infrastructure with Docker access. Its
[package upload and artifact API](artifact-transfer.md) lets a client use a
separate filesystem without Docker or coordinator database access:

```sh
./project client submit examples/sum-array --request-file var/client/request.json --wait
./project client status TASK_ID
./project client download TASK_ID --output var/client/results
```

Use the task ID printed by submission for status and download. A completed
selection supplies the audit report, selected source and task-scoped evidence.
No optimization CLI command is needed after intake. The tiny example commonly
retains the baseline because it cannot establish reliable performance.

Loopback is the development default. Remote execution/transfer binds require TLS
and bearer authentication; see the [transfer guide](artifact-transfer.md) for
configuration, retry identities, limits and output layout.

## Durable acceptance and restart behavior

`SubmitSubmission` validates the manifest/package/source binding and commits both
the submission and its execution row in one SQLite transaction before returning a
receipt. An identical message returns another receipt without changing policy,
creating another task or resetting the attempt budget. Existing manual baseline
and cohort commands do not enqueue automatic work.

The execution row freezes the generating roles, verification/benchmark/selection
and recovery policies, total dispatcher attempt cap and retry delay. Defaults
use the three deterministic generating roles and existing mandatory review and
qualification gates. Unsupported starter rules are recorded as generation
failures; they do not bypass selection or invent proposals. Operator-controlled
`workflow_configuration` on `CoordinatorService` can select a smaller cohort or
explicit validated plans before intake. Clients cannot alter policies in the RPC.

The dispatcher polls durable pending rows once per second and runs each task under
an expiring database claim, renewed while it works and checked by every write
([dispatch claims](cloud-storage.md#dispatch-claims-and-fencing-phase-3)). It preserves
the submitted task ID. After a restart, it confirms cleanup of that task's owned sandbox
containers, reuses a committed successful baseline and resumes the frozen cohort,
agent journal, recovery leases and final audit. Completed/noisy benchmark batches
cannot be resampled. An interrupted baseline starts a new job attempt; it never
overwrites partial attempt artifacts. A crash after final selection only requires
settling the execution row, without recompilation or measurement.

Restart the server with the same state, toolchain image and service dependencies:

```sh
./project serve --no-start
```

Task rejection is terminal. Infrastructure failures wait five seconds before a
dispatcher retry, with three total dispatcher attempts, including interrupted
attempts across restarts. Candidate recovery has its own frozen bounded policy.
Exhaustion marks the task failed and remains visible in the result summary; it
never selects an unqualified candidate. A process loss may cause repeated
uncommitted execution, but committed results remain idempotent. If another local
orchestrator holds the state lock, dispatch waits without consuming an attempt.

The direct runtime command also enables automatic dispatch by default for the
coordinator role. `EXECUTION_ENABLED=false` disables dispatch; `TRANSFER_ENABLED=false` disables upload/download.
The [scheduler and worker roles](autonomous-workers.md) now support independent
execution services with `EXECUTION_BACKEND=remote`. The default Compose deployment
keeps infrastructure-only mode. Parallel dispatch and model backends remain separate work.

## Validation

```sh
.venv/bin/python -m pytest -q tests/test_dispatch.py tests/test_runtime.py
PIPELINE_INTEGRATION_TESTS=1 .venv/bin/python -m pytest -q tests/test_dispatch_docker.py
```

The unit/service checks cover transactional intake, duplicate requests, invalid
package bindings, baseline failure, bounded infrastructure retry, lock exclusion,
live gRPC automatic completion and restart boundaries. The opt-in integration
test runs an actual coordinator subprocess against Docker, RabbitMQ and Redis,
kills it after committed candidate verification, and verifies resumption under
the original task ID.
