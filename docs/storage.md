# Durable state and artifacts

The first storage backend is SQLite, owned by the coordinator, plus a local
content-addressed artifact directory. Redis caches scoped crash quarantines;
it is not the system of record.
No additional Python dependencies or database server are required.

## Initialize storage

With the development environment activated:

```python
from compiler_optimizer.storage import Repository

repo = Repository("var")
print(repo.list_tasks(status="accepted"))
```

This creates `var/state.sqlite3` and `var/artifacts/`. `var/` is ignored by Git.
Constructing a new repository for the same directory reopens its state. SQL schema
version 8 is tracked with SQLite `user_version`. The initial `storage/schema.sql`
and additive `storage/benchmark.sql`, `storage/pipeline.sql` and `storage/agents.sql`
migrations plus `storage/selection.sql`, `storage/recovery.sql`, `storage/execution.sql`
and `storage/fleet.sql` ship with the application; version 1–7
data migrates transactionally.

Schema v7 adds `submission_executions`, the automatic dispatch queue and frozen
operator configuration. RPC intake commits its row atomically with the task.
The local pipeline lock serializes dispatch; committed baseline/cohort evidence
provides restart checkpoints. See [automatic execution](automatic-execution.md).
Schema v8 adds the fleet registry and primitive work queue. Capability profiles,
incarnations, hashed worker credentials, job/parent leases, result references and
cleanup state remain coordinator-owned. Independent workers exchange artifacts
and fenced results over RPC; they never open coordinator database files.
Unknown database versions are rejected without deleting or replacing existing data.
Future migrations must run transactionally; there is no automatic downgrade.

[Fault recovery](fault-recovery.md) adds fenced jobs/attempts, worker health,
local blacklist evidence and a transactional RabbitMQ/Redis notification outbox.

Each operation opens/closes its own connection. Foreign keys, WAL mode, a
10-second lock timeout, and `synchronous=FULL` are enabled. Writes use
`BEGIN IMMEDIATE`; a failed operation rolls back its message, entity records,
artifact links, and task-history changes together.

## Persisted records and API

- `save_submission(Submission)` records the accepted task, immutable baseline,
  original manifest/build settings, and original package/source artifact links.
- `transition(task_id, expected_revision=..., status=..., reason=...)` starts a
  task or marks it failed. Status flows from `accepted` to `running` or `failed`;
  running tasks become `completed` through selection, or become `failed`.
  Completed/failed tasks are terminal. Revision checks reject competing updates.
- `save_request(OptimizationRequest)` records an agent assignment against a known
  source belonging to that task. `save_proposal(Proposal)` appends its validated
  response and source history. Existing proposals cannot be overwritten.
- `save_job(VerificationJob)` records each job attempt. Retries retain the same
  task, subject, source, manifest, and compiler, increase the attempt by one, and
  use a new message identity. Successful jobs cannot be retried. The
  [recovery controller](fault-recovery.md) enforces worker assignments, leases,
  deadlines, fencing and bounded retry policy for cohort workflows.
- `save_result(VerificationResult)` matches the stored current job attempt,
  persists correctness/failure details and measurements, links optional logs,
  and writes successful measurement data as an artifact.
- `select_result(task_id, job_id, attempt, expected_revision=..., reason=...,
  scores={...})` records an immutable final decision and completes the task in
  one transaction. The selected result must be successful and current; the
  baseline must also have passed verification. Selecting the baseline retains
  the original source. Reasons and finite numeric scores are preserved for audit.
- `get_task`, `list_tasks`, `history`, `list_proposals`, `list_jobs`, `list_results`,
  `list_artifacts`, and `get_decision` return persisted records. Protobuf objects
  are deserialized afresh, so callers cannot modify stored records by mutation.
  Filter accepted/running tasks at coordinator startup to discover unfinished work.

Save methods return `True` on first acceptance and `False` for an identical
message redelivery, including after a task completes. Message IDs are globally
unique across persisted task message types. Reusing a message ID with changed
contents, or creating an existing entity under a new message ID, raises
`ConflictError`. State and relationship checks run in the same transaction as
deduplication. Transport handlers should acknowledge only after this transaction
commits. The [Performance pipeline](performance-pipeline.md) additionally persists
delivery intent with its proposal, publishes with RabbitMQ confirms and acknowledges
after saving its final report. The [cohort recovery controller](fault-recovery.md)
also persists worker leases, fenced attempts and an outbox for bounded retries and
blacklist/dead-letter notifications. Remote multi-host dispatch remains deferred.

The [specialized agents](specialized-agents.md) add durable per-role iteration
budgets, cached input/output artifacts, required review evidence and frozen
measurement work. New workflow reports include the bound reviews and work record;
the repository enforces them before measurement and candidate selection.

