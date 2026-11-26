# Observability and failure testing

## Start and view

```sh
./project up
./project status
```

Prometheus is at <http://localhost:9090>; Grafana is at <http://localhost:3000>.
The provisioned **Compiler Optimizer: execution and recovery** dashboard is at
<http://localhost:3000/d/compiler-optimizer>. Development login is `admin` /
`grafana-dev-password`; `.env.example` documents credential and port overrides.
Grafana's admin password is initialized only when its data volume is first created.
Prometheus retains 15 days of samples, and both services have persistent volumes.
Ports bind to loopback. The dashboard and data source are managed from
`monitoring/grafana`; edit those files to preserve changes across recreation.

Compose application services expose `/metrics` on their existing health ports
(18080–18082 on the host). The coordinator's durable metrics describe its own
coordinator-data volume. Host optimization commands use a separate state directory,
so start an exporter for that exact directory in another terminal:

```sh
python -m compiler_optimizer.monitoring --state var/cohort --host 0.0.0.0
python -m compiler_optimizer.coordinator.round \
  --package examples/sum-array --agents performance --state var/cohort
python tools/check_observability.py --require-host
```

The exporter serves <http://localhost:18083/metrics>. It defaults to loopback;
`--host 0.0.0.0` allows the Compose Prometheus container to reach it through
`host.docker.internal` (including the Linux host-gateway mapping). Choose a trusted
development network when binding to all interfaces. The optional host target is
down until the exporter starts; it does not make application readiness fail.
Use the dashboard's **Evidence store** selector: `optimizer-host` for host runs,
`optimizer-services` for the Compose coordinator. Do not sum both targets when
they expose the same database. A single exporter serves one directory; additional
stores need distinct ports and scrape jobs. No workflow executes inside the
monitoring server, and Compose workers retain their infrastructure-only role.

## What is exposed

- RabbitMQ ready and unacknowledged message depth comes from its built-in
  `rabbitmq_prometheus` plugin on internal port 15692. Default aggregated metrics
  avoid one time series per transient proposal queue. Both ready and unacknowledged
  depth appear in the queue panel, including retry/dead-letter queues.
- `optimizer_tasks{status}` and `optimizer_oldest_active_task_seconds` describe
  current durable task state. `optimizer_task_duration_seconds` is a histogram
  from accepted history timestamp to terminal history timestamp, labeled by final
  status. Running tasks do not enter completed-duration observations.
- `optimizer_workers{state}` classifies registered execution workers as idle,
  busy, quarantined or stale using server time and the associated frozen recovery
  policy. Idle local workers stop heartbeating after execution and eventually
  appear stale: a new health probe is required before reuse. This differs from
  Compose service readiness. Maximum heartbeat age is also exposed.
  The stale-worker alert uses `optimizer_stale_assigned_workers`, so historical
  idle worker registrations do not create permanent alerts after completed runs.
- `optimizer_retries_total{reason}` counts durable retry intents, including lease
  expiry and unavailable workers, rather than counting RabbitMQ redelivery.
  Current attempt/job states are gauges; `optimizer_recovery_outbox_pending` shows
  unconfirmed publications. Terminal receipts have a separate counter by reason.
- `optimizer_crashes_total{phase,reason}` counts committed runtime failure
  observations (including timeout, memory/output limits, nonzero exit, signals and
  sanitizer failures). The measurement phase counts failed measurement batches,
  with reason `PROGRAM_FAILURE`; verification preserves the exact enum reason.
  Independent confirmation is another observation. A crash count is not itself
  a blacklist decision; use terminal reason `confirmed_program_crash` for quarantine.
- `optimizer_verification_results_total{outcome,reason,stage}` includes compilation
  and infrastructure failures, successful checks and failed runtime checks.
- `optimizer_optimization_gain_mean_ratio{metric}` shows mean committed runtime,
  memory and weighted gains. Retained baselines contribute zero. The corresponding
  signed `optimizer_optimization_gain_ratio` histogram exposes distributions;
  negative gains are possible when one resource trades off against another.
  Signed histograms omit `_sum`/`_count` in the Python client; the `+Inf` bucket is
  their observation count. The mean panel uses exact aggregates, not interpolated
  histogram quantiles. These are accepted decisions, not unverified candidate claims.
- `optimizer_service_ready`, `optimizer_dependency_up` and Prometheus `up` distinguish
  cached application readiness, dependency probe outcomes and scrape availability.

Counts and durations are derived from one consistent SQLite read transaction,
then immutable evidence artifacts are inspected outside the database transaction.
There is no scrape-time database mutation or in-memory event counter to lose on
restart. Repeated scrapes, duplicate results, lost acknowledgements and exporter
restarts do not inflate observations. Replacing/deleting the underlying repository
resets its counters. The local exporter currently scans evidence at scrape time;
large deployments should add indexed summaries before increasing workload volume.
Do not prune evidence while serving this exporter.

Metrics labels contain bounded states, phases, enum reasons and metric names.
Task/proposal/worker UUIDs, source hashes, transformation text, inputs, compiler
diagnostics and credentials are excluded. If collection fails, `/metrics` returns
503 with a generic message rather than publishing zeros. `/healthz` remains
process liveness and `/readyz` retains dependency readiness semantics.

## Structured events and alerts

