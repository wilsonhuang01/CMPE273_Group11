# Autonomous scheduler and sandbox workers

The coordinator can delegate all compiler/program invocations to independent
worker processes. The scheduler publishes durable RabbitMQ jobs; workers use
RPC leases and artifact transfer instead of opening coordinator SQLite/CAS state.
Verification, reviews, benchmarking analysis and final selection remain on the
coordinator, with the existing acceptance gates.

## Run a two-worker fleet

Prepare RabbitMQ/Redis and the native compiler image with `./project up` on a
Docker-capable development host. Then run each command below in its own terminal.
The scheduler and coordinator do not need Docker access once infrastructure and
worker images are prepared:

```sh
EXECUTION_BACKEND=remote ./project serve --no-start
```

```sh
./project scheduler
```

```sh
WORKER_DIRECTORY=var/worker-a ./project worker
```

```sh
WORKER_DIRECTORY=var/worker-b HEALTH_PORT=18086 ./project worker
```

Defaults are coordinator RPC on `127.0.0.1:50052`, coordinator health on 18083,
scheduler health on 18084, and worker health on 18085. Each worker needs its own
`WORKER_DIRECTORY` and health port. A local file lock prevents concurrent use of
one worker directory. This directory holds only identity, scratch data and
watchdog files; it contains no coordinator database. Worker UUID persists across
restarts, while each process gets a new incarnation and random registration secret.
The execution backend is frozen when a submission is accepted. Restart the
coordinator with the same backend to resume that work; a coordinator in the
other mode leaves those queued tasks untouched without spending attempts.

Wait for both workers to register, then use the normal client:

```sh
./project client submit examples/sum-array --request-file var/client/fleet.json --wait
./project client status TASK_ID
./project client download TASK_ID --output var/client/fleet-results
```

The server resolves compiler metadata from healthy registered workers. Submitting
before a compatible worker registers returns unavailable; retry with the same
request file after the fleet is ready. Unsupported starter generators still
abstain/fail their candidate and preserve baseline fallback. `EXECUTION_BACKEND=local`
retains the previous host-managed sandbox pool for existing demos and evaluations.

`WORKER_TARGET=auto` selects the worker's native Docker CPU architecture. Explicit
`linux-aarch64`, `linux-x86_64` and `linux-riscv64` profiles require their installed
toolchain images. A worker advertises one immutable target/image/compiler profile;
run a separate worker directory/process for another target. Emulation remains
correctness-only for performance qualification.

## Configurable endpoints and deployment boundaries

Scheduler and worker settings include:

- `COORDINATOR_ENDPOINT`: RPC host/port, default `127.0.0.1:50052`.
- `COORDINATOR_TLS=true` and optional `COORDINATOR_CA`: validate the server TLS certificate.
- `OPTIMIZER_API_TOKEN`: operator bearer token, shared with the coordinator.
- Existing `RABBITMQ_HOST`, port, user, password and virtual-host settings.
- `WORKER_DIRECTORY`, `WORKER_TARGET`, `HEALTH_HOST` and `HEALTH_PORT`.

Remote RPC endpoints require TLS and bearer authentication, as described in the
[transfer guide](artifact-transfer.md). Registration additionally creates a
per-incarnation worker secret: heartbeat, claim, completion and cleanup requests
must present the correct worker UUID, incarnation and secret. A lease token and
fence bind every execution/result to one assignment. Broker messages contain
dispatch IDs, not these credentials. Executors remain trusted producers; this
does not implement per-user tenancy or protect a deployment from a compromised
trusted worker host.

The application image can run the remote coordinator and scheduler without a
Docker socket. Configure its normal RPC/TLS endpoint, persistent coordinator
storage and `EXECUTION_BACKEND=remote`. Run the trusted executor on a host with
Docker access and the matching compiler images. Agents and coordinator containers
do not acquire Docker privileges. AMQP defaults remain local development settings;
managed broker TLS, shared cloud storage and AWS lifecycle operations are covered
by the cloud backlog.

The default Compose stack deliberately preserves its existing intake/topology
services: coordinator execution/transfer/fleet RPCs are disabled and
`SERVICE_ENABLED=false` keeps its scheduler/worker containers in infrastructure
mode. Host `scheduler` and `worker` commands enable the autonomous loops by
default. Set `FLEET_ENABLED=true` on an executing coordinator. Existing state
volumes are not moved or rewritten by changing endpoints.

## Dispatch, authority and cancellation

SQLite schema v8 adds `fleet_workers` and `fleet_jobs`. Only the coordinator opens
these tables. Registration validates target/compiler/image and sandbox policy
metadata. Heartbeats run once per second, with six-second worker freshness,
ten-second renewable leases and a fixed per-operation deadline derived from the
manifest's execution limit plus bounded transfer/orchestration allowance.
Repeated heartbeat sequences do not extend freshness or authority.

The coordinator records a bounded compile, execute or environment-probe request
and task-owned input artifacts. The independent scheduler atomically assigns
pending work through RPC, then publishes persistent messages with mandatory
routing and publisher confirms to `compiler-optimizer.work.v1`. Per-worker
durable queues use manual acknowledgements. Assigned work is republished until
claimed, so scheduler interruption between database assignment and publication
does not lose the job. Duplicate deliveries cannot claim running/completed work.

Workers fetch immutable inputs over the transfer API, validate compiler bindings
and execute with the existing Docker seccomp, cgroup, process/output limits and
external watchdog. Live lease checks gate sandbox creation/start. Results stream
back with bounded chunks and hash validation; the coordinator checks authority
again at commit and binds results to the job, task, image, build profile and CPU
affinity. Candidate work must also retain its parent recovery lease. Workers
cannot directly write verification outcomes or final optimization decisions.

An expired/cancelled assignment moves to cleanup. The executor cancels owned
containers when heartbeat renewal fails; external watchdogs survive executor
process death. Before fallback/retry, a healthy executor on the same Docker engine
must confirm removal of containers bearing the exact expired lease label. Cleanup
receipts from a different engine or stale identity are rejected. Expired results
cannot commit, including uploads that began before fencing. Failed primitive work
feeds the existing bounded baseline retry or candidate recovery policy, preferring
a different healthy worker.

On this development topology, both workers share one Docker engine, so the
survivor can confirm cleanup after process termination. If a physical remote host
becomes unreachable and no trusted executor can verify its cleanup, work fails
closed rather than claiming successful host recovery. A separate host recovery
supervisor is needed for that deployment scenario. Uncommitted execution can
repeat; the guarantee is fenced, idempotent committed evidence, not exactly-once
execution. Benchmark concurrency and distributed storage across coordinator hosts
remain separate backlog actions.

## Validation

```sh
.venv/bin/python tools/generate_contracts.py --check
.venv/bin/python -m pytest -q tests/test_fleet.py tests/test_dispatch.py tests/test_runtime.py
PIPELINE_INTEGRATION_TESTS=1 .venv/bin/python -m pytest -q tests/test_fleet_docker.py
```

The integration test starts a coordinator and scheduler with Docker absent from
their executable search paths, registers two independent workers with separate
directories, terminates one during a real sandbox operation, verifies cleanup and
reassignment, and downloads the final source/evidence. Unit/RPC tests exercise
late results, duplicate delivery, wrong secrets/incarnations, heartbeat replay,
parent fencing and cleanup from the wrong Docker engine.
