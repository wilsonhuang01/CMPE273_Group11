# Browser API and optimizer handoff (W02)

This is the version 1 implementation contract for W04 and the future AWS
adapter. It defines planned routes, not a running server. The
[W01 journey](website-demo.md) remains the product scope. Local development and
fixture validation need no AWS resources. AWS compatibility is pending deployment
handoff, not established by this document.

Machine-readable contracts:
[OpenAPI 3.1](../contracts/web/v1/openapi.json),
[JSON Schema 2020-12](../contracts/web/v1/schema.json), and
[synthetic fixtures](../contracts/web/v1/fixtures/). All fixture hashes, IDs,
results and previews are illustrative; they are not measured optimization evidence.
The schema closes objects to unknown fields. Missing evidence is `null` or an empty
collection, never fabricated. Nullable metadata permits reopening tasks after loss
of adapter-side display metadata. Incompatible schema/report versions fail closed.
Additive optional fields can extend v1; changed required fields or meanings need v2.

## Routes and messages

All JSON envelopes carry `schema_version: 1`. Browser URLs are same-origin and
relative. The Python service holds all endpoint, CA and token configuration.
Return `Cache-Control: no-store` for API responses and attachments; accept JSON
submission bodies only, at most 4 KiB. Enforce same-origin POST requests. No token,
raw provider response, stack trace, coordinator path or backend error text is
returned to the browser. Source preview strings are rendered using text nodes.

- `GET /api/v1/examples` → 200 `examples`. The server allowlist initially contains
  `sum-array`. Each entry includes the escaped-at-render-time source text, package
  digest, description, native target and readiness information. `example_sha256`
  is SHA-256 of the exact deterministic package bundle prepared for submission,
  not just `main.c`. Resolve readiness from operator-validated backend capability
  evidence; browser architecture and a responsive gRPC socket are insufficient.
  Unestablished capability returns an unavailable entry with `target: null` and
  a reason. A transport outage can return 503. No arbitrary filesystem input.
- `POST /api/v1/tasks` → 202 `receipt`, including on an identical retry of a
  completed task. The submission contains `request_id`, `example_id`,
  `example_sha256`, and explicit native `target`. `Location` and `status_url`
  both identify `/api/v1/tasks/{task_id}`. A receipt acknowledges durable intake,
  not successful compilation. The request never waits for optimization to finish.
- `GET /api/v1/tasks/{task_id}` → 200 `task`. Task IDs and request IDs must be
  canonical lowercase nonzero UUIDs. The response separates lifecycle `state`,
  backend `stage`, `reason`, task-scoped provenance, execution mode, optional
  `result` and download availability. Unknown task returns 404, not a failed task.
- `GET /api/v1/tasks/{task_id}/download?kind=bundle|source` → 200 ZIP attachment;
  default is `bundle`. Source is the selected-source ZIP, including the original
  when retained. Bundle is a ZIP containing the existing client's `result.json`,
  `report.json`, `artifacts.json`, selected source if present and verified raw
  evidence. No source exists for some failed tasks; diagnostics can still exist.
  Unavailable/pending artifacts return 409. Check task registration, offsets, size
  and SHA-256 through existing transfer helpers before publishing an attachment.
  Stage in bounded temporary storage (512 MiB verified input budget plus at most
  512 MiB output archive; reject larger output); clean up on success, failure and
  disconnect. Use ZIP_STORED and attachment names `task-{task_id}-{kind}.zip`.
  Do not accept artifact keys, hashes or filesystem paths as browser parameters.

A `result` exists only for a completed task with a final supported decision.
`outcome` is `candidate_selected` or `baseline_retained`; it is separate from
lifecycle. Required fields include decision reason, selected-source digest/size,
proposal states/reasons/checks, overall checks, optional runtime/peak-memory
statistics, qualification, context and report reference. Runtime values are in
milliseconds; peak RSS in KiB. Convert only from documented backend units.
`uncertainty` retains a labelled backend-reported interval/summary and
`repetitions` is the corresponding observation count. Do not mix methods or
workloads into a single statistic; use `null` and the downloadable report when
no comparable summary is available. No client-derived speedup is required.
Only expose sanitized summaries of known report fields, not arbitrary report JSON.
Empty checks/candidates mean no summarized evidence is available, not approval.

