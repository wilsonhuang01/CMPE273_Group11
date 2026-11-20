# Local infrastructure

The Compose stack runs RabbitMQ, Redis, a coordinator, a scheduler, one application
worker, Prometheus and Grafana.
Application services share a non-root Python runtime image. Docker Compose v2 or
newer and a running Docker Engine/Docker Desktop are required.

## Start and inspect

The recommended entry point is `./project up`. It creates the local Python environment,
starts and checks the stack, and builds the native CPU toolchain. `./project status`
checks readiness and monitoring. Both commands load literal settings from `.env`;
see [Evaluation and release](evaluation.md) for demo and packaging commands.
Low-level commands to start just the infrastructure (without building a compiler
profile) are:

```sh
cp .env.example .env
docker compose config --quiet
docker compose up --detach --build --wait --wait-timeout 240
python3 tools/check_stack.py
docker compose ps
```

The `.env` copy is optional when using all defaults; Compose reads it automatically
when present. The smoke script uses default ports or values exported in your shell.
If you change ports only in `.env`, export the matching health-port variables before
running the script, or inspect the URLs below directly.

Default local endpoints:

- Coordinator readiness: `http://localhost:18080/readyz`.
- Scheduler readiness: `http://localhost:18081/readyz`.
- Worker readiness: `http://localhost:18082/readyz`.
- Coordinator internal intake gRPC: `localhost:50051`.
- RabbitMQ AMQP for the opt-in host pipeline: `localhost:5672`.
- RabbitMQ management: `http://localhost:15672`, with user `optimizer` and password
  `optimizer-dev-password` for this development stack.

Redis is published at `localhost:6379` for the host recovery controller; Prometheus
and Grafana use `http://localhost:9090` and `http://localhost:3000`.
All published ports bind to `127.0.0.1`. This is a local development deployment with plain
internal connections and known development credentials. Authentication/TLS for
application RPCs and production deployment remain outside the local implementation.

## What the running services do

- The coordinator initializes/reopens the SQLite repository and artifact store.
  Its gRPC `SubmitSubmission` and `SubmitProposal` handlers validate and commit
  intake before returning receipts. Duplicate messages are safe to resend. Missing
  prerequisites, invalid contracts, and storage conflicts become gRPC errors.
- The scheduler declares durable exchanges, queues, bindings, and a dead-letter
  sink. It restores declarations after a broker reconnect. It does not consume,
  dispatch, or execute verification jobs yet.
- The worker checks infrastructure connectivity and runs its service lifecycle.
  It does not consume jobs, advertise execution capacity, compile, or run source.

Every application's health JSON explicitly reports `execution_enabled: false`.
`infrastructure_ready` means its declared infrastructure dependencies are available;
it does not mean these service containers execute submissions. The implemented
[cohort coordinator](coordinator-selection.md) runs on the operator host with
specialized agents, scoped RabbitMQ delivery, verification, benchmarking and
[automatic recovery](fault-recovery.md). Its local logical workers use separate
host-owned state without adding Docker socket mounts to the Compose services.
Use `./project demo` or `./project evaluate` for the complete local workflow.
The [baseline CLI](baseline.md) now compiles and tests original C/C++ programs in
separate ephemeral containers; it does not enable execution in these services.
Its [sandbox policy and external watchdogs](sandbox.md) are enforced on those containers.
The coordinator RPC accepts an internal submission with already-stored
artifact references; a public source upload endpoint is not provided yet.

App containers have read-only root filesystems, dropped capabilities,
`no-new-privileges`, a bounded writable `/tmp`, and no Docker socket mount. Only
the coordinator mounts durable application state. These settings apply to service
processes; submitted programs use the separate implemented [sandbox policy](sandbox.md).
Application images run on the Docker engine architecture. Compiler profiles support
x86-64, ARM64 and RISC-V/QEMU; performance qualification requires a native x86-64
or ARM64 profile. See [Architecture support](architectures.md).

## Configuration and storage

`.env.example` lists Compose credentials, host ports, project name, and probe interval.
Change values before first startup. RabbitMQ initializes its default user only
when its data volume is empty; changing its environment does not rotate credentials
in an existing broker database. Manage existing credentials through RabbitMQ tools.

