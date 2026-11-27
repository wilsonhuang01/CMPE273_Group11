# Cloud storage, state and messaging (action R)

Action R moves coordinator state off a single host so the AWS control plane (T) and
worker fleet (U) can use shared managed services. It is being delivered in phases.

| Phase | Scope | Status |
|---|---|---|
| 1 | S3 artifact backend; TLS-aware RabbitMQ (Amazon MQ) and Redis (ElastiCache) clients | **Implemented** |
| 2 | PostgreSQL repository backend (Amazon RDS) with versioned schema; SQLite stays the local default | **Implemented** |
| 3 | Database-backed dispatch claims, leases and fencing replacing the local pipeline lock | **Implemented** |
| 4 | Terraform for the S3 bucket, RDS, Amazon MQ and ElastiCache; broker/cache validation against the managed engines; secret rotation | **Implemented** (not yet applied to an AWS account) |

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
- **Versioned schema.** On first start, the repository creates the version 12 baseline
  and applies each later migration (`MIGRATIONS` in `storage/postgres.py`) in the same
  transaction and under the same lock, so concurrent starts are safe. Each applied
  version is recorded in `schema_version`, and existing databases upgrade in place (12 → 13
  adds dispatch claims). An unknown version is rejected without changing any data.
  There is no automatic downgrade.
- **Fencing unchanged.** Execution-lease fences use the database identity (host, port,
  database and schema, never credentials), so leases behave as with SQLite.

Phase 2 makes state durable and shared, so a replacement coordinator on another host
resumes every task, attempt count and decision. Phase 3 (below) makes it safe to run
several coordinators against the same database at once.

## Dispatch claims and fencing (phase 3)

Coordinators no longer rely on the local `pipeline.lock` to decide who runs a task.
Schema version 13 adds `dispatch_claims`, and each dispatcher works like this:

1. **Claim.** In one serialized write transaction, take the oldest due, unclaimed task for
   this execution backend. A claim is a random token, an owner ID
   (`host:pid:random`), an expiry and a **fence** that increases on every new claim. A
   task with a live claim can't be claimed by anyone else.
2. **Renew.** While the task runs, a background thread extends the lease every third
   of `DISPATCH_LEASE_SECONDS`. If renewal finds the claim gone, the task's work is told
   to stop.
3. **Fence every write.** The claim is attached to the execution context, including the
   cohort's worker threads. Every repository write in that context first checks, inside
   its own transaction, that the claim is still this coordinator's and has not expired.
   If not, the write raises `ClaimLost` and commits nothing. A stalled or partitioned
   coordinator therefore can't record late results, attempts or decisions once a
   replacement has taken over.
4. **Release or expire.** A finished, failed or interrupted dispatch releases its claim.
   The fence is kept, so later owners continue the sequence. A coordinator that dies
   without releasing leaves a claim that expires after the lease. Any other
   coordinator, or the same one after a restart, then takes it over (`dispatch_takeover`
   event, next fence), confirms cleanup of the previous owner's sandboxes and resumes
   from committed checkpoints.

Lease times use the database clock on PostgreSQL, so clock skew between coordinator hosts
cannot shorten or extend a lease. Claim events (`dispatch_claimed`, `dispatch_takeover`,
`dispatch_claim_lost`, `dispatch_renew_failed`) are structured log events with the
task, owner and fence.

| Variable | Meaning |
|---|---|
| `DISPATCH_LEASE_SECONDS` | Claim lease (default 30, at most 300). A crashed coordinator's task resumes after at most this long. |
| `DISPATCH_CONCURRENCY` | Maximum live claims across **all** coordinators sharing the database (default 1). Above 1 requires `EXECUTION_BACKEND=local`. |

With the default concurrency of 1, extra coordinators are **hot standbys**: exactly one
task runs at a time across the deployment, as before, and a standby takes over within
one lease if the active coordinator fails. Raising the limit lets coordinators with
their own local Docker engines run different tasks in parallel. It is refused for the
remote backend because fleet workers are shared across tasks and each holds one
recovery assignment, so parallel tasks would contend for the same workers. Lifting that
needs task-scoped worker assignment, which is outside action R.

