# Concurrent preparation and isolated measurement

The cohort coordinator overlaps proposal generation, compilation, verification and
reviews within a bounded preparation pool. All preparation drains before the
cohort enters measurement. Benchmarks run serially, holding an exclusive Docker
engine window from the first environment probe through evidence commit. This
keeps generation, compilation, sanitizer/fuzz cases and reviews out of qualifying
measurement windows.

## Capacity and reservations

Capacity comes from the Docker engine's `NCPU` and `MemTotal`, rather than the
client computer's CPU count. Admission leaves one engine CPU and 512 MiB free.
Each candidate reserves one CPU and the larger of its compile/run memory limits,
plus 256 MiB for trusted agent work. The active preparation count is the minimum
of available CPU slots, available memory divided by that reservation, the operator
limit (eight by default), and the recovery pool size. Insufficient capacity fails
before starting work. SDK adapters without Docker telemetry remain sequential.

```sh
python -m compiler_optimizer.coordinator.round --package examples/sum-array \
  --agents performance,architecture --workers 4 --state var/concurrent

python -m compiler_optimizer.coordinator.round --task-id TASK_ID \
  --state var/concurrent
```

`--workers` is an upper bound. Embedded callers can set
`CohortCoordinator(..., concurrency=N)`. Recovery workers are claimed atomically;
a busy worker does not consume another candidate's infrastructure-attempt budget.
Resume may reduce active concurrency if resources or the configured limit shrink.
The original verification, benchmark, selection and recovery policies remain frozen.

Local Docker clients share engine-ID-scoped OS locks and a resource ledger under
the operator's temporary directory. Individual sandbox lifetimes reserve their
CPU and memory through watchdog cleanup. Generation and reviews use shared work
windows; measurements obtain an exclusive window. A writer blocks new readers
while existing work drains. A measurement also checks that no optimizer watchdog
containers remain, so process death cannot silently turn abandoned sandboxes into
measurement load. These locks coordinate participating processes under the same
operator account and shared temporary directory.

Remote scheduling enforces CPU/memory reservations across worker identities on
the same engine. A durable measurement window first stops new ordinary dispatch,
then waits for assigned/running work and cleanup to drain. Only jobs carrying the
window's authority can run until it releases. Lease loss and coordinator restart
fence that authority; an engine with unconfirmed cleanup stays unavailable to
measurement. Workers check assigned memory reservations against sandbox limits.
As with any benchmark deployment, unrelated applications and Docker clients
outside this admission protocol require a dedicated measurement VM; environment
checks and conservative qualification still apply.

## Durable completion and cancellation

Schema 10 adds cohort/member checkpoints, remote measurement windows and
measurement claims. Ready candidates survive a coordinator restart without
repeating verification, and completion follows the immutable selection decision.
Cancellation requests are durable and do not replace completed decisions:

```sh
python -m compiler_optimizer.coordinator.round --task-id TASK_ID \
  --state var/concurrent --cancel
```

An active owner observes the request, stops queued preparation, and drains active
work before recording cancellation. If it has stopped, resume the task to finish
that drain. A cancelled cohort cannot select a candidate or restart on resume;
its already committed artifacts remain available for inspection. Cancellation
requires an initialized cohort. It does not interrupt an arbitrary trusted custom
backend's Python call; adapters must enforce their own call deadlines.

Each candidate claims measurement durably before sampling. Completed measurements,
including noisy or statistically unqualified runs, cannot be sampled again under
another plan, job attempt or entry point. A crash leaving an unfinished claim is
also excluded rather than resampled. Only recorded infrastructure/measurement
failures permit another attempt, with the same plan and a persistent retry cap.
Pre-migration reports are checked before issuing a new claim. Idempotent report
redelivery and cached reads remain supported.

## Validation

`tests/test_concurrency.py` exercises overlapping generation and verification,
resource limits shared across worker identities, cross-process measurement
exclusion, cleanup fencing, cancellation, smaller-pool resume and prevention of
resampling. The opt-in Docker test observes two concurrent compiler invocation
lifetimes, two drained measurement windows and cached resume using real sandboxes:

```sh
CONCURRENCY_INTEGRATION_TESTS=1 python -m pytest tests/test_concurrency_docker.py
```

Use `WORKER_TARGET=linux-aarch64 COMPILER_IMAGE=cmpe273-arm:18` on a native ARM
engine. The fixture uses a portable architecture patch for either CPU target.
Independent worker loss and reassignment are also covered by
`tests/test_fleet_docker.py` with `PIPELINE_INTEGRATION_TESTS=1`, RabbitMQ and Redis.
