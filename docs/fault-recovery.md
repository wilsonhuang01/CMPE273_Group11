# Fault recovery and scoped quarantine

The host cohort coordinator now runs each candidate through a persistent recovery
job. Verification, required Correctness/Critic reviews, frozen benchmark plans and
weighted selection still apply. Failed, quarantined or exhausted candidates cannot
win; the original program remains the fallback.

## Run

Start the stack and build its native toolchain with `./project up`.
For an asserted successful-optimization and failure-recovery demonstration, run
`./project demo`; see [Evaluation and release](evaluation.md).
The manual cohort example below uses the default x86-64 profile; select
`--target linux-aarch64` on a native ARM64 Docker engine to qualify benchmarks.
RabbitMQ AMQP and Redis are exposed on loopback ports 5672 and 6379.
Credentials and host/port overrides come from `runtime.config.Settings` environment
variables; Compose's `REDIS_HOST_PORT` changes the published port, so also set
`REDIS_PORT` for the host command when using a nondefault port.

```sh
python -m compiler_optimizer.coordinator.round \
  --package examples/sum-array --agents performance,architecture \
  --recovery-policy examples/recovery-policy.json --workers 2 \
  --state var/recovery

python -m compiler_optimizer.coordinator.round \
  --task-id TASK_ID --state var/recovery
```

The CLI enables recovery by default with two local logical workers, separate
identities and DockerRunner instances. Both use the same pinned compiler image and
sandbox policy. `--workers` accepts 1–8; at least two available, context-compatible
workers are needed to confirm a crash independently. Work runs sequentially per
cohort; this is a host-managed sandbox pool, not remote worker dispatch. Compose's
application worker still does not receive the Docker socket or execute submissions.
The SDK opts in with `CohortCoordinator(..., recovery=RecoveryController(...))`
or `PipelineCoordinator(..., recovery=RecoveryController(...))`.

The legacy `coordinator.pipeline` CLI and standalone `workers.verify` and
`workers.benchmark` CLIs also enable the same leased executor and Redis quarantine
checks by default. They use the same Redis environment settings, bounded attempt
budget, watchdog cleanup and exact source/context identity. Standalone commands
honor durable backoff locally; the pipeline uses broker retry deliveries. Redis
outages fail closed and consume the frozen retry budget. Confirmed exclusions are
stored durably before publication to Redis; failed publication remains pending.
Read-only inspection and compilation-only commands do not require Redis.

Standalone verification and benchmarking keep separate recovery checkpoints.
Benchmark checkpoints are scoped to the verification job and attempt, so a cached
verification never skips measurement. Completed measurements are reused; only
infrastructure/measurement failures can retry. A benchmark program crash is
confirmed through independent verification rather than repeated measurement.
`workers.verify --benchmark-plan PLAN.json` sets the benchmark component of the
quarantine context (default: the standard benchmark plan). Supply the same trusted
plans when resuming custom standalone work. Attempts to replace existing
verification plans or frozen pipeline/cohort plans fail before execution.

For embedded callers, `workers.execution.execute_verification` and
`execute_benchmark` provide these managed standalone paths and accept an injected
`RecoveryController`. The lower-level `run_verification` and `run_benchmark`
functions are executor primitives used inside the active lease. They reject
unfenced writes once a proposal has managed recovery state.

Database schema 9 adds operation scopes while retaining existing job IDs, frozen
policy/context artifacts, attempt counts, outcomes and outbox events. Existing
pipeline checkpoints retain their stored verification, benchmark and selection
policies. A terminal legacy candidate produces an evidence-checked recovery report
and retains the baseline through the existing selection gates.

## Delivery and retry rules

Each immutable proposal uses a scoped durable RabbitMQ queue. Publisher confirms,
persistent messages and mandatory routing precede the publication checkpoint.
Manual acknowledgement follows a durable evaluation checkpoint. Pika's owner
thread continues servicing broker heartbeats while evaluation runs elsewhere.

An infrastructure failure records its evidence, releases/quarantines the worker,
increments the durable attempt budget and creates a transactional outbox intent.
A persistent copy enters a scoped retry queue with message TTL and a dead-letter
route back to the proposal queue. Its publication is confirmed before the original
delivery is acknowledged. Exponential backoff uses deterministic jitter. SQLite
owns the attempt count; message headers and redelivery flags cannot reset it.
Terminal candidates produce a persistent notice in `deadletters.v1` through the
dead-letter exchange with routing key `recovery.REASON`, confirmed before ack.
Malformed messages are nacked without requeue through the queue's DLX.

Default policy: three total attempts, 15-second leases, 2-second heartbeats,
10-second worker freshness, an absolute one-hour attempt deadline, backoff starting
at one second and capped at 30 seconds with 20% jitter, five-second worker cooldown,
five-second cleanup retry window and seven-day blacklist TTL. The JSON policy is
strictly validated and frozen per proposal before the first attempt. Resume adopts
the stored policy; explicitly changing it conflicts. Missing healthy workers also
consume bounded attempts after backoff, rather than waiting forever. A worker in
cooldown requires a new successful control-plane probe before reuse. A fresh,
unused worker is preferred for infrastructure retries; crash confirmation always
requires a different worker.