Each coordinator runs one dispatcher. Standalone CLIs (`./project optimize`, pipeline
and evaluation commands) still take `pipeline.lock` exclusively, and dispatchers take it
shared, so a running CLI and a dispatcher on the same state directory still exclude each
other.

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

## Managed services, rotation and validation (phase 4)

The action Q stack in [`infra/aws/environment`](../infra/aws/environment) now also
creates the services the coordinator connects to. Everything is in the private data
tier and accepts only TLS from the coordinator, scheduler and worker security groups.
See [AWS infrastructure](aws-infrastructure.md) for sizes and costs per environment.

| Service | Engine | Configuration |
|---|---|---|
| Artifacts (`storage.tf`) | S3 | SSE-KMS with the `data` key and bucket keys; public access blocked; TLS 1.2+ only. The bucket policy **refuses any object write without `If-None-Match: *`**, so artifacts can't be overwritten by any client. Versioning keeps deleted or superseded versions for `artifact_retention_days`. Production adds Object Lock (governance mode, `evidence_retention_days`). Live artifacts never expire. |
| State (`database.tf`) | RDS for PostgreSQL 16 | `rds.force_ssl`, TLS 1.2+, storage encrypted with the `data` key, Multi-AZ and 14-day backups in production, deletion protection and a final snapshot for retained environments. |
| Broker (`messaging.tf`) | Amazon MQ for RabbitMQ 3.13 | AMQPS only, not publicly accessible, encrypted with the `data` key. |
| Cache (`messaging.tf`) | ElastiCache for Valkey 8.0 | TLS required, AUTH required, at-rest encryption with the `data` key. Two nodes with Multi-AZ failover in production. Durable quarantine evidence stays in the database, so losing the cache loses nothing. |

The coordinator's task role can read and create objects under `artifacts/`. It can't
delete them. The non-secret coordinator variables (`ARTIFACT_*`, `DATABASE_URL`,
`RABBITMQ_*`, `REDIS_*` and the three secret IDs) are published as
`data_services.coordinator_environment` in the environment configuration for the
control plane (T) to inject. Their image must contain the RDS CA bundle at
`/etc/ssl/certs/rds-global-bundle.pem`.

### Credentials never pass through Terraform

| Credential | Created by | Rotation |
|---|---|---|
| Database login | RDS (`manage_master_user_password`), in a secret encrypted with the `secrets` key | RDS rotates it every `database.rotation_days` (30). The password changes in place, so a rejected login makes the coordinator re-read the secret and retry once. |
| Broker login (`rabbitmq-password`) | `./project infra up` | The `rotate-rabbitmq` function (in the VPC) uses **alternating users**. It sets a new password on whichever of `optimizer`/`optimizer-alt` is not current, through the RabbitMQ management API. The current login keeps working until the next rotation. |
| Cache token (`redis-auth-token`) | `./project infra up` (random value, then `ROTATE` + `SET`) | The `rotate-redis` function issues `ROTATE`. ElastiCache keeps the two newest tokens, so the current token stays valid until the next rotation. |

Every `rotation_days` (30), Secrets Manager invokes the rotation functions in
[`infra/aws/lambda/rotation.py`](../infra/aws/lambda/rotation.py). A retried step is
safe, because an applied cache token is recorded with an `APPLIED` version label and is
not sent twice.

Amazon MQ needs a first password when the broker is created, and Terraform 1.5 can't
keep that out of state. That bootstrap password is therefore the one value in the
(encrypted) state. `infra up` stores it in the secret and rotates twice right away, so
it stops working before any service uses the broker. The cache is created without a
token for the same reason, and `infra up` then enables AUTH. ElastiCache accepts
unauthenticated clients between its `ROTATE` and `SET` steps. That window is closed
before `up` finishes, and the secret is tagged `AuthEnforced` only after `SET`
completes.