The cohort CLI and runtime configure newline-delimited JSON logs. Events cover
verification results, attempt start/completion and elapsed duration, retirement,
infrastructure retries, program crashes, recovery terminals, broker interruptions,
received/acknowledged/duplicate deliveries, observed queue depth and final selected
gains. IDs correlate the records with durable evidence. Delivery queue depth is the
remaining ready count reported by `Basic.GetOk`; RabbitMQ's exporter supplies the
continuous broker-wide depth. Metrics scrapes also emit a `metrics_snapshot` event
with scalar aggregate values, so task state, health, retry/crash counts and gains
are visible in JSON logs as well as Prometheus/Grafana.

Only allowlisted fields are emitted; no source, raw request, exception message,
sanitizer output or connection secret is logged by these instrumentation paths.
Library users can configure the `compiler_optimizer.events` logger themselves.
Logs may repeat after replay; durable metric counts remain idempotent. CLI reports
remain JSON on stdout and logs go to stderr. Use, for example:

```sh
python -m compiler_optimizer.coordinator.round --task-id TASK_ID --state var/cohort \
  > result.json 2> events.jsonl
docker compose logs --no-log-prefix coordinator scheduler worker
```

Prometheus evaluates rules for unavailable services, stale workers, exhausted
infrastructure retry budgets and sustained queue backlogs. See `/alerts` in
Prometheus; external notification delivery is not configured.
`monitoring/prometheus/alerts.yml` holds the rules. Validate with:

```sh
docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yml
docker compose exec -T prometheus promtool test rules /etc/prometheus/alerts.test.yml
```

## Failure tests

```sh
python -m pytest tests/test_observability.py tests/test_recovery.py tests/test_pipeline.py
BASELINE_DOCKER_TESTS=1 RECOVERY_INTEGRATION_TESTS=1 python -m pytest \
  tests/test_failure_injection_docker.py tests/test_recovery_docker.py \
  tests/test_baseline_docker.py tests/test_watchdog_docker.py
```

The added integration tests terminate their own leased worker-owner subprocess
without cleanup and resume its persisted job on a new worker after lease expiry.
They close their own AMQP connection after commit but before ack, then verify
resume settles the unacked original and newly published duplicate without
recompilation, remeasurement or another attempt. The shared broker is not stopped.
Real looping and memory-exhausting candidates require independent confirmation
and quarantine; compiler syntax errors fail once without crash blacklisting.
All retain the verified original, with metrics checked against persisted evidence.
The earlier sandbox suite also tests infinite-loop deadlines, OOM, compiler errors,
and watchdog survival after owner exit. Tests remove only their own Redis keys and
use unique workloads/scoped queues. CI runs these tests and validates live scrape
targets, queue metrics, provisioned panels, data source health and alert syntax.

Configuration follows the official [Prometheus scrape configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
[Python custom collector API](https://prometheus.github.io/client_python/collector/custom/),
[RabbitMQ Prometheus plugin](https://www.rabbitmq.com/docs/prometheus) and
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/) documentation.

## Action O: stages, independent agents and release evidence

Agent health ports now also serve `/metrics`; the `optimizer-agents` scrape job
collects the six agent services when their Compose profile is enabled. It may be
absent/down when only the infrastructure profile is running. Model calls and
reported input/output tokens come from each agent's private durable ledger, with
provider latency histograms. Calls include retries and interrupted reservations;
unknown token usage after a failed/interrupted call remains zero, not an estimate
of billable usage. Model IDs, prompts, completions and provider error messages are
excluded from metrics. Do not sum replicas exposing the same store.

`optimizer_agent_operation_duration_seconds{method,state}` measures the latest
finished attempt per deduplicated Generate/Review/Plan operation. Retrying an
operation replaces that operation's duration; this is an evidence snapshot,
not a cumulative attempt histogram. Old operations with no timestamps remain
cached and do not acquire invented timings. Execution attempts have a separate
`optimizer_execution_attempt_duration_seconds{status}` histogram, including retries.

Coordinator metrics also expose `optimizer_fleet_workers{state}` (idle/busy/stale
using fleet heartbeat freshness), primitive `optimizer_fleet_jobs{state}`,
`optimizer_measurement_windows{state}`, started initial/revision slots through
`optimizer_generation_rounds_total{kind}`, evaluation outcomes and final bounded
`optimizer_candidate_rejections_total{reason}`. Repeated scrapes and replay do not
increase durable counts. Fleet availability is worker slots, not a count of CPU
cores; the scheduler still enforces each profile's resource budget. Repository
and fleet summaries use read transactions; immutable reports are read afterward.

The Docker debate test now interrupts delivery after the first committed round,
reopens the repository and resumes the revision with frozen feedback and budgets.
The existing fleet test kills an independent worker; recovery tests close an AMQP
connection before ack and settle duplicate deliveries. Fleet unit tests reject
stale commits and replayed heartbeats. These exercise **local process and network
faults**, not loss of a physical host, cloud failover or GPU recovery.

To produce redacted evidence without copying the agent's private prompt store:

```sh
python tools/reproduction.py --agent-state var/agent-performance \
  --image cmpe273-arm:18 > var/action-o-provenance.json
./project package --output dist/release.tar.gz \
  --agent-state var/agent-performance --image cmpe273-arm:18
```

Omit `--agent-state` for runs without model calls. Repeat it for each agent store.
The exporter opens agent SQLite read-only, preserves model/backend identity,
prompt/response hashes, state, latency and usage, and excludes text, errors and
credentials. `--image` resolves an existing local image to its immutable ID;
it requires Docker. Release archives always include `provenance.json` with source
hashes, configuration hashes, declared image references and installed package
versions. Source revision may be absent outside Git; source hashes still identify
packaged implementation files. No `.env` values are copied into provenance.
