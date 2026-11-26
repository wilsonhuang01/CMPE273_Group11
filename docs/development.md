# Development setup

For one-command startup, the demo, evaluation and packaging, see
[Evaluation and release](evaluation.md). The [docs index](README.md) summarizes
current implementation status.
Use Python 3.11 or newer, a C/C++ compiler, and Git. CI uses Linux x86-64, Clang 18,
and Python 3.11/3.13. macOS can run these development checks with Apple Clang;
submitted programs execute in Linux containers. Native x86-64 and ARM64 profiles
support performance qualification; RISC-V/QEMU supports correctness validation.

## Install and check

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
clang-format --dry-run --Werror examples/sum-array/main.c
python tools/generate_contracts.py --check
python -m pytest -q -m 'not docker'
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE="$(command -v python)"
cmake --build build --parallel
ctest --test-dir build --output-on-failure --no-tests=error
python -m build
```

The development extra installs pytest, Ruff, CMake, clang-format 18, and the Python
build frontend, plus the pinned protobuf/gRPC generator. Runtime dependencies
include protobuf, gRPC, and JSON Schema validation for the shared contracts.
Pika and the Redis client support infrastructure probes and broker topology setup.
Dependency ranges live in `pyproject.toml`; the tested reproduction versions
are pinned in `constraints.txt`. To choose Clang explicitly, set `CC=clang CXX=clang++` before the first
CMake configure. Use a fresh build directory when changing compilers.

CTest invokes pytest against the built example and its manifest's input/output
fixtures. For direct test runs after building, use
`python -m pytest --program build/bin/sum-array`. These checks execute only the
repository-owned example on your development host. They do not validate or sandbox
uploaded submissions, and they do not measure optimization performance.

Apply formatting with `ruff format .` and
`clang-format -i examples/sum-array/main.c`. Do not format `.in` or `.out` fixtures;
their exact bytes define correctness.

## Code ownership boundaries

- `src/compiler_optimizer/coordinator/`: submission orchestration and selection.
- `src/compiler_optimizer/agents/`: Performance, Memory, Correctness, Architecture,
  Critic, and Benchmark agent implementations.
- `src/compiler_optimizer/scheduler/`: verification dispatch, queues, and retries.
- `src/compiler_optimizer/workers/`: sandbox lifecycle, compilation, and execution.
- `src/compiler_optimizer/monitoring/`: external watchdogs and worker health.
- `src/compiler_optimizer/shared/`: shared message definitions and utilities.
- `src/compiler_optimizer/storage/`: coordinator database and verified artifact store.
- `src/compiler_optimizer/runtime/`: service configuration, intake RPCs, and health lifecycle.
- `examples/`: small complete submission packages for functional checks.
- `src/compiler_optimizer/evaluation/`: frozen workloads, equal-budget experiments,
  recovery demonstrations, cost accounting and report generation.
- `benchmarks/`: recorded evaluation results and reproducibility notes.
- `tests/`: unit, service/RPC, failure-injection and opt-in Docker integration tests.
- `tools/` and `project`: setup, health checks and release packaging commands.

The shared package implements [versioned contracts](contracts.md),
manifest validation, and messaging serialization. The [storage layer](storage.md)
persists coordinator state and artifacts in a service-owned local directory.
The [Compose stack](local-infrastructure.md) starts the infrastructure and runtime
services. A [synchronous baseline command](baseline.md) validates packages and
compiles/tests/measures original programs in ephemeral Clang 18 containers.
The [sandbox policy](sandbox.md) adds syscall filtering and independent host watchdogs.
[Proposal verification](verification.md) adds fixed/generated differential testing,
sanitizer profiles, and bound evidence before candidate measurements/selection.
[Reproducible benchmarking](benchmarking.md) adds pinned paired measurements,
noise/memory/significance gates, and durable reports required for candidate selection.
The [Performance Agent milestone](performance-pipeline.md) connects a starter patch
backend, scoped RabbitMQ delivery, verification, benchmarking and coordinator results.
The [specialized agents](specialized-agents.md) add Memory/Architecture generation,
Correctness/Critic reviews and Benchmark measurement work with durable budgets.
[Fault recovery](fault-recovery.md) implements local worker heartbeats, leases,
bounded retries, broker recovery and scoped crash blacklisting.
[Autonomous workers](autonomous-workers.md) add independent scheduler/executor
processes, RPC leases, artifact transfer and fenced results. Recovery after process
loss requires a healthy executor on the same Docker engine to confirm cleanup;
unreachable physical-host recovery remains deployment work.
The [selection coordinator](coordinator-selection.md) collects frozen cohorts,
enforces review votes and weighted confidence gates, and atomically audits decisions.
CMake builds trusted repository examples; it is not the submission build interface.

## Continuous integration

`.github/workflows/ci.yml` checks Python/C formatting, lint, the CMake build,
generated contract consistency, contract validation tests, example correctness,
Python distribution builds, and wheel imports on pushes and
pull requests. The Python checks use read-only repository permissions and need no
application secrets or external infrastructure; runtime tests bind loopback ports.
A separate Compose CI job builds the images, starts RabbitMQ/Redis and the runtime
services, checks readiness and persistent artifacts, then removes its test stack.
It builds x86-64, RISC-V and offline CUDA toolchains, then runs opt-in Docker
correctness, measurement, agent, recovery and failure-injection tests. Native
measurement integration selects the Docker engine architecture; local ARM64
validation uses the `native-arm` profile. The [evaluation guide](evaluation.md)
lists the full integration-test command and the recorded local results.

## Reproduce a clean source release (action O)

`constraints.txt` pins the tested runtime, development and optional AWS/model
Python dependencies. Use it for local installs:

```sh
python -m pip install -c constraints.txt -e '.[dev]'
./project package --output /tmp/optimizer-source.tar.gz
python tools/reproduction.py --check /tmp/optimizer-source.tar.gz
```

The last command extracts into a temporary directory, creates a fresh virtualenv,
installs with constraints, builds the trusted C example, checks generated contracts,
runs all non-Docker tests, builds distributions and exercises release packaging.
It needs network access to the package index, a native C compiler and CMake build
prerequisites. It never reads the development `.env`, build directory or virtualenv
from the source checkout. CI runs this gate in a separate clean-release job.
The Docker CI job separately validates startup and fault scenarios.

Application Python builds use the same constraints. Python/Ubuntu base images
and upstream Compose dependencies use digest pins. OS package repositories are
still live: record the actual built compiler/application image IDs in run evidence
and reuse those images for matched measurements. These source pins do not promise
bit-identical OS rebuilds. Refresh constraints and image pins deliberately and run
both CI suites. See [observability](observability.md) for redacted model usage and
release provenance options. Physical multi-host failure remains AWS acceptance work.
