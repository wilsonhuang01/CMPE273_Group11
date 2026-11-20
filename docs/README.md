# Project documentation

Status as of **November 20, 2026**, after the local step 16 release: the complete
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
[recorded comparison](../benchmarks/results/step16/report.html) includes actual
native ARM64 measurements, optimization costs and recovery receipts. In one trial
of two reduction workloads, the multi-generator arm selected a 1.057x speedup and
80% lower peak RSS on 750,000 integers. Other comparisons retained the original.
All four demo scenarios passed. These results do not establish general optimizer
or multi-agent superiority.

## Implemented capabilities

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

Optimization runs through the host CLI with Docker access and host-owned SQLite/CAS
state. The Compose coordinator, scheduler and worker containers provide intake,
health and infrastructure services; they do not execute submissions or mount the
Docker socket. General job/result queues exist, but remote multi-host worker
dispatch is not implemented. The cohort command enables local recovery; the older
single-proposal pipeline and standalone worker commands do not use that controller.

Still pending: NVIDIA GPU execution, GPU correctness/event timing and real device/
host recovery operations; external inference-provider adapters and authentication;
a public source-upload endpoint; remote execution authentication and dispatch;
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
  [coordinator selection](coordinator-selection.md) and
  [fault recovery](fault-recovery.md): orchestration and failure behavior.
- [Architecture support](architectures.md) and [evaluation/release](evaluation.md):
  hardware limits, measured results, demonstration and packaging.

Older schema versions and workflow contracts remain documented where they explain
compatibility and resumption. Their version numbers are migration history, not
unfinished implementation steps. The recorded release archives and benchmark
snapshots are immutable historical evidence; this documentation describes the
current source tree and may include corrections made after those archives.