Runtime services also support `RABBITMQ_HOST`, `RABBITMQ_PORT`, `RABBITMQ_USER`,
`RABBITMQ_PASSWORD`, `RABBITMQ_VHOST`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`,
`STATE_DIRECTORY`, `HEALTH_HOST`, `HEALTH_PORT`, `GRPC_PORT`,
`COORDINATOR_HEALTH_URL`, and `PROBE_INTERVAL_SECONDS` when launched directly.
Compose provides container hostnames and uses fixed internal ports independently
of published host-port overrides. Probe intervals must be between 1 and 60 seconds.

Five named volumes persist across container recreation:

- `coordinator-data`: SQLite, WAL files, and artifacts at
  `/var/lib/compiler-optimizer` (see [storage](storage.md)).
- `rabbitmq-data`: broker data at `/var/lib/rabbitmq`; its hostname is fixed so
  RabbitMQ reuses the same node's data after recreation.
- `redis-data`: Redis data at `/data`, with append-only persistence enabled.
  Redis remains temporary coordination/blacklist storage; task truth is in SQLite.
- `prometheus-data`: monitoring time series.
- `grafana-data`: Grafana state and initial administrator credentials.

Compose prefixes volumes/networks with `COMPOSE_PROJECT_NAME`, defaulting to
`cmpe273-group11`. Use one coordinator for this SQLite-backed deployment.
Changing the project name creates a separate stack and separate volumes.

The runtime image uses Python 3.11 on Debian Bookworm; the broker uses RabbitMQ 4.1
and Redis uses 7.4. Tags receive upstream patch updates and dependency ranges are
not a deployment lock. Reproducible image digest locking can be added before
deployment outside this local environment.

## Health and queue topology

`/healthz` answers process liveness even during dependency outages. `/readyz`
returns HTTP 503 until the latest dependency probes pass, then HTTP 200. Readiness
also expires if the background probe loop stops updating. Reports contain
dependency status/error class names without credentials or exception text.

Coordinator probes check authenticated RabbitMQ/Redis connections and a SQLite
query. Scheduler and worker additionally check coordinator readiness; the scheduler
probe includes topology declaration. Compose gates initial startup using
`service_healthy` conditions. Application probes continue after startup; an outage
can mark a running container unhealthy. Docker health status alone does not restart
an unhealthy process. Services retry probes automatically, and `unless-stopped`
restarts exited processes. SIGTERM/SIGINT trigger graceful HTTP/gRPC shutdown.

The durable topic exchange `compiler-optimizer.v1` has four queues:

- `optimization.proposals.v1` binds `optimization.proposal.v1`.
- `verification.jobs.v1` binds `verification.job.v1`.
- `verification.results.v1` binds `verification.result.v1`.
- `worker.heartbeats.v1` binds `worker.heartbeat.v1`.

Each queue has `x-dead-letter-exchange = compiler-optimizer.v1.dead`. The durable
`deadletters.v1` queue binds that exchange with `#`. Message publication,
acknowledgments, TTL retry queues, bounded backoff and terminal dead-letter notices
are implemented by the host workflow on separate scoped proposal/retry routes.
The four general queues above are declared infrastructure, not a remote worker
execution fleet. See [fault recovery](fault-recovery.md) for delivery behavior and
[message contracts](contracts.md) for payload formats.

## Logs, restart, and stop

```sh
docker compose logs --follow coordinator scheduler worker
docker compose restart coordinator scheduler worker
docker compose up --detach --wait --wait-timeout 120
docker compose down
```

Application log files rotate at 10 MiB with three files per container. `down`
removes containers/network and retains the named volumes. `down --volumes` also
deletes the persisted state; use that only for a deliberate reset. Stop writers
and back up all state before changing storage or credentials.

CI builds and starts this same stack, checks readiness, recreates application
containers, and verifies a stored artifact survives. It deletes only its own
ephemeral CI volumes afterward. Unit/integration checks in `tests/test_runtime.py`
cover readiness transitions, topology, configuration, live local HTTP, and gRPC
intake with durable storage. Those tests require permission to bind loopback ports.
