# Single-proposal optimization pipeline

The [specialized agents](specialized-agents.md) extend this workflow with Memory and Architecture
generators, mandatory Correctness/Critic reviews and a Benchmark planning agent.
The commands below still default to Performance; use `--agent` for other generators.
The [selection coordinator](coordinator-selection.md) adds weighted cohort ranking and applies its
policy to new single-proposal flows; historical workflows keep their frozen contract.

This opt-in, single-host workflow connects baseline execution, a Performance Agent,
durable RabbitMQ proposal delivery, sandbox verification, paired benchmarking,
coordinator selection, and a retrievable result. SQLite and artifacts remain owned
by the local coordinator. The Compose application containers keep infrastructure
and intake running without Docker sockets or queued execution.

For the recommended workflow with automatic recovery, use the
[cohort coordinator](coordinator-selection.md) or `./project demo`. This page
documents the compatible single-proposal `coordinator.pipeline` path, which does
not use the recovery controller or blacklist.

## Run the workflow

With the development environment activated:

```sh
docker compose up --detach --build --wait --wait-timeout 240
docker compose --profile toolchain build compiler
python -m compiler_optimizer.coordinator.pipeline \
  --package examples/sum-array --state var/pipeline \
  --verification-plan examples/sum-array.verification.json \
  --benchmark-plan examples/sum-array.benchmark.json
```

RabbitMQ AMQP binds to `127.0.0.1:5672`. Override its published port with
`RABBITMQ_AMQP_PORT` in Compose. The host runner reads `RABBITMQ_HOST`,
`RABBITMQ_PORT`, `RABBITMQ_USER`, `RABBITMQ_PASSWORD`, and `RABBITMQ_VHOST` from
its environment with the existing development defaults. Export matching host
values when changing Compose settings; Compose's `.env` does not populate the
host process environment. Credentials are not printed.

The command returns JSON with task/proposal IDs, patch reference, rationale,
transformation, correctness outcome, benchmark statistics/reasons, selected job
and source bundle, and final task status. Exit 0 means completion, including safe
baseline fallback; exit 1 means the reference failed/was invalidated; exit 2 means
invalid input or operational interruption. Interrupted workflows print their task
ID and state directory for resumption.

The tiny example validates the wiring. Pipeline and cohort commands select the
Docker engine's native ARM64 or x86-64 profile by default; `--target` overrides it.
Emulated execution cannot qualify native performance claims. Short/noisy samples, excess memory,
or insufficient gain also retain it. Deterministic unit tests cover successful
candidate selection without relying on live timing on shared hardware.

## Structured agent output

`PerformanceAgent` accepts a replaceable `PatchBackend.generate(request, sources)`.
The default backend uses bounded syntax-aware rules for a
[documented C/C++ subset](transformations.md), including stdio buffering and
unsigned pointwise unrolling. It abstains when grammar or proof conditions do not
match. Independent checks determine whether a candidate improves a workload.

For arbitrary supported C17/C++17 programs, use `--patch response.json` to replay
a structured patch from a model or operator, or implement `PatchBackend` in Python.
External model credentials and network inference are not required by the rule backend.
Memory and Architecture generators and Correctness/Critic/Benchmark roles are
implemented in [Specialized agents](specialized-agents.md). External inference
provider adapters and provider authentication remain deferred.

```json
{
  "schema_version": 1,
  "parent_source_sha256": "SHA256_OF_ORIGINAL_SOURCE_BUNDLE",
  "rationale": "Explain the expected improvement and assumptions.",
  "transformation": "portable-transformation-id",
  "edits": [
    {"path": "main.c", "old": "unique exact original text", "new": "replacement text"}
  ]
}
```

Patch JSON is bounded to 1 MiB, rationale to 16 KiB, and edits to 32. Duplicate or
unknown fields, wrong parent hashes, ambiguous/missing context, traversal, NULs,
empty/no-op patches and fixture/build-setting edits are rejected. Edits apply in
listed order, once each, to existing source files/headers only. Python applies
byte replacements; no shell, patch tool, build script, or model instructions run
on the host. The complete package remains bounded to 10 MiB. Proposal role,
compiler, parent, version, rationale and transformation are bound to the saved
assignment and patch. Source and patch hashes are rechecked on delivery.