[Coordinator selection](coordinator-selection.md) freezes a policy/cohort before
verification, preserves per-candidate progress and commits its recomputed ranking
audit atomically with the final decision. `get_selection_session`,
`selection_progress`, `preview_selection` and `selection_report` expose this state.

The repository validates v1 contracts and task relationships; it does not run
tests or determine whether a worker is truthful. Local recovery binds results to
assigned, leased workers; authentication for remote worker reports is still deferred.
The [selection coordinator](coordinator-selection.md) implements voting, scoring
and automatic selection, with the repository recomputing and enforcing its audit.
Proposal selection requires [bound correctness evidence](verification.md)
and the latest [reliable benchmark improvement](benchmarking.md). `record_benchmark`
validates benchmark reports and commits their artifact links and invalidation
atomically; `list_benchmarks` returns immutable run records.
Reference invalidation and its verification result are committed atomically.

## Artifact lifecycle

`repo.artifacts` exposes an `ArtifactStore` with `put(bytes)`, `put_file(path)`,
`put_stream(binary_stream)`, and `get(ArtifactRef)` methods. Writes return the v1
artifact reference used by submissions, proposals, and results. Files live under
`sha256/<first-two-hex-digits>/<full-digest>` and identical bytes share one object.
Keys are generated by the store and must match the declared digest when read;
user-supplied arbitrary paths and URLs are not accepted by this backend.

Writes stream to a temporary file, enforce a size cap, flush/fsync it, publish
through a no-overwrite hard link, and sync the destination directory. Concurrent
writers of the same object deduplicate. Existing objects are verified rather than
overwritten. Reads reject nonregular files and symlinks and check byte count and
SHA-256 before returning data. The default per-object cap is 128 MiB; standalone
`ArtifactStore` instances may configure `max_bytes`. This is a storage cap, not a
change to the submission's 10 MiB total file-content limit. The
[package loader](baseline.md) validates local directories and stored bundles; the
[sandbox runner](sandbox.md) enforces execution limits. Public arbitrary-archive
upload ingestion remains outside this backend.

Publish artifacts before saving messages that reference them. The repository
verifies their contents before committing those references. Submission packages
and baseline sources have task ownership; candidate sources identify their
proposal. Logs and benchmark artifacts identify their `job_id/attempt`. Successful
benchmark artifacts contain binary protobuf `VerificationSuccess` messages;
the full `VerificationResult` also remains in SQLite. Repeated benchmark plans,
raw samples and JSON reports additionally use their benchmark run ID as owner.

To attach a compiled executable or additional log/measurement artifact after its
job has been recorded, call:

```python
# repo, task_id, job_id, and attempt come from the active verification job.
binary_ref = repo.artifacts.put_file("build/bin/sum-array")
repo.attach_artifact(task_id, binary_ref, kind="binary", job_id=job_id, attempt=attempt)
```

`list_artifacts(task_id)` includes `kind` and `owner_id` alongside each reference.
The job must belong to the task, and the task must still be running.

The filesystem and SQLite do not share a transaction. A crash after publishing a
blob but before a database commit can leave an unreferenced object or `.upload-*`
temporary file. This is preferable to committing a reference before publishing
the blob. Automatic garbage collection/deletion is intentionally absent; retain
objects until a future reachability-based cleanup process can prove they are unused.

## Deployment and recovery boundaries

Keep the state directory on a persistent, service-owned local filesystem and keep
it out of writable sandbox mounts. The root and its ancestors must be trusted;
this backend does not defend against an administrator or another process that can
rewrite the storage directory while it is in use. Hard links and directory fsync
must be supported. Artifact reads currently return bounded bytes in memory.

All SQLite connections must run on the same host. A future remote worker fleet
must communicate through the coordinator rather than opening this database across
a network filesystem. WAL requires shared memory on one host, as described in the
[SQLite WAL documentation](https://www.sqlite.org/wal.html). The commit durability
setting follows [SQLite synchronous documentation](https://www.sqlite.org/pragma.html#pragma_synchronous).
This backend supports process restarts and concurrent local callers; it does not
replicate state or tolerate loss of the storage host/disk. Multiple coordinator
hosts will require a shared database such as PostgreSQL and an object-store backend.

For a consistent backup, stop writers and copy the entire state directory,
including any `-wal`/`-shm` files and all blobs. Restore that directory as a unit.
Do not copy only the live `.sqlite3` file while writers are running. The evaluation
runner uses SQLite backup plus immutable CAS hard links to fork a quiescent
baseline checkpoint; this is not a general live backup/replication service.
Replication, automated backup scheduling and retention remain deferred. Local
task/worker recovery is implemented separately by the [recovery controller](fault-recovery.md).

Run the storage checks with `python -m pytest tests/test_storage.py`. They cover
reopening data, abrupt process exit after a committed write, complete audit history,
baseline fallback, immutable decisions, stale-result rejection, rollback,
concurrent updates/publication, deduplication, and artifact integrity/path checks.