### Rotating credentials in the services

With a secret ID, a service reads the credential from Secrets Manager each time it
opens a connection, through a 60-second per-process cache (`runtime/secrets.py`), so a
rotation needs no restart. Open connections are unaffected. Secret outages surface as
the usual connection errors: AMQP connection errors for the broker, `redis.ConnectionError`
for the cache and `UnavailableError` for the database. Values never appear in logs,
`repr` or error messages.

| Variable | Meaning |
|---|---|
| `RABBITMQ_SECRET_ID` | Secret with `{"username", "password"}`. The username overrides `RABBITMQ_USER`. Can't be combined with `RABBITMQ_PASSWORD`. |
| `REDIS_SECRET_ID` | Secret holding the AUTH token (or `{"username", "password"}` for RBAC). Can't be combined with `REDIS_PASSWORD`. |
| `DATABASE_SECRET_ID` | The RDS-managed secret. Requires `DATABASE_URL`. Can't be combined with `DATABASE_PASSWORD`. |

The task roles of the coordinator and scheduler can read these secrets. The worker
instance role already could. Secret IDs need `boto3` (`pip install -e '.[aws]'`).

### Validating the managed engines

`python -m compiler_optimizer.runtime.service_check [broker|cache|database|artifacts]`
runs the coordinator's own client code against whatever the environment points to:

- **Broker:** the real topology (declared twice), publisher confirms, an unroutable
  mandatory publish being returned, nack/requeue with the redelivered flag, rejection to
  a dead-letter exchange with `x-death`, and the proposal queue's per-message TTL retry
  queue dead-lettering back to the main exchange.
- **Cache:** an unauthenticated client being refused, and the quarantine commands
  through `RedisBlacklist`: `SET NX PXAT`, no overwrite and expiry.
- **Database:** TLS, the full schema migration in a throwaway schema (dropped
  afterwards), and an unencrypted connection being refused.
- **Artifacts:** an S3 round trip and deduplication under `artifacts/service-check/`.

The data tier can't be reached from outside the VPC. `./project infra validate --env <name>`
therefore invokes the `service-check` Lambda function, which runs in the service
subnets with the coordinator's security group and secrets. The launcher builds that
package reproducibly (the wheel plus linux/arm64 drivers and the RDS CA bundle).

`tests/test_managed_engines.py` runs the same checks against RabbitMQ 3.13 and Valkey 8.0
in Docker with TLS and authentication. The CI `managed-engines` job runs it on every push.

**Not yet done:** none of this has been applied to an AWS account. Read-only plans
succeed for all three environments (150, 147 and 148 resources). Until `infra up` and
`infra validate` run against real services, Amazon MQ's and ElastiCache's behavior is
validated only through their engine versions, not through the managed services.

## Tests

Dispatch claims are covered in `tests/test_dispatch.py`. The tests check that claims are
exclusive, that a takeover after expiry fences the previous owner's writes and stale
releases, that released claims keep their fence, that backends stay separate, and that
the cross-coordinator cap holds. A standby takes over an abandoned task and completes it,
a lease is renewed through work longer than itself, and a coordinator whose claim is
replaced stops without recording an outcome. `tests/test_dispatch_docker.py` runs real
`./project serve` processes. A killed coordinator's restart takes over its expired claim,
and with two live replicas, killing whichever owns the task hands it to the other, which
completes it with fence 2.

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
These tests do not show behavior against real AWS services.

`python -m pytest tests/test_secret_rotation.py` covers both rotation strategies with
stand-ins that enforce Secrets Manager's staging-label rules, including retried steps,
configuration checks and stray versions. It also covers connect-time secret reads for all
three clients, the database's single retry after a rejected password, and service-check
reporting. `MANAGED_ENGINE_TESTS=1 python -m pytest tests/test_managed_engines.py`
runs the service check and the broker rotation against the real RabbitMQ management API,
and models two-token cache rotation with Valkey ACL passwords.
