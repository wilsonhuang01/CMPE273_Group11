# Project documentation

Status as of **November 20, 2026**, after the local release: the complete
CPU optimization workflow is implemented and evaluated on this machine. It
connects specialized agents, RabbitMQ delivery, mandatory automated correctness
checks, paired benchmarking, weighted coordinator selection, bounded recovery,
scoped crash blacklisting and observability. The default agent backends are
handwritten rules and evidence checks; external model inference is not configured.

## Start here

With Python 3.11+ and Docker running:

```sh
./project up
./project demo
./project evaluate
```

[Evaluation and release](evaluation.md) covers startup, resource budgets, result
interpretation, failure demonstrations, testing and packaging. The
[expanded comparison](../benchmarks/results/action-p/report.html) includes 36
native ARM64 results across three C/C++ families, three trials and four
configurations, with held-out checks, costs, raw evidence and uncertainty.
Backends are deterministic rules; results apply to the tested corpus and machine.
The [earlier comparison](../benchmarks/results/step16/report.html) and its recovery
demo remain available as historical evidence.

## Implemented capabilities

- **Versioned LLVM IR proposals:** pinned LLVM tools, immutable bitcode, trusted
  pass recipes and native verification/review/benchmark selection. See
  [LLVM setup and limits](llvm-ir.md).

- **Generalized native CPU candidates:** syntax-aware buffering, streaming,
  unrolling and guarded vectorization candidates for a documented C/C++ subset.
  See [source transformations and target contracts](transformations.md).

- **Bounded debate and revisions:** optional criticism-driven revision rounds,
  durable lineage/budgets and a one-shot held-out final gate. See [debate setup](debate.md).

- **CPU execution and verification:** controlled C17/C++17 builds, isolated Docker
  execution, independent watchdogs, fixed/generated differential tests and UBSan.
  AddressSanitizer is restricted to native amd64 workers.
- **Specialized agents and selection:** Performance, Memory and Architecture
  generate candidates; Correctness and Critic review them; Benchmark organizes
  measurements. A proposal must pass tests, reviews and reliable-gain gates.
  If none qualifies, the coordinator retains the original.
- **Local fault recovery:** a host-managed pool with logical worker identities,
  heartbeats, leases, fencing, bounded retries/backoff, manual acknowledgments,
  dead-letter notices and transformation/source/context-scoped blacklisting.
- **Architecture support:** native x86-64 and ARM64 benchmark qualification,
  RISC-V/QEMU correctness-only validation and offline CUDA-to-PTX compilation.
  Native ARM64 is the measured profile on this development machine.
- **Observability and delivery:** structured logs, Prometheus, Grafana,
  repeatable comparison/demo commands, Python distributions, curated release
  archives, source fingerprints and preserved raw evidence.

The recorded release passed 388 unit/service tests and 43 Docker tests; one
AddressSanitizer test was skipped because this machine is ARM64. See
[development checks](development.md) for current validation commands.

## Execution and deployment boundaries

Optimization supports local host execution and [independent worker services](autonomous-workers.md).
In remote mode only trusted workers need Docker; coordinator-owned SQLite/CAS
state stays behind RPC and artifact transfer. The default Compose deployment
retains intake/infrastructure mode without Docker socket mounts. Physical
multi-host cleanup/recovery still needs a host supervisor. The cohort command enables local recovery; the older
single-proposal pipeline and standalone worker commands do not use that controller.

Still pending: NVIDIA GPU execution, GPU correctness/event timing and real device/
host recovery operations; external inference-provider adapters and authentication;
production worker identity/authorization and physical host recovery;
replicated storage, scheduled backups and automatic artifact retention; MLIR
transformation integration. GPU quarantine/recovery decision rules are tested
contracts, not validated hardware recovery.

## Guides

- [Development](development.md), [local infrastructure](local-infrastructure.md)
  and [observability](observability.md): setup, services, monitoring and checks.
- [CPU workload contract](initial-workload.md), [message contracts](contracts.md)
  and [storage](storage.md): supported inputs, provenance and durable state.
- [Baseline execution](baseline.md), [sandbox isolation](sandbox.md),
  [verification](verification.md) and [benchmarking](benchmarking.md): execution
  and acceptance gates.
- [Single-proposal pipeline](performance-pipeline.md),
  [specialized agents](specialized-agents.md),
  [model-backed agents](model-agents.md),
  [coordinator selection](coordinator-selection.md) and
  [fault recovery](fault-recovery.md): orchestration and failure behavior.
- [Architecture support](architectures.md) and [evaluation/release](evaluation.md):
  hardware limits, measured results, demonstration and packaging.

Older schema versions and workflow contracts remain documented where they explain
compatibility and resumption. Their version numbers record migration history.
The recorded release archives and benchmark
snapshots are immutable historical evidence; this documentation describes the
current source tree and may include corrections made after those archives.

[Automatic submission execution](automatic-execution.md) describes `./project serve`,
durable RPC dispatch, restart recovery and the host coordinator's separate state.
[Package and artifact transfer](artifact-transfer.md) covers `./project client`,
streaming upload, task-scoped downloads and TLS/bearer authentication.
[Autonomous workers](autonomous-workers.md) covers scheduler/worker commands,
capability registration, durable job dispatch, execution leases and cleanup.
[AWS infrastructure](aws-infrastructure.md) covers the Terraform environments,
`./project infra` provisioning/teardown, secrets, budgets and retention rules.
[Cloud storage](cloud-storage.md) covers action R: the S3 artifact backend, the
PostgreSQL repository, fenced dispatch claims for several coordinators, TLS to
Amazon MQ/ElastiCache, the managed-service Terraform, credential rotation and the
engine validation (`./project infra validate`).
