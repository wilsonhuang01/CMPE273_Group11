# Cloud storage, state and messaging (action R)

Action R moves coordinator state off a single host so the AWS control plane (T) and
worker fleet (U) can use shared managed services. It is being delivered in phases.

| Phase | Scope | Status |
|---|---|---|
| 1 | S3 artifact backend; TLS-aware RabbitMQ (Amazon MQ) and Redis (ElastiCache) clients | **Implemented** |
| 2 | PostgreSQL repository backend (Amazon RDS) with migrations; SQLite stays the local default | Planned |
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

The SQLite database still lives in `STATE_DIRECTORY` during phase 1. That is enough for
a single coordinator host whose blobs are durable in S3. Phase 2 removes the remaining
single-host state.

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

`python -m pytest tests/test_cloud_storage.py` runs offline against an in-memory S3
client that models conditional puts, checksums and error codes. It covers round trips,
deduplication, corruption detection without overwrite, racing puts, size limits,
spooling, distinct missing/integrity/outage errors, a repository committing task state
against S3 blobs, settings validation and the TLS parameters of both clients.
These tests do not show behavior against real AWS services. That validation is part
of phase 4 and of action W.
