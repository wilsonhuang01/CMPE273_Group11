# Architecture support and hardware limits

Implemented paths are native Linux x86-64 and ARM64 C17/C++17 execution,
**RV64GC/LP64D correctness validation using QEMU user-mode emulation inside Docker**,
and offline CUDA-to-PTX compilation. Native CPU profiles can qualify benchmarks;
emulated execution cannot. The [recorded evaluation](evaluation.md) used native
ARM64 on the Apple Silicon development machine.

NVIDIA GPU execution, CUDA-event timing, GPU diagnostics and device/host recovery
operations are not implemented or validated. The GPU recovery decision policy is
implemented and tested, but requires a real GPU backend and trusted recovery
operations before it can control hardware.

## Build and run on this machine

Use `./project up` to build the native CPU profile and start the complete local
stack. `./project demo` and `./project evaluate` use that profile automatically.
The optional RISC-V path is built separately:

```sh
docker compose --profile riscv build compiler-riscv
python -m compiler_optimizer.workers.capabilities --probe

python -m compiler_optimizer.coordinator.round \
  --package examples/sum-array --agents performance --target linux-riscv64 \
  --state var/riscv
```

The capability command inspects installed images and health probes, rather than
assuming a tag or host CPU makes execution available. On the Apple Silicon
development machine, the x86-64 toolchain runs in a Linux/amd64 container; the
RISC-V toolchain uses a native Linux/arm64 container, then explicitly invokes
`qemu-riscv64`. Both report performance qualification disabled on this machine.
CUDA execution always reports unavailable because no GPU execution backend is supplied.
`cuda_compile_only` reports the separately probed offline NVCC compiler.
No host QEMU installation, privileged binfmt registration, GPU access or host
reboot is needed. On an amd64 Docker engine the same RISC-V image builds natively
for amd64; the profile remains emulated and correctness-only.

Resume with the same target and state:

```sh
python -m compiler_optimizer.coordinator.round \
  --task-id TASK_ID --target linux-riscv64 --state var/riscv
```

`--image` overrides the default `cmpe273-riscv:18` image. Images are resolved to
immutable IDs. An image declaring another target is rejected. Pending workflows
must use the exact original image digest after an image rebuild. To inspect a
baseline/candidate independently:

```sh
python -m compiler_optimizer.workers.baseline examples/sum-array \
  --target linux-riscv64 --state var/riscv-manual
python -m compiler_optimizer.workers.verify TASK_ID \
  --candidate examples/sum-array-optimized --target linux-riscv64 --state var/riscv-manual
```

The full cohort command uses the existing RabbitMQ delivery, Redis blacklist,
worker heartbeats, leases, bounded retries, automated verification and both
Correctness/Critic reviews. Target-compatible replacement workers retain the
original image, target and controlled flags. Incompatible workers are skipped
without quarantining them as unhealthy. The example Architecture Agent still
has its original x86-specific rule; use a suitable custom patch backend for other
architecture optimizations. The Performance example is portable C.

## Controlled execution profiles

`workers.targets` is the shared host/supervisor registry. The operator selects the
target; source manifests cannot inject compiler flags, commands or emulators.
Existing messages remain compatible, with `CompilerSettings.target` identifying
the profile. Worker heartbeats may advertise the supported CPU targets. No CUDA
language/target is admitted through the CPU contracts.

The RISC-V build uses Clang 18, the installed GNU RISC-V sysroot/C++ libraries and
these normal flags (with `-std=c++17` for C++):

```text
-std=c17 -O2 --target=riscv64-linux-gnu --gcc-toolchain=/usr -march=rv64gc -mabi=lp64d -Wall -Wextra
```

Execution uses this fixed command prefix:

```text
/usr/bin/qemu-riscv64 -cpu rv64 -L /usr/riscv64-linux-gnu
```

Only RISC-V ELF binaries are admitted to that execution path. Compiler version,
flags, target and image digest are persisted. Hardware evidence and recovery
context record container platform, emulator command and QEMU version; the image
digest also binds the exact emulator, sysroot, supervisor and child filter.
Blacklists consequently cannot conflate RISC-V and x86-64 execution.

