# Cloud storage, state and messaging (action R)

Action R moves coordinator state off a single host so the AWS control plane (T) and
worker fleet (U) can use shared managed services. It is being delivered in phases.

| Phase | Scope | Status |
|---|---|---|
| 1 | S3 artifact backend; TLS-aware RabbitMQ (Amazon MQ) and Redis (ElastiCache) clients | **Implemented** |
| 2 | PostgreSQL repository backend (Amazon RDS) with versioned schema; SQLite stays the local default | **Implemented** |
| 3 | Database-backed dispatch claims, leases and fencing replacing the local pipeline lock | Planned |
| 4 | Terraform for the S3 bucket, RDS, Amazon MQ and ElastiCache; broker/cache validation against the managed engines; secret rotation | Planned |

Local development is unchanged: with no new variables set, the coordinator uses
SQLite plus the local content-addressed directory, and plaintext AMQP/Redis on the
Compose network.

## S3 artifact backend

`S3ArtifactStore` keeps the contract of the local [artifact store](storage.md#artifact-lifecycle):

- **Keys are derived, never chosen.** Objects live at
  `s3://<bucket>/<prefix>/sha256/<xx>/<digest>`. The persisted `ArtifactRef.key` is the
  same `sha256/<xx>/<digest>` used locally, so references stay portable between backends.
  The bucket and prefix are deployment configuration rather than data.
- **No overwrites.** Uploads use a conditional `If-None-Match: *` put. If the object
  already exists, the store reads and verifies it, and a corrupt or conflicting object
  raises `ArtifactError` instead of being replaced. A racing conditional put
  (`409 ConditionalRequestConflict`) is retried up to three times.
- **End-to-end integrity.** Each put sends `ChecksumSHA256`, so S3 rejects bytes damaged
  in transit. Every read checks `Content-Length` before it downloads the body, then checks
  the byte count and SHA-256 before returning the data.
- **Bounded transfer.** The per-object cap (128 MiB by default) is checked while
  streaming, before any upload. Bodies over 8 MiB are staged in a temporary file
  rather than in memory. The boto3 client uses 5 s connect and 30 s read timeouts
  and standard-mode retries.
- **Encryption.** With `ARTIFACT_S3_KMS_KEY_ID`, puts request SSE-KMS with that key,
  intended to be the action Q `data` key. Without it, the bucket default applies.
- **Failures are typed.** A missing object raises `NotFoundError`. Integrity failures raise
  `ArtifactError`. Throttling, timeouts and transport errors raise `UnavailableError`. That
  is a `ConnectionError`, so the existing RPC handlers return gRPC `UNAVAILABLE` and
  callers can retry. A failed integrity check is never treated as transient.
- **Health.** `Repository.check_health` now also checks the artifact backend
  (`HeadBucket` for S3), so coordinator readiness fails when the bucket is unreachable.

S3 and the database do not share a transaction, the same as with the local backend.
Blobs are published before the messages that reference them, so a crash can leave
an unreferenced object but never a dangling reference.

Only the coordinator opens the S3 store. Workers and agents keep exchanging content
through the coordinator's authenticated transfer RPCs, so they need no S3 permissions.
The agents' private caches stay local.

### Configuration

| Variable | Meaning |
|---|---|
| `ARTIFACT_BACKEND` | `local` (default) or `s3`. `s3` is accepted only for the coordinator role. |
| `ARTIFACT_S3_BUCKET` | Required with `s3`. |
| `ARTIFACT_S3_PREFIX` | Optional key prefix, e.g. `staging/artifacts`. `..` and empty segments are rejected. |
| `ARTIFACT_S3_KMS_KEY_ID` | Optional KMS key ID, ARN or alias for SSE-KMS. |
| `AWS_REGION` | Region for the S3 client; credentials come from the default AWS chain (ECS task role, SSO profile, …). |
| `S3_ENDPOINT_URL` | Optional S3-compatible endpoint for testing. Must be HTTPS, or HTTP on loopback only; credentials in the URL are rejected. |

The S3 backend needs `boto3`: `pip install -e '.[aws]'`.

With phase 2, `DATABASE_URL` also moves task state off the coordinator host; see below.

## PostgreSQL repository (phase 2)

With `DATABASE_URL` set, the coordinator keeps all task state in PostgreSQL instead of
`STATE_DIRECTORY/state.sqlite3`. All storage modules run unchanged on either engine.
`storage/postgres.py` runs the existing SQL on PostgreSQL:

- **Same schema, same order.** `storage/postgres.sql` is the SQLite version 12 schema
  with 64-bit `BIGINT` columns and `BYTEA` payloads. Columns stay in the same order
  because the repository inserts rows by position. Each table has a hidden trailing
  `rowid` identity column, so `ORDER BY rowid` keeps SQLite's insertion order. Rows hide
  it and support both `row[0]` and `row["name"]`, like `sqlite3.Row`.
- **Two dialect rewrites only.** `?` becomes `%s`, and `INSERT OR IGNORE` becomes
  `INSERT … ON CONFLICT DO NOTHING`. Parameters are bound client-side, so untyped
  expressions such as `? IS NULL` behave as they do in SQLite. Python booleans are
  stored as 0/1, as SQLite stores them.
- **Same transaction semantics.** Every write transaction first takes a
  transaction-scoped advisory lock for its schema. Writers are therefore serialized,
  as with SQLite `BEGIN IMMEDIATE`, and every statement sees all previously committed
  writes (READ COMMITTED). Reads run as REPEATABLE READ, READ ONLY snapshots. Lock waits
  are capped at 10 s and statements at 30 s.
- **Same errors.** Unique, foreign-key and check violations raise `ConflictError`.
  Connection loss, timeouts and server errors raise `UnavailableError`, which the RPC
  handlers map to `UNAVAILABLE`. A failed transaction is rolled back as a whole.
- **Failover-tolerant pool.** Each coordinator keeps up to four idle connections. If a
  restart or RDS failover has closed a pooled connection, the next transaction replaces
  it before running anything, so callers don't see the dead connection. Connections are
  never shared across `fork`.
- **Versioned schema.** On first start, the repository creates the schema and records
  version 12 in `schema_version`, under the same lock, so concurrent starts are safe.
  An unknown version is rejected without changing any data. Schema changes after
  version 12 must be added as transactional steps for both engines. There is no
  automatic downgrade.
- **Fencing unchanged.** Execution-lease fences use the database identity (host, port,
  database and schema, never credentials), so leases behave as with SQLite.

Phase 2 makes state durable and shared, so a replacement coordinator on another host
resumes every task, attempt count and decision. It does **not** yet make concurrent
coordinators safe. Automatic execution is still guarded by the local `pipeline.lock` in
`STATE_DIRECTORY`, so run **one** coordinator per database schema until phase 3 adds
database-backed claims and fencing.

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | `postgresql://user@host:5432/dbname`. Accepted only for the coordinator. It must not contain a password. Any non-loopback host requires `sslmode=verify-full`, e.g. `?sslmode=verify-full&sslrootcert=/etc/rds/global-bundle.pem` for RDS. |
| `DATABASE_PASSWORD` | Password, injected from Secrets Manager. Never logged or shown in `repr`. `PGPASSWORD` also works. |
| `DATABASE_SCHEMA` | Schema holding the tables (default `optimizer`). Separate schemas isolate environments in one database. |

Install the driver with `pip install -e '.[postgres]'` (psycopg 3). SQLite remains the
default and needs nothing extra. `./project` tools other than `serve`, such as
evaluation checkpoints and the standalone CLIs, still use local SQLite state.

## TLS for the broker and cache

All RabbitMQ and Redis connections, including readiness probes, the proposal queue,
fleet dispatch and the quarantine cache, are now built by `runtime/connections.py`.

| Variable | Meaning |
|---|---|
| `RABBITMQ_TLS` | `true` connects with AMQPS. The port defaults to 5671 (Amazon MQ) unless `RABBITMQ_PORT` is set. |
| `RABBITMQ_CA` | Optional CA bundle for a private broker certificate. Requires `RABBITMQ_TLS=true`. |
| `REDIS_TLS` | `true` connects with TLS, as required by ElastiCache in-transit encryption. |
| `REDIS_CA` | Optional CA bundle. Requires `REDIS_TLS=true`. |
| `REDIS_USERNAME` | Optional user for ElastiCache RBAC. When unset, `REDIS_PASSWORD` is sent as the AUTH token. |

With TLS enabled, clients require TLS 1.2 or later, a valid certificate chain and a
matching hostname. Certificate checks cannot be turned off; use a CA bundle instead.

## Tests

`python -m pytest tests/test_postgres_storage.py` checks SQL translation, rows and
configuration offline. With a server, `--postgres-url` (or `TEST_POSTGRES_URL`) adds
tests for state shared between separate coordinator roots, durability across an abrupt
process exit, whole-transaction rollback, writer serialization, lock-timeout and outage
errors, replacing connections killed by a server restart, read-only snapshots and
rejecting an unknown schema version.

The same option reruns the **entire** suite on PostgreSQL. Every `Repository(root)`
gets its own schema, and spawned `./project serve` coordinators are pointed at it:

```sh
initdb -D /tmp/pg -U optimizer --auth=trust
pg_ctl -D /tmp/pg -o "-p 55432 -k '' -c listen_addresses=127.0.0.1" -l /tmp/pg.log start
createdb -h 127.0.0.1 -p 55432 -U optimizer optimizer_test
python -m pytest -m "not docker" --postgres-url postgresql://optimizer@127.0.0.1:55432/optimizer_test
```

Tests marked `sqlite_only` (SQLite migration history, file inspection and the evaluation
checkpoint copy) are skipped in that mode.

`python -m pytest tests/test_cloud_storage.py` runs offline against an in-memory S3
client that models conditional puts, checksums and error codes. It covers round trips,
deduplication, corruption detection without overwrite, racing puts, size limits,
spooling, distinct missing/integrity/outage errors, a repository committing task state
against S3 blobs, settings validation and the TLS parameters of both clients.
These tests do not show behavior against real AWS services. That validation is part
of phase 4 and of action W.
