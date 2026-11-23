# Agent RPC services

Action F runs Performance, Memory, Architecture, Correctness, Critic and Benchmark
as independent gRPC services. The existing deterministic rules and frozen patch
replay remain the backends. Model providers and model hosting belong to Action G;
no model-hosting application or model weights are required here.

## Run the Docker services

`./project up` builds and starts all six services with separate private volumes,
readiness endpoints, bearer authentication and development TLS. Agents have no
Docker socket, coordinator database mount, broker credentials or submitted-program
execution capability. They run as UID 10002, separate from sandbox UID 10001:
Linux `RLIMIT_NPROC` counts processes/threads across containers sharing a UID.
Keeping these identities separate prevents agent threads exhausting compiler
process limits without weakening sandbox policy.

The Compose coordinator uses `AGENT_BACKEND=rpc`, with TLS connections to agent
DNS names on the private Compose network. Its readiness includes authenticated
agent identity checks. The default coordinator container remains intake-only;
use the existing host or remote execution backend to execute submissions.

For the separate host coordinator, copy the public development certificates and
opt into RPC. For the default development token:

```sh
mkdir -p var/agent-ca
docker compose cp agent-performance:/agent-ca/. var/agent-ca/
AGENT_BACKEND=rpc AGENT_TLS=true \
  AGENT_API_TOKEN=optimizer-agent-dev-token-change-me-0001 \
  AGENT_CA_DIRECTORY="$PWD/var/agent-ca" ./project serve --no-start
```

Use your configured `AGENT_API_TOKEN` if different. The checked-in Compose token
and self-signed certificates are development defaults; shared deployments should
supply their own credentials and trusted server certificates. Certificates and
private keys are generated inside agent volumes. Only public certificates are
shared with the coordinator. Refresh the copied public certificates after replacing
agent volumes/certificates. Existing coordinator/worker API credentials are separate.

Host RPC ports are 50061 Performance, 50062 Memory, 50063 Architecture, 50064
Correctness, 50065 Critic and 50066 Benchmark. Each has an
`AGENT_ROLE_PORT` Compose override, such as `AGENT_PERFORMANCE_PORT`.
`AGENT_ENDPOINTS` overrides all six endpoints using a JSON object with lowercase
role names. `AGENT_TIMEOUT_SECONDS` sets the entire call budget, including artifact
transfer, RPC, result download and validation (default 15 seconds; maximum 120).
`AGENT_TLS=true` enables TLS, and `AGENT_CA_DIRECTORY` contains `ROLE.crt` trust files.

The same services can run as native processes with separate state directories:

```sh
STATE_DIRECTORY=var/agents/performance \
  python -m compiler_optimizer.runtime --role performance
```

Repeat with `memory`, `architecture`, `correctness`, `critic` and `benchmark`.
Native defaults bind loopback on the RPC ports above and health ports 18091–18096
in the same role order. Compose publishes health on 19091–19096. A non-loopback
agent listener requires TLS and `AGENT_API_TOKEN`; configure `RPC_TLS_CERT` and
`RPC_TLS_KEY` for managed certificates. `AGENT_DEV_TLS=true` is the Compose-only
development helper, using `AGENT_TLS_DIRECTORY` for private credentials.

## Protocol and authority

`AgentService.GenerateProposal` retains its original request/response types.
Optional proposal fields carry the structured patch reference and provenance;
`OptimizationRequest.replay_patch` selects an uploaded, operator-owned replay
patch. `Review` and `PlanBenchmark` use versioned assessment envelopes containing
the exact submission, proposal, verification job/result and frozen plan reference.
The Benchmark service organizes work; sandbox workers perform measurements.

`PutArtifact` and `GetArtifact` transfer bounded, task-scoped content. Agents never
open coordinator paths or databases. Uploads require a header, contiguous bounded
chunks, a declared size and a matching SHA-256 digest. Downloads are also verified.
The coordinator applies returned patches itself and recomputes the source bundle;
a service cannot substitute source, compiler settings, a review's evidence binding
or the frozen benchmark plan. Incorrect automated results, rejected reviews,
emulated targets and unqualified measurements still cannot win selection.

Generation supports deterministic starter rules and frozen file patches. For RPC
cohorts, pass `--patch ROLE=FILE.json` to replay an operator patch. Arbitrary custom
Python backend objects remain an explicit local SDK feature; they are not silently
executed in the coordinator when RPC mode was selected.

Host CLIs and embedded SDK callers retain `local` mode by default for compatibility.
Use `AGENT_BACKEND=rpc` for host CLIs/automatic submission services, or inject
`AgentClient` into `PipelineCoordinator`/`CohortCoordinator`. Local mode supports
existing tests and custom in-process adapters. The transport of a committed
proposal cannot be silently switched on resume. Automatic submissions freeze
`agent_backend` at intake; pre-RPC tasks retain local agents even after the service
default changes. Server locations and credentials remain operator configuration.

## Deadlines, idempotency and provenance

All operation and transfer calls require finite deadlines. No transport retry loop
can expand a coordinator role's existing journal budget: generators and reviewers
have at most two attempts per operation, and Benchmark planning has one. A stopped,
slow or invalid agent consumes an attempt, records failure and cannot advance the
acceptance gates. Cohort failures retain the baseline through the existing selector.
A timed-out server computation cannot publish its late response. Trusted backend
implementations must also enforce their own internal deadlines; gRPC cancellation
does not forcibly terminate arbitrary Python code. Service worker/RPC counts are bounded.

Each role owns an `agent.sqlite3` ledger and content store. Completed operations
return the identical serialized response after redelivery and process restart.
Changing inputs under an existing operation identity conflicts. Concurrent duplicate
calls are rejected while the first is running. Interrupted operations remain in
the bounded attempt history. Only one process may own a role's state directory;
replicas must use independent local directories, not a shared SQLite network mount.

Provenance identifies the protocol version, role, persistent service UUID, declared
backend/release and request/response hashes. It is an audit binding, not hardware
attestation. Coordinator schema 11 stores successful and failed RPC receipts plus
immutable request/response artifacts. Existing source, verification, review,
measurement and selection records retain their strict formats and validation.
Each service's logs and ledger remain independent of the coordinator journal.

## Validation

`tests/test_agent_rpc.py` starts six real processes and exercises generation for all
three proposal roles, required reviews, planning, restart reuse, concurrent cohorts,
wrong credentials, deadlines, stopped services, forged evidence and altered plans.
It also checks automatic submission dispatch and legacy transport compatibility.

The Compose integration uses TLS agents, independent volumes and real compiler
sandboxes for a two-candidate cohort:

```sh
AGENT_INTEGRATION_TESTS=1 python -m pytest tests/test_agent_rpc_docker.py
```

On a native ARM engine, also set
`WORKER_TARGET=linux-aarch64 COMPILER_IMAGE=cmpe273-arm:18`.