For a pre-existing passing baseline, obtain the parent hash from
`repo.get_task(task_id)["submission"].source.bundle.sha256`, then run:

```sh
python -m compiler_optimizer.coordinator.pipeline --task-id TASK_ID \
  --patch response.json --state var/baselines
```

The coordinator freezes original tests, compiler settings and both execution
plans. Generation is not correctness evidence. Incorrect or hanging candidates
never reach benchmarking. Reference invalidation fails the task. Only a verified
candidate with a qualified benchmark can replace the baseline.

## Delivery and resumption

Schema v3 transactionally migrates v1/v2 databases. `enqueue_pipeline` atomically
stores the exact proposal, patch/plan artifact links and delivery intent. An agent
error leaves no runnable proposal; its assignment remains as audit history and
the passing baseline can receive another assignment.

The coordinator publishes the validated Proposal protobuf to the durable exchange
using scoped route `optimization.proposal.v1.PROPOSAL_ID` and durable queue
`performance.PROPOSAL_ID.v1`, isolating this milestone from reserved scheduler
queues. Messages are persistent, mandatory-routed and publisher-confirmed. A crash
between confirmation and marking publication can produce a duplicate; consumers
reuse durable stage results and final reports.

The consumer checks the entire proposal body and envelope against stored state.
Poison messages go to the existing dead-letter sink; uncommitted processing errors
requeue. A worker thread runs verification/benchmarking while the Pika thread
services heartbeats. Prefetch is one. Acknowledgement follows coordinator selection
and report persistence; the scoped queue is then removed, including duplicate
copies of that immutable proposal.

```sh
python -m compiler_optimizer.coordinator.pipeline --task-id TASK_ID --state var/pipeline
```

Resume reuses stored patches and plans; omit generation/plan options. Completed
results return without publication or execution. Committed verification and
benchmark stages are reused with plan identity checks. Interruption after selection
but before report persistence is repaired from durable evidence. An interrupted
uncommitted execution may run in a fresh sandbox/job; its independent watchdog
owns cleanup. Check watchdog audits after a hard kill. Broker interruption after
commit does not change the decision; resume returns the stored report.

A local advisory lock serializes orchestrators per state directory, without
holding SQLite transactions during queue/execution waits. This standalone path
does not use worker leases or automatic retry scheduling. The
[cohort recovery path](fault-recovery.md) implements those features for a local
worker pool. Neither path promises exactly-once execution across an uncommitted
stage. Use a native x86-64 or ARM64 profile for performance qualification.

## Retrieve results through the coordinator

The coordinator adds backward-compatible `GetOptimizationResult`. Run the local
API against the **same host state directory** in another terminal:

```sh
python -m compiler_optimizer.coordinator.results --state var/pipeline --port 50052
```

```python
import json
import grpc
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc

with grpc.insecure_channel("127.0.0.1:50052") as channel:
    result = rpc.CoordinatorServiceStub(channel).GetOptimizationResult(
        pb.OptimizationQuery(task_id="TASK_ID"), timeout=5
    )
    print(result.status, result.stage, json.loads(result.report_json))
```

The response includes status/stage, report JSON/artifact reference, and selected
`SourceVersion`. Running tasks report their current stage. The API binds to loopback
and uses the existing internal intake trust model. Compose's port 50051 serves its
separate named volume, not host `var/pipeline`. Neither endpoint is a public artifact
upload/download service. Read selected bytes from the corresponding artifact store:

```python
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.package import unbundle

repo = Repository("var/pipeline")
files = unbundle(repo.artifacts.get(result.selected_source.bundle))
print(files["main.c"].decode())
```

## Validation

```sh
python -m pytest tests/test_pipeline.py
PIPELINE_INTEGRATION_TESTS=1 python -m pytest tests/test_pipeline_docker.py
```

Unit tests cover real orchestration with fake transport/execution: qualified
selection, fallback, malicious patches, publish/ack ordering, heartbeats, outages,
resumption, migrations and result RPC. The opt-in tests use actual RabbitMQ and
Docker for generation, sanitizer verification, benchmarking, result retrieval,
wrong-output candidates and infinite loops. CI runs both alongside earlier suites.
