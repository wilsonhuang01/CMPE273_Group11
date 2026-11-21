# Version 1 message contracts

The canonical service and queue schema is
[`contracts.proto`](../src/compiler_optimizer/shared/v1/contracts.proto), in the
`compiler_optimizer.v1` namespace. Python message classes, type stubs, and gRPC
client/server stubs are checked in beside it. These contracts are used by the
implemented local runtime, host coordinators, agents, broker adapter and recovery
controller. See [current progress](README.md) for execution and deployment boundaries.
The [automatic host coordinator](automatic-execution.md) atomically schedules
`SubmitSubmission` requests for execution; Compose's coordinator is intake-only.

## User submission format

Keep the existing `submission.json` format from the
[initial workload contract](initial-workload.md). The packaged
[`submission.schema.json`](../src/compiler_optimizer/shared/v1/submission.schema.json)
defines its structural constraints. `parse_manifest` adds strict integer checks,
safe relative paths, language/source extension matching, duplicate test ID checks,
benchmark references, UTF-8 argument byte limits, and duplicate JSON key rejection.
It returns a protobuf `SubmissionManifest` without mutating dictionary inputs.

```python
from pathlib import Path
from compiler_optimizer.shared.manifest import parse_manifest

manifest = parse_manifest(Path("examples/sum-array/submission.json").read_bytes())
print(manifest.language)
```

Pass original JSON bytes/text at ingestion so duplicate keys remain detectable.
Manifest validation does not read referenced files or inspect archives. Package
ingestion must additionally reject symlinks, missing files, nonregular entries,
path escapes, and packages exceeding the file-count or size limits. Workers must
enforce sandbox policy; a valid manifest is not authorization to execute it.

## Shared identity and provenance

Every top-level message has `meta` with `schema_version = 1`, a unique `message_id`,
`task_id`, and `created_at_unix_ms`. IDs are canonical lowercase, nonzero UUIDs.
Timestamps are positive Unix milliseconds through year 9999. Idle/quarantined
heartbeats, and their receipts, leave `task_id` empty; busy heartbeats identify the
task and current job. Validation does not require synchronized clocks against the
local machine; receivers use their own receive times for health deadlines.

`ArtifactRef` contains a storage-relative opaque `key`, lowercase SHA-256 digest
of the artifact's exact bytes, and byte count. Keys cannot contain traversal,
absolute paths, backslashes, or URL schemes. The [local artifact store](storage.md)
implements storage/retrieval with size and digest verification on reads.

`SourceVersion` identifies an immutable source/header-only bundle. The baseline
is version 1 with no parent. A proposal increases its requested parent's version
by one and records the parent's bundle hash. Sibling proposals can share a version
number: `(task_id, proposal_id)` identifies a candidate, not the version alone.
Bundle hashes refer to exact archived bytes. The package loader uses a stable
archive layout so identical source trees produce identical hashes.

`CompilerSettings` records `clang`/`clang++`, a normalized Clang 18 semantic version
(optionally followed by build text), the worker image's `sha256:` digest, language,
the selected controlled CPU target, and ordered flags. For `linux-x86_64` the flags must be exactly:

```text
-std=c17 -O2 -march=x86-64 -mtune=generic -Wall -Wextra
```

`linux-aarch64` uses `-march=armv8-a -mtune=generic` with the same language,
optimization and warning flags. `linux-riscv64` uses the controlled cross-compiler
flags and correctness-only emulator policy in [Architecture support](architectures.md).
Workers may advertise supported CPU targets without duplicates. CUDA is
not an admitted CPU language or execution target.

C++17 uses `-std=c++17`. Source arguments, controlled include paths, `-lm`, and the
output path are appended by the worker as specified in the workload contract;
these are not user-supplied flags. The coordinator assigns the build profile and
workers check their actual toolchain against it. Contract validation
checks declarations, not the existence of the declared compiler image.

## Message responsibilities

- `Submission`: versioned task/request metadata, validated manifest, original
  package artifact (including tests), baseline source bundle, and build profile.
- `OptimizationRequest`: assigned proposal ID and agent role, manifest, parent
  source, and fixed build profile. Roles cover Performance, Memory, Correctness,
  Architecture, Critic, and Benchmark. Their inputs, outputs, limits and acceptance
  gates are implemented as described in [Specialized agents](specialized-agents.md).
