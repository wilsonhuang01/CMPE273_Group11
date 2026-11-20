# Baseline compilation and execution

The synchronous baseline command validates a submission directory, builds its
original C/C++ source with Clang 18, executes supplied tests in fresh containers,
and records baseline time/memory data. A failed baseline is rejected before agent
optimization. This standalone command does not dispatch proposals; the
[cohort workflow](coordinator-selection.md) integrates agents, RabbitMQ and recovery.

## Build and run

With Docker running and the Python environment installed, run from the repository
root:

```sh
docker compose --profile toolchain build compiler
python -m compiler_optimizer.workers.baseline examples/sum-array --state var/baselines
```

The command prints JSON containing task/job IDs, pass/reject status, compiler
version, immutable image digest, and baseline measurements. Exit status is 0 for
a passing baseline, 1 for a persisted rejected baseline, and 2 for invalid package
data or an unavailable toolchain detected before execution. Storage failures
remain errors; successful execution is reported only after persistence.

Use `--image <local-image>` for another locally built copy of the same toolchain.
The default profile is Linux x86-64 with Clang 18. Native ARM64 and RISC-V/QEMU
profiles are also available through `--target`; see [Architecture support](architectures.md).
The runner uses the inspected immutable
SHA-256 image ID for every invocation. Retagging cannot change a task's compiler.
The image installs Ubuntu's Clang/LLVM 18 and C/C++ development libraries. Its exact
patch release and dependencies are pinned by the resulting image ID and recorded
with each result; base tags and apt repositories can change across rebuilds.
Retain the image to replay a run.

The opt-in `toolchain` profile does not add a permanent infrastructure service.
The command runs on the trusted operator host with Docker CLI/daemon access and
executes submitted binaries only in containers. Compose services still have no
Docker socket mount and do not automatically consume or execute jobs.

`--state` is a local coordinator repository, separate from the Compose coordinator's
named volume. Use one coordinator/owner per repository; remote artifact transfer
and service-based job dispatch have not been added.

## Package ingestion and controlled build

The directory follows the [initial workload contract](initial-workload.md).
Ingestion rejects symlinks, special files, missing sources/fixtures, unsafe paths,
more than 100 files, and more than 10 MiB total file contents. Paths and arguments
are passed without shell interpolation. Bytes are snapshotted before compilation;
later directory changes do not change the recorded submission.

Two deterministic uncompressed ZIP artifacts are stored: the full package and a
source-only bundle of listed translation units and local `.h`/`.hpp` headers.
Entries have sorted paths, fixed timestamps, and regular read-only file modes.
Fixtures are omitted from the source bundle. This command reads directories;
ingestion of arbitrary uploaded archives remains separate work.

Each invocation has no network, a read-only root filesystem, one CPU of quota,
equal memory/swap limits, dropped capabilities, `no-new-privileges`, bounded tmpfs
storage, disabled core dumps, and a versioned syscall allowlist. No host paths,
service credentials, repository volume, or Docker socket are mounted.

Compilation uses fixed C17/C++17, `-O2`, controlled target-profile flags, and controlled include,
math-library, and output arguments. Users cannot supply build scripts or flags.
The executable is capped at 16 MiB and stored before tests. The writable build
tmpfs is 128 MiB, source tmpfs is 16 MiB, and temporary tmpfs is 64 MiB. Execution
receives only the executable, selected stdin, and argv; expected outputs remain
outside its container.

## Supervision and correctness gates

A trusted Python supervisor starts compilation as UID/GID 10001 with no
supplementary groups. Measurement-v2 native execution uses a trusted C launcher
that drops the same identity before starting the submitted program. The helper
runs as container root with only SETUID, SETGID,
and KILL capabilities, allowing it to drop the child identity and terminate it.
Submitted code receives no effective capabilities. Source/executable files are
root-owned; the program cannot rewrite the helper's inputs. Native measurement-v2
execution permits three PIDs: supervisor, trusted launcher and submitted program.
Legacy measurement-v1 execution permits two; RISC-V uses its separate four-PID
emulator policy. The native program's process/thread limit is one. Compilation
permits 64 PIDs including helper/compiler/linker processes.

The helper streams bounded stdout/stderr and kills the child process group on a
wall deadline or combined output overflow. An [independent host watchdog](sandbox.md)
enforces an outer deadline, monitors owner heartbeats, and verifies whole-container
removal. Docker settings and the exact syscall policy are checked before startup;
host protocol output is also capped. [Fault recovery](fault-recovery.md) adds local
worker health, scheduling, leases and retries around cohort execution; standalone
baseline execution does not retry through that controller.

Each correctness test runs once in a fresh container. Require exit zero and exact
stdout bytes, including trailing newlines. Stderr is diagnostic. After all tests
pass, selected benchmark inputs run again in fresh containers and must still
produce correct output. A changed output during measurement also rejects a baseline.

Failures preserve stage/test ID and available exit/signal evidence. Normal nonzero
compiler exits become `COMPILATION_ERROR`; crashes, deadlines, output overflow, and
observed cgroup OOM kills have distinct reasons. Daemon/helper failures become
`INFRASTRUCTURE_ERROR`. Rejected tasks become `failed`; passing baseline tasks
remain `running` for later agents. This command does not choose an optimization
or mark the complete optimization task finished.

## Measurements and persisted evidence

Each provisional baseline measurement has one sample and no warmups, speedup claim, or
statistical-significance decision. Elapsed time surrounds child creation through
exit, including executable loading and excluding Docker startup. Peak memory is
Linux child `ru_maxrss`, converted from KiB to bytes, excluding the trusted parent's
RSS. The cgroup memory limit includes the whole sandbox. The dedicated benchmark
[stage](benchmarking.md) adds repeated paired measurements and selection gates.

The default target is Linux x86-64; `--target linux-aarch64` selects native ARM64
and `--target linux-riscv64` selects correctness-only QEMU execution. Docker Desktop
on ARM may emulate the x86-64 profile; engine
architecture, allocated CPUs/memory, kernel version, and CPU quota are recorded.
Emulated measurements support functional validation and should not be compared
with native benchmark results.

The repository retains the submission, source hash/version, baseline job, full
verification result, binary artifact, and JSON invocation log. The log contains
base64 stdout/stderr, stage/test IDs, exit status, failure reason, time, and memory.
Successful benchmark data is also stored as a protobuf artifact. Inspect a run:

```python
import json
from compiler_optimizer.storage import Repository

repo = Repository("var/baselines")
task = repo.list_tasks()[-1]
result = repo.list_results(task["task_id"])[-1]
print(result)
print(json.loads(repo.artifacts.get(result.log)))
```

These results establish behavior for supplied inputs, rather than general semantic
equivalence or absence of undefined behavior.

## Validation

```sh
python -m pytest tests/test_baseline.py
BASELINE_DOCKER_TESTS=1 python -m pytest tests/test_baseline_docker.py
```

The opt-in Docker suite runs real C17/C++17 programs and covers compile failure,
wrong output, nonzero exit, segmentation signal, infinite loop, excess output, and
memory exhaustion. All submitted code stays inside Docker. CI builds the same
toolchain and runs these tests. Ordinary checks skip them unless
`BASELINE_DOCKER_TESTS=1`; `COMPILER_IMAGE` selects another local toolchain image.