Verification retains fixed fixtures, declared comparison tolerances, generated
inputs, differential baseline/candidate checks and mandatory sanitizer checks.
For RISC-V, the `undefined` profile adds `-fsanitize-trap=all` to the existing
undefined/bounds instrumentation. A sanitizer violation traps in the emulator;
it may be reported as a signal, with the sanitizer phase retained in evidence.
No cross-target UBSan runtime is needed. Reports explicitly identify `qemu-user`
and trapping instrumentation. AddressSanitizer is rejected for this profile;
there is no silent downgrade to uninstrumented verification.

This is **functional validation, not native RISC-V benchmarking**. After successful
verification and reviews, cohort assessment completes with exclusion reason
`emulated_target_correctness_only`, no benchmark batch and no candidate selection.
The original is retained. Both benchmark execution and repository benchmark
ingestion reject this target, even if a caller supplies plausible native timings.
Baseline elapsed time/RSS describe the supervised emulator process, not native
RISC-V latency or guest-only memory. CPU/GPU or cross-architecture speedup claims
cannot be inferred from these runs.

## Emulator containment and health

Submitted code stays inside an ephemeral, read-only, network-disabled container
with the existing capability, seccomp, memory, output, CPU and detached-watchdog
controls. Native execution uses the [measurement-v2 policy](sandbox.md#native-rss-measurement-v2).
RISC-V execution
permits four cgroup tasks (supervisor plus QEMU's bounded helper threads).
`RLIMIT_NPROC` is UID-wide across containers, so it is not used as the emulator's
local thread budget. The cgroup limit is the authoritative local limit.

An additional native-kernel BPF filter is installed in the unprivileged emulator
child before exec. It rejects fork/vfork, rejects process-creating clone, forces
clone3 to ENOSYS for the controlled thread fallback, and permits clone only with
CLONE_THREAD. It rejects other syscall ABIs, including x32 on amd64. The outer
daemon seccomp filter still restricts networking, namespaces and host interaction.
The filter persists across exec and cannot be relaxed by the guest. Tests exercise
both BPF architecture branches without installing a filter in the host process,
and run actual guest fork/network denial checks inside Docker.

Worker health executes a known-good, image-baked RISC-V smoke binary through the
same supervisor, QEMU and child filter. Failure is an infrastructure fault before
proposal execution, preventing an emulator startup problem from being mistaken
for a deterministic source crash. Watchdogs and lease fencing still govern
execution and cleanup. Passing this smoke test is necessary, not proof that every
possible QEMU or hardware fault can be distinguished from an application fault.

## CUDA scope and recovery contract

CUDA compilation does not require a GPU. Build and use the separate compile-only
container on this Mac (the amd64 compiler runs under Docker's CPU emulation):

```sh
docker compose --profile toolchain build compiler
docker compose --profile cuda-compile build compiler-cuda
python -m compiler_optimizer.workers.cuda_compile examples/cuda/vector_add.cu \
  --state var/cuda-compile
```

The image adds NVCC 13.0 from NVIDIA's official package repository to the controlled
CPU compiler image, without installing a host driver. The trusted harness accepts
one `.cu` translation unit up to 1 MiB, with fixed `--ptx --std=c++17
--gpu-architecture=compute_75 -O2` and GCC 13. No caller-supplied compiler flags,
GPU requests or host mounts are allowed. Compiler processes run as UID 10001 with
the existing compile sandbox, 60-second wall limit, 2 GiB memory cap, bounded
diagnostics and a detached watchdog. NVIDIA device visibility is disabled.

Output is **PTX device code, not an executed program**. A content-addressed audit
stores the exact source, PTX and diagnostic log, pinned image digest, NVCC version
and target. Its kind is `cuda-ptx-compile-only`, with `execution_enabled`,
`correctness_verified` and `performance_qualified` all false. Syntax errors are
rejected. The runner refuses program execution and CPU optimization assignments;
these artifacts cannot enter coordinator selection as verified proposals. This
path does not link/run CUDA host applications or time GPU kernels.

`shared.gpu_recovery.assess_gpu_recovery` supplies hardware-independent admission
decisions, tested with injected observations:

- Container exit keeps the device quarantined pending health checks.
- A reset-required observation requires device operator recovery.
- An unresponsive GPU, reboot-required observation or failed diagnostics drains
  the host and keeps the device quarantined.
- Only a trusted post-recovery diagnostic plus known-good smoke-test observation
  permits readmission.

This is a **pure policy contract**, not a device registry, hardware health check,
GPU worker implementation or reset/reboot service. Calling it cannot enable CUDA
execution in this application. No host recovery command is executed on this Mac.

A future GPU milestone still needs:

1. A dedicated Linux/NVIDIA worker with compatible driver/toolkit and controlled
   GPU device assignment. Keep reset/reboot privileges in a separate host supervisor.
2. Extend the existing offline compilation profile into a trusted execution harness,
   with mandatory numeric correctness checks and Compute Sanitizer runs on actual hardware.
3. Trusted CUDA-event recording and synchronization in a controlled stream, with
   kernel time separated from transfers and total latency. Track device memory
   separately from host RSS and revise scoring accordingly.
4. Persisted device UUID/host identity, exclusive leases, device/host quarantine,
   diagnostics and recovery receipts. Fence outstanding work and drain affected
   devices/hosts before requesting a supported reset or operator reboot. A container
   kill is never sufficient evidence that a hung GPU recovered.
5. Actual health/smoke tests before readmission and infrastructure retries on a
   different healthy device. Keep unhealthy hardware quarantine separate from
   source blacklisting. Confirm deterministic source failures independently under
   matching GPU model, driver, toolchain and execution policy before blacklisting.

The current GPU policy tests establish these decision rules only; they cannot
validate driver behavior, GPU isolation, event timing or recovery from a real hang.

## Validation

```sh
python -m pytest tests/test_targets.py
RISCV_DOCKER_TESTS=1 RECOVERY_INTEGRATION_TESTS=1 \
  python -m pytest tests/test_riscv_docker.py
CUDA_COMPILE_TESTS=1 python -m pytest tests/test_cuda_compile_docker.py
```

Tests cover controlled compiler/target admission, unavailable CUDA, GPU recovery
decisions, both native BPF branches, real RV64GC C17/C++17 builds, differential and
generated inputs, trapping UBSan, wrong output, infinite loops, compilation errors,
guest process/network isolation, refusal of performance qualification, and the
RabbitMQ/Redis coordinator path with target-compatible infrastructure retry.
Tests also compile the sample CUDA kernel into actual PTX and reject invalid CUDA
without requesting a GPU. CI builds all three toolchains and runs the suites.
The toolchain design follows
[Clang cross-compilation](https://clang.llvm.org/docs/CrossCompilation.html) and
[QEMU user emulation](https://www.qemu.org/docs/master/user/main.html) documentation.
The offline CUDA profile uses the [NVIDIA installation guide](https://docs.nvidia.com/cuda/cuda-installation-guide-linux/)
and [NVCC compilation phases](https://docs.nvidia.com/cuda/cuda-compiler-driver-nvcc/index.html).
The future GPU recovery design follows [NVIDIA recovery actions](https://docs.nvidia.com/deploy/nvidia-smi/index.html).

## Native ARM64 execution and evaluation

`docker compose --profile native-arm build compiler-arm` builds `cmpe273-arm:18`
with Clang 18 and the controlled `linux-aarch64` profile (`-march=armv8-a`,
`-mtune=generic`). `./project up` selects this profile automatically on an ARM64
Docker engine. This native path can qualify CPU benchmarks; the x86-64 and RISC-V
paths described above remain emulated on Apple Silicon. Benchmark records bind
execution target and native status to the verified compiler profile. See
[evaluation](evaluation.md) for native RSS measurement v2 and equal-budget trials.