- `Proposal`: proposal ID, role, derived source version, rationale, transformation
  identifier, and build profile. The identifier describes a pattern for later
  blacklist lookup; it is not by itself evidence that the pattern is unsafe.
- `VerificationJob`: job ID, either `baseline = true` or a proposal ID, source,
  manifest, build profile, positive attempt number, and an absolute deadline later
  than creation. The deadline covers the whole job; resource limits still apply
  independently to each compilation/execution.
- `WorkerHeartbeat`: stable worker ID, process-start `incarnation_id`, increasing
  sequence, state, supported targets, and the active task/job when busy. Sequence
  ordering is scoped to an incarnation and checked by the recovery repository.
- `VerificationResult`: task/job/attempt and subject identity, worker ID, source,
  build profile, hardware description, optional log artifact, and exactly one
  success or failure outcome.
- `Receipt`: new response metadata plus the accepted request/event's message ID.
  Services return it only after they have durably accepted the input. A receipt
  acknowledges acceptance, not successful optimization or verification.

Successful results list passed test IDs and benchmark measurements. Measurements
contain a test ID, 1–1000 finite positive elapsed-time samples in seconds, peak
memory in bytes, and warmup count. Benchmarked tests must have passed. When matched
to a job, results must cover exactly its correctness tests and benchmark test IDs.
Warmup/repetition policy and performance significance are implemented in separate
[versioned benchmark artifacts](benchmarking.md); these provisional result metrics
alone do not qualify a candidate for selection.

Failures identify their compile, test, measurement, or infrastructure stage and
reason: compilation error, output mismatch, nonzero exit, signal, timeout, memory
limit, output limit, infrastructure error, or unknown failure. Test/measurement
failures require a test ID. Exit codes and signals are optional evidence, are
mutually exclusive, and are required for their corresponding failure reasons.
Diagnostics are capped at 4096 UTF-8 bytes; larger logs belong in artifacts.

## Validation boundaries

Use `validate_message` on every inbound/outbound top-level protobuf message.
`encode_message` and `decode_message` do this automatically and cap payloads at
1 MiB. Larger sources and logs travel through artifact references.

Generated gRPC serializers do not run semantic validation automatically. Service
handlers must validate requests, validate returned messages, and translate
`ContractError` into gRPC `INVALID_ARGUMENT` for invalid requests. Invalid agent
responses must be rejected rather than accepted as verified code.

Use `validate_proposal_for_request` to check task/proposal IDs, role, immediate
parent version/hash, creation order, and identical compiler settings. Use
`validate_result_for_job` to reject wrong task/job/subject IDs, stale attempts,
changed source/build settings, and incomplete test/benchmark coverage. These
helpers take the stored assignment/job. The [repository](storage.md) adds
persistence, deduplication, and task-state transitions. Recovery enforces local
worker assignment, lease and fencing checks. The fleet additionally authenticates
worker incarnations and assignment tokens; per-user authorization remains deferred.

## Direct calls and queued work

gRPC definitions provide these direct calls:

- `CoordinatorService.SubmitSubmission(Submission) -> Receipt`.
- `CoordinatorService.SubmitProposal(Proposal) -> Receipt`.
- `CoordinatorService.GetOptimizationResult(OptimizationQuery) -> OptimizationResult`.
- `AgentService.GenerateProposal(OptimizationRequest) -> Proposal`.
- `WorkerHealthService.ReportHeartbeat(WorkerHeartbeat) -> Receipt`.
- `ArtifactTransferService.UploadPackage(stream PackageUploadFrame) -> Receipt`.
- `ArtifactTransferService.ListArtifacts(OptimizationQuery) -> TaskArtifacts`.
- `ArtifactTransferService.DownloadArtifact(ArtifactRequest) -> stream ArtifactChunk`.
- `FleetService.Ping`, `Register`, `Heartbeat`, `Schedule`, `Claim`, `CheckLease`,
  `UploadResult(stream FleetResultFrame)` and `ConfirmCleanup`.

The [fleet protocol](autonomous-workers.md) registers immutable capabilities and
issues expiring, fenced primitive compile/execute/environment assignments.
Workers authenticate with an incarnation secret and lease token. Result streams
enforce 64 KiB chunks, 32 MiB total, a bounded deadline and a content hash;
authority is checked again in the commit transaction. These messages use the
fleet service's explicit validators rather than the general event validator.

