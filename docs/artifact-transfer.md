# Package upload and artifact download

The host coordinator provides streamed package upload, task status, a task-scoped
artifact catalog and hash-verified downloads. Clients need Python and the package
dependencies, but no Docker, coordinator database or shared filesystem.

Start the coordinator with Docker and the infrastructure available:

```sh
./project serve
```

In a client terminal, submit a package and wait for its final task status:

```sh
./project client submit examples/sum-array --request-file var/client/request.json --wait
```

The default endpoint is `127.0.0.1:50052`. The server selects its native CPU target
and assigns the trusted compiler settings; `--target linux-aarch64`,
`--target linux-x86_64` or `--target linux-riscv64` requests an explicit installed
profile. Clients cannot supply compiler commands or flags. RISC-V remains
correctness-only. Existing starter-agent limitations and selection gates apply.

The client prints the task ID before sending and the receipt/final status as JSON.
For local CUDA compilation, submit `examples/cuda --target cuda-ptx`. Its `cuda17`
manifest has one `.cu` source and no tests or benchmark IDs. Status/download expose
PTX and compiler diagnostics with no selected source or performance decision. See
[the compile-only task contract](architectures.md#cuda-scope-and-recovery-contract).

The request file stores the package hash, target and stable metadata before the
network call. If a receipt is lost or the coordinator restarts, repeat the same
command with the same package, target and request file. It reuses the accepted
task and frozen policy. Use a new request file for a different submission. No
token, source bytes or server filesystem path is stored in the request file.

Use the printed task ID to inspect or retrieve a completed or failed task:

```sh
./project client status TASK_ID
./project client download TASK_ID --output var/client/results
```

The output directory must not already exist. The client verifies all transferred
objects in a temporary directory before publishing:

- `result.json`: task status and selected-source/report references.
- `report.json`: the saved selection report, or failure/status summary.
- `selected-source.zip` and `source/`: selected source when a decision exists.
- `artifacts.json`: artifact references, kinds and owners.
- `artifacts/SHA256`: raw package, source, test, review, benchmark and log evidence
  registered to the task, deduplicated by content hash.

All source paths are validated before writing; ZIP extraction APIs are not used.
Failures before publication remove staged data. Existing output directories and
symlink destinations are refused. Downloads default to a 512 MiB total budget;
`download --maximum-bytes N` explicitly changes that budget. Failed tasks still
provide registered diagnostics even when no selected source exists.

`python -m compiler_optimizer.client` exposes the same CLI. Global options such
as `--endpoint`, `--timeout`, `--tls`, `--ca` and `--token-env` precede the command.
`--timeout` is a per-RPC deadline, at most 120 seconds. `submit --wait-seconds N`
sets the overall polling budget; expiration leaves the accepted task running.

## Authentication and remote transport

The development default binds gRPC and health endpoints to loopback. A host
coordinator with execution, transfer or fleet RPCs enabled refuses a non-loopback gRPC bind
unless both TLS and bearer authentication are configured:

```sh
export GRPC_HOST=0.0.0.0
export RPC_TLS_CERT=/path/to/server-certificate.pem
export RPC_TLS_KEY=/path/to/server-private-key.pem
# Set OPTIMIZER_API_TOKEN through your environment/secret manager.
./project serve --no-start
```

`OPTIMIZER_API_TOKEN` must contain 32–512 printable non-space ASCII characters;
generate a random secret rather than using the development broker credentials.
The TLS certificate must identify the hostname clients use. Clients use gRPC’s default trust
roots, or the explicitly supplied CA PEM:

```sh
# Set OPTIMIZER_API_TOKEN in the client environment to the deployment's token.
./project client --endpoint optimizer.example.com:50052 --tls --ca /path/to/ca.pem \
  submit examples/sum-array --request-file var/client/remote-request.json --wait
```

The CLI reads the token from `OPTIMIZER_API_TOKEN`; `--token-env NAME` selects a
different variable. It does not accept tokens in command-line arguments or print
them in RPC errors. Authentication covers intake, status, heartbeat and transfer
RPCs. Missing/incorrect credentials are rejected before service code runs. The
CLI also refuses a remote endpoint without TLS and a token.

One shared operator token authorizes the deployment's task data. This is a trusted
team deployment, not per-user tenancy. Artifact downloads additionally require
the exact key, SHA-256 and size to be registered to the requested task; possession
of an unrelated CAS hash does not grant download access. Per-user task ownership,
tenant quotas remain separate service work. The fleet adds per-incarnation
credentials and assignment authority for worker control/result RPCs.

Compose's legacy coordinator explicitly disables execution and transfer and
publishes its intake port only on loopback. It does not become an upload server.
Use the host endpoint on 50052 for these commands. Autonomous workers and their
result commits are implemented by [action C's fleet service](autonomous-workers.md),
which uses this catalog/download API for task-scoped input transport.

## Wire and resource limits

`ArtifactTransferService.UploadPackage` accepts one `PackageUploadHeader` followed
by data frames. It enforces a 120-second request deadline, 64 KiB data chunks,
at most 512 frames, and a 12 MiB encoded package limit. Stored packages contain
at most 100 regular files and 10 MiB total contents. The format is the deterministic
ZIP_STORED snapshot produced by the client. Traversal, duplicate paths, symlinks,
compressed/encrypted archives, invalid manifests and hash/size mismatches reject
intake before artifacts or tasks are published. Temporary spooling is bounded
and cleaned up when transfer fails.

The server validates the package, resolves the trusted compiler, stores package
and source snapshots centrally, then calls the existing atomic submission and
execution intake. A repeated valid request reuses stored compiler metadata and
does not reset workflow/recovery budgets.

`ListArtifacts` lists at most 2,048 task-owned references within the 1 MiB message
limit. `DownloadArtifact` takes a task ID and exact immutable artifact reference,
validates the stored object's complete size/hash before sending, and streams
64 KiB chunks with offsets. Each object is capped at 128 MiB and each download
requires a deadline of at most 120 seconds. The client independently checks the
offsets, size and digest. No upload can directly forge verification, benchmark,
review or final-decision evidence.

## Validation

```sh
.venv/bin/python tools/generate_contracts.py --check
.venv/bin/python -m pytest -q tests/test_transfer.py tests/test_runtime.py tests/test_dispatch.py
PIPELINE_INTEGRATION_TESTS=1 .venv/bin/python -m pytest -q tests/test_transfer_docker.py
```

Service tests exercise separate client/server directories, large streamed
uploads, retries, invalid archives, reference scoping, corruption, authentication,
TLS and unsafe listener settings. The opt-in Docker test runs CLI submission,
automatic execution, status polling and source/evidence download from a client
directory without coordinator state or database configuration.