## Durable submission identity

Before sending, the browser persists a cryptographically random UUID request ID
and the full submission body. Retry after timeout, disconnect, lost receipt or
reload sends that exact body and ID. New run explicitly generates a new ID.

The adapter must atomically persist its own request ledger **before any upload**:
request ID, environment identity, example/package hash, target and the complete
`PackageUploadHeader` (including task/message IDs and creation timestamp).
Use `task_id = request_id`; generate the message ID once and preserve it.
This is adapter metadata, never access to coordinator storage. Reuse the stored
header with `OptimizerClient.submit`, including when the upload receipt was lost.
The coordinator already deduplicates an identical submission. Never recreate a
header with a fresh timestamp or message ID for a retry.

A duplicate ID with a different body/environment returns 409
`idempotency_conflict` before any backend call. Concurrent duplicates serialize
through a unique ledger key and reuse one header; multi-instance deployment needs
shared durable ledger storage. Preserve bindings for the deployment lifetime;
there is no automatic key expiry in v1. Persist package snapshots for unresolved
requests so an application update cannot change a retried upload. A digest changed
by a release requires a new run; never silently substitute new source.

Store ledger state on a durable volume locally and durable shared storage in a
multi-replica cloud runtime. On restart reuse the ledger. If it is unavailable,
fail readiness and return 503 without submitting. Restore it with the environment;
do not reconstruct lost identities automatically. An endpoint/environment switch
must not redirect an unresolved retry. Existing task links and artifact catalogs
remain bound to their original environment; the initial deployment serves one
environment per origin and does not migrate history.

## State mapping, polling and errors

Map current RPC statuses exactly: `accepted` → `queued`, `running` → `running`,
`completed` → `completed`. For `failed`, map to `rejected` only when the summary
contains `reason: baseline_rejected`; otherwise use `failed`. Candidate exclusion
or reviewer veto alone is not task rejection. Unknown statuses, malformed reports,
wrong task IDs or unsupported completed outcomes return 502 `incompatible_backend`
(or `integrity_error` for verified integrity failures), never invented success.
A failed intake before a task is accepted is an HTTP error, not a stored rejected
task. Terminal states are completed, failed and rejected.

Pass through only a bounded, recognized backend stage label; use `null` when absent
or unknown. Examples include `queued`, `baseline`, `optimizing`,
`collecting_candidates`, `completed` and `failed`. This label supplies no percentage
or guaranteed agent timeline. An infrastructure retry can retain a running state;
backend execution summary reasons must not be presented as a final failure early.

For completed cohort reports, `status: selected` maps to `candidate_selected` and
`retained_baseline` maps to `baseline_retained`. The older pipeline report needs
an explicit supported mapper before use; it cannot be assumed identical to the
cohort schema. Validate report/selected-source references against task-owned
artifacts. Corrupt or missing referenced evidence must not enable downloads.

Poll every 2 seconds while queued/running; allow one in-flight read. On transient
failures use jittered exponential backoff from 2 to 30 seconds and honor
`Retry-After`. Stop at terminal state or navigation; suspend reads when hidden and
resume on return. After 10 minutes, offer Resume polling without resubmission.
Connection state and the last-read time live in the browser, not task lifecycle.
Restart or polling timeout never cancels a task. Bound status RPCs to 10 seconds
and transfer/upload RPCs to 120 seconds (existing maximum); support cancellation
and do not hold submission open for execution.

Every HTTP failure uses the schema's `error` envelope with a safe message,
`retryable`, and nullable known request/task IDs. `retryable` means retry the same
operation/identity; it never authorizes a new task. HTTP mappings:

- 400 `invalid_request`: invalid UUID/JSON/schema/query, or RPC INVALID_ARGUMENT.
- 404 `example_not_found` or `task_not_found`: allowlist miss or RPC NOT_FOUND.
- 409 `idempotency_conflict`, `unsupported_target`, or `download_unavailable`.
- 413 `invalid_request`: request or transfer budget exceeded.
- 429 `rate_limited`: adapter admission limit; supply Retry-After.
- 503 `backend_unavailable`: transport/credential/readiness failure, including RPC
  UNAVAILABLE, UNAUTHENTICATED or PERMISSION_DENIED; secrets stay server-side.
- 504 `deadline_exceeded`: uncertain submission must retry the same identity.
- 502 `integrity_error` or `incompatible_backend`: failed hash/reference check,
  RPC FAILED_PRECONDITION, malformed/unsupported contract; operator action required.
- 503 `backend_unavailable` for RPC RESOURCE_EXHAUSTED unless a known size limit
  maps to 413; supply Retry-After for transient capacity exhaustion.
- 500 `internal_error`: unexpected adapter failure; preserve request identity.

Only transient transport/deadline/capacity/internal errors are retryable. A browser
404 for an environment-specific task must not trigger resubmission elsewhere.

## Adapter evidence boundary

Implement one server-side adapter interface in W04: `list_examples()`,
`submit(submission)`, `get_task(task_id)` and `download(task_id, kind)`. Local and
AWS connections share the schemas, normalization and transfer validation above;
only server configuration changes. Use `read_package`/`prepare_upload` and
`OptimizerClient.submit`, `.status` and `.download` rather than shell commands.
Remote gRPC requires TLS, validated trust and a server-resolved bearer secret.

Already available via RPC: `OptimizationResult.task_id/status/stage/report_json`,
optional report and selected source; `ListArtifacts` and `DownloadArtifact` expose
task-scoped evidence. Selection reports expose `reason`, selected proposal/source,
candidates, votes, exclusions and benchmark references. Detailed verification,
benchmark and compiler/worker context require reading the referenced artifacts.
Check sizes and hashes before interpreting them. Provider provenance must come
from task-scoped proposal/review evidence; configuration alone does not establish
that a live model call happened. Missing provenance maps to `unavailable`.

Web-derived/proposed fields are the catalog, readiness, request ledger, sanitized
checks/metrics/context, normalized state/outcome and download booleans. There is
no current RPC catalog or aggregated readiness API: use W03's operator-validated
capability record, fail closed if absent, and revalidate at deployment. A future
capability RPC would be an explicit extension. No additional task progress RPC is
required for this milestone; per-agent activity, retries and percentages are omitted
unless later supported by task-scoped evidence. W02 adds no backend RPC fields.

## AWS handoff record

Fill the following record in this document when the deployment branch supplies
staging. `pending` is not a usable configuration. Never put secret values here.
W12 must reconcile deployed contracts before changing endpoints; W13/W14 validate
the same fixtures/behavior and a real submission/download in staging.

```yaml
environment: pending
application_revision: pending
coordinator_revision: pending
browser_contract: v1
coordinator_proto_report_versions: pending
coordinator_endpoint: pending  # private DNS:port, server-side only
access_path: pending  # VPC/subnet/security-group or approved tunnel
web_runtime_placement: pending
public_origin: pending
tls_server_name: pending
tls_trust_reference: pending  # system trust or CA distribution reference
bearer_secret_reference: pending  # ARN/name only; rotation owner required
secret_rotation_owner: pending
native_targets_and_capability_evidence: pending
provider_mode_and_provenance_support: pending
execution_and_transfer_enabled: pending
upload_download_limits: pending  # agree with API and existing RPC limits
rpc_deadlines: pending
ledger_storage_backup_and_environment_binding: pending
readiness_evidence_and_checked_at: pending
support_contact: pending
```

Readiness requires compatible deployed revisions/proto/report formats, trusted TLS,
working authentication and network access from the web runtime, enabled execution
and transfer, available native workers and ledger storage. Do not expose a shared
operator token to the browser. Per-user tenancy is outside this trusted-team demo.