Callers must set per-call deadlines. gRPC is for direct request/response
coordination; scoped RabbitMQ proposal delivery triggers the workflow, and the
autonomous scheduler publishes primitive work for registered executors.
The submission RPC accepts already-ingested artifact references.
The [transfer API](artifact-transfer.md) ingests packages from clients without
shared filesystems, assigns trusted compiler metadata and invokes submission.
Transfer chunks are at most 64 KiB inside the existing 1 MiB message limit;
stream byte/frame/deadline bounds are enforced separately. The [local runtime](local-infrastructure.md) implements
the coordinator intake/result calls. [Worker health](fault-recovery.md) includes a
heartbeat RPC service for host-managed recovery. Agents run locally behind Python
interfaces; a standalone AgentService RPC server is not implemented.
The [Performance pipeline](performance-pipeline.md) adds a local result server for
host-owned state. Queries require canonical task UUIDs; responses provide status,
stage, final JSON/report reference and the selected source version when available.
Its scoped durable proposal routes append a proposal ID to `optimization.proposal.v1`.
These isolate the first milestone from the general scheduler queues.

`messaging.py` defines the durable topic exchange `compiler-optimizer.v1`:

- `optimization.proposal.v1`: `Proposal`, consumed by coordinator intake.
- `verification.job.v1`: `VerificationJob`, consumed by sandbox workers.
- `verification.result.v1`: `VerificationResult`, consumed by coordinator intake.
- `worker.heartbeat.v1`: `WorkerHeartbeat`, consumed by worker health monitoring.

Proposals and heartbeats may use the direct RPC or corresponding event route.
Choose one transport per logical send; fallback delivery must retain its identity
so the receiver can deduplicate it. The general job/result routes remain reserved;
the implemented fleet uses the separate durable direct exchange
`compiler-optimizer.work.v1`, per-worker queues, and `FleetDispatch` envelopes.
Results commit through RPC. The [scheduler runtime](local-infrastructure.md)
declares the queues, bindings and dead-letter sink. The implemented host workflow
uses scoped proposal consumers, publisher confirms, manual acknowledgments, TTL
retry queues and terminal notices as described in [Fault recovery](fault-recovery.md).

`encode_event` returns the route, serialized body, and AMQP property values:
`content_type = application/x-protobuf`, `type =` the protobuf descriptor's fully
qualified name, `message_id`, `correlation_id = task_id`, and `delivery_mode = 2`.
The `message_type` helper attribute maps to AMQP's `type` property. `decode_event`
selects the message class from a known route and validates the body. The scoped
proposal broker adapter also checks its envelope against the stored proposal.

RabbitMQ delivery is at least once. Broker redelivery preserves the message ID.
An explicit retry keeps the job ID, increments `attempt`, assigns a new message ID
and deadline, and preserves source, tests, and build settings. Receivers must
deduplicate messages and accept results only for the currently assigned attempt.
Persist acceptance before acknowledging queue deliveries. Invalid/unsupported
messages must be rejected without a requeue loop. Retry counts, infrastructure
failure classification, backoff, and dead-letter handling belong to recovery.

## Compatibility and regeneration

Correctness verification adds optional v1 job plan/input references and generated
test IDs, a result correctness-report reference, and `SANITIZER_FAILURE`.
Plan-bearing jobs require bound reports and complete supplied/generated coverage.
See [verification](verification.md) for evidence validation and legacy selection rules.

Only schema version 1 is accepted. Missing/zero versions and unknown enum values
are rejected. The user-facing manifest rejects unknown fields. Protobuf preserves
unknown fields for compatible additive evolution, but changing a field's meaning,
type, or required invariant needs a new package/route version. Never reuse field
numbers; reserve removed field names/numbers. Adding enum values requires receiver
updates before producers send them. Deterministic serialization is useful for
testing; protobuf serialization is not a canonical source-hashing format.

Install the development extra, then regenerate with:

```sh
python tools/generate_contracts.py
python tools/generate_contracts.py --check
python -m pytest tests/test_contracts.py
```

The generator is pinned to `grpcio-tools==1.76.0`. Generated files are excluded
from manual formatting/lint rules. CI regenerates into a temporary directory and
compares bytes, so a schema edit requires updating its checked-in outputs.

The generation layout follows the [gRPC Python guide](https://grpc.io/docs/languages/python/quickstart/)
and [protobuf language guide](https://protobuf.dev/programming-guides/proto3/).