Compiler/daemon/transport faults, unknown infrastructure failures and failed
measurement infrastructure retry under the same source, compiler and frozen
plans. A successful verification is reused for measurement retries. Only a batch
whose raw record says it failed for infrastructure/measurement reasons may be
replaced under an active lease. Completed, noisy, underpowered or program-failed
batches cannot be resampled to obtain a favorable score. Review and planning
outputs are reused under their original iteration budgets.

## Leases, heartbeats and idempotency

SQLite schema v6 stores registered worker UUID/incarnation, monotonic heartbeat
sequence, health and assignment; recovery jobs, attempt history and fencing
tokens; terminal outcomes; local quarantine evidence; and the publication outbox.
`WorkerHealthService.ReportHeartbeat` is served alongside coordinator RPCs.
Identical heartbeat duplicates return a receipt without renewing freshness.
Conflicting sequences, unknown identities, stale incarnations and invalid
assignments are rejected. Heartbeats alone cannot extend task leases: renewal
requires the current lease token, fence, worker incarnation and fresh busy state.

Every managed compile/run/environment operation checks execution authority before
starting. Repository writes check the current fence at transaction entry and
again before commit. Expired/replaced workers cannot submit late verification,
reviews, benchmark data or completion receipts. The controller monitors worker
heartbeats, lease expiry and an absolute deadline independently of the execution
thread. Timed-out handles are retired. Recovery cancels their watchdogs and removes
only containers labeled with that exact lease token. A replacement cannot run
until sandbox cleanup is confirmed; otherwise the candidate is excluded with
`sandbox_cleanup_unconfirmed`. Detached sandbox watchdogs still enforce resource
limits if the host coordinator dies. Trusted SDK runner adapters must enforce
their own bounded execution and cancellation.

Durable jobs identify `(task_id, proposal_id)`. Completed outcomes are replayed
without recompiling or measuring. Attempt transitions and outcomes use fenced
transactions; existing verification/result identifiers retain their original
idempotency checks. Lost acknowledgements, retry publications or coordinator
restarts can cause duplicate delivery, but cannot add execution authority or
overwrite committed evidence. Outbox events have deterministic IDs and replay
after restart; duplicate DLQ notices can be deduplicated by `recovery-event-id`.
Successful completion removes that proposal's work and retry queues. This provides
at-least-once delivery and idempotent committed results, not exactly-once execution.

## Deterministic crash quarantine

A candidate runtime signal, nonzero exit, sanitizer failure, timeout, memory limit
or output limit schedules one independent confirmation using full verification
on another healthy worker in the same execution context. Two matching signatures
(reason, test, signal/exit code and build profile) create quarantine. Evidence
contains both worker identities and verification/raw report references. A passing
or differently failing confirmation is `unstable_program_failure`: excluded for
this task with no shared blacklist entry. Output mismatch and syntax rejection
are ordinary failed candidates, not shared crash patterns. Invalid reference
programs fail the task and never blacklist the proposed transformation.

Redis keys have the form:

```text
compiler-optimizer:blacklist:v1:
  SHA256(canonical({transformation, source_sha256, context_sha256}))
```

Context binds the parent source and entire input package; compiler image digest,
executable, version, language, target and flags; verification and benchmark plan
digests; all resource limits; Docker engine, CPU model/features/allowed CPUs and
kernel; and sandbox policy digest, platform, CPU quota and network policy. IDs of
tasks/proposals/workers are excluded so an exact workload can reuse a quarantine
across tasks. Another source, transformation, workload, toolchain, plan, hardware
context or limit gets a different key. No transformation-wide ban is created.

Quarantine is committed locally with evidence and an outbox event before Redis
publication. Redis uses `SET NX PXAT` with the original absolute expiry; replay
does not extend TTL. Reads validate the exact identity, expiry and two matching
independent observations. A known local quarantine can safely deny execution
during a Redis outage. An unknown or malformed cache result fails closed as an
infrastructure error with bounded retries, without creating a crash blacklist.
After expiration the same context may be evaluated again. Redis is a shared
quarantine cache; SQLite/artifacts remain the durable evidence record.

## Inspect and test

`repo.get_recovery_job(task, proposal)`, `repo.recovery_attempts(task, proposal)`,
`repo.recovery_workers()`, `repo.recovery_outcome(task, proposal)` and
`repo.recovery_outbox(task, pending=False)` expose durable recovery evidence.
The selection evaluation artifact links the recovery receipt or attempts, and the
final audit links that evaluation. Terminal reasons distinguish infrastructure
exhaustion, unconfirmed/unstable crashes, confirmed crashes and blacklist hits.

```sh
python -m pytest tests/test_recovery.py tests/test_pipeline.py
RECOVERY_INTEGRATION_TESTS=1 python -m pytest tests/test_recovery_docker.py
```

Unit tests cover alternate-worker retries, deadlines, commit fencing, lease
takeover, heartbeat RPC/idempotency, lost acknowledgement, measurement retries,
cache outage, context isolation, nonreproducing crashes and broker confirm/ack
ordering. Integration tests use actual Docker compilation/UBSan, RabbitMQ TTL
redelivery/DLQ and Redis TTL, including skipping an exact quarantine from a fresh
coordinator database. They delete only their own Redis key and observer queue.
