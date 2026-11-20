# Sandbox isolation and external watchdogs

The baseline runner now enforces a versioned Docker/seccomp policy and arms a
separate host watchdog process before starting each compilation or test container.
Existing baseline commands use this automatically after rebuilding the toolchain:

```sh
docker compose --profile toolchain build compiler
python -m compiler_optimizer.workers.baseline examples/sum-array --state var/baselines
```

The runner requires a matching Linux amd64/arm64 toolchain image, Docker seccomp
support and cgroup v2. Unsupported/mismatched configurations fail before submitted code starts.
There is no unconfined execution option. Docker Desktop on ARM can emulate the
x86-64 image: Docker installs the syscall profile for its native Linux kernel,
rather than relying on unsupported guest-process filter installation.

## Enforced policy

`workers/policy.py` defines policy version 1 and a SHA-256 fingerprint of its
canonical seccomp JSON. Before execution the runner reads Docker's effective
`HostConfig` and checks:

- Private PID/UTS namespaces, private cgroup namespace, IPC disabled, network mode
  `none`, no published ports, and no host user namespace override.
- A read-only root filesystem; exactly the expected bounded tmpfs mounts; no bind
  mounts, shared application volumes, inherited volumes, devices, or Docker socket.
- One CPU of quota, the requested memory limit, equal memory/swap limit (no extra
  swap allowance), and PID limits of 64 for compilation, 3 for native measurement-v2
  execution, 2 for legacy measurement-v1 execution, or 4 for RISC-V/QEMU.
- All capabilities dropped, with only SETUID/SETGID/KILL restored for the trusted
  supervisor; mandatory `no-new-privileges` and the exact seccomp profile.
- Daemon log spooling disabled, so a faulty helper cannot fill Docker's log files.
  Output is captured through bounded pipes and persisted by the coordinator.

The supervisor runs submitted compiler/program processes as UID/GID 10001 with
no supplementary groups. Their effective capabilities are cleared on identity
change/exec. Root-owned source and executable files prevent child rewrites. Only
compilation receives the writable build filesystem; execution receives the
root-owned executable and writable `/tmp`. Neither gets fixtures other than its
selected stdin. Core dumps, CPU time, file size, descriptors, and process counts
also have kernel resource limits inside the container.

The seccomp profile defaults to `EPERM`, permitting the runtime/compiler's file,
memory, signal, clock, and process-management calls explicitly. Namespace-creating
clone flags, `unshare`, `setns`, mounts, ptrace, BPF, performance-event access,
kernel-module operations, and unlisted syscalls are denied. `clone3` returns
`ENOSYS` so libc can use the restricted legacy clone path.

The whole-container profile must permit the trusted helper to fork, drop UID/GID,
and kill its child. Native measurement-v2 execution uses supervisor, trusted C
launcher and program; legacy execution uses supervisor and program. The native
program's hard process/thread limit is one. RISC-V additionally uses its
[emulator filter](architectures.md). The
unprivileged child cannot signal the root supervisor or regain its capabilities.
AF_INET/AF_INET6 sockets are denied. AF_UNIX sockets and associated connection
operations support local runtime/emulator IPC; they do not enable IP networking.
No Docker API socket is present.

The helper reports its policy version/digest. The host checks those against its
own policy before accepting a response, independently of its Docker configuration
check. Old image versions must be rebuilt. Submissions cannot choose or modify
policy, Docker options, image IDs, mounts, or watchdog settings.

## Independent enforcement

`monitoring/watchdog.py` launches as a separate host process/session with Docker
access and no inherited input/output pipes. It remains alive if the invoking
baseline process is stopped or killed. It reads only trusted host-side lease
files; the sandbox cannot access or update those files.

Each sandbox has a random name and ownership token. Its watchdog records the
owner PID, hard monotonic deadline, and heartbeat. The owner refreshes its heartbeat
every 250 ms. Removal is triggered by:

- The invocation wall budget plus a fixed five-second startup/transport allowance.
- Owner process exit.
- More than three seconds without an owner heartbeat.
- An explicit stop request, including oversized/malformed transport handling.

The helper still enforces the precise child wall deadline and combined stdout/
stderr limit. The external watchdog handles a stopped/unresponsive helper or owner
without requiring their cooperation. It verifies the container's ownership token
before forcibly removing the whole container, including all its processes.

The host's Docker attachment reader independently caps protocol stdout/stderr at
32 MiB, large enough for the bounded executable plus diagnostic envelope. It checks
watchdog status while streaming. If the watchdog exits unexpectedly, the parent
rejects the invocation and attempts container removal itself.

Normal completion also requires watchdog confirmation that the container was
removed. A nonzero cleanup command or an unavailable daemon is not treated as
proof of removal. The watchdog retries up to five times with bounded Docker calls;
the parent waits up to 15 seconds for confirmation. Unconfirmed cleanup produces
an infrastructure failure and an audit-directory path, never a successful result.

External invocation deadlines produce timeout results. Owner/heartbeat/transport
failures produce infrastructure errors when the owner is still able to persist a
result. If the owner is killed, the external cleanup report survives for later
inspection. The [recovery controller](fault-recovery.md) handles bounded task
rescheduling and cleanup checks for cohort workflows.

## Audit records

The CLI stores watchdog records under `<state>/watchdogs/<random-id>/`. Library
callers may set `DockerRunner(..., watchdog_directory=...)`; the default is
`var/watchdogs`. Each record has private directory permissions and includes:

- `seccomp.json`: the exact profile sent to Docker.
- `lease.json`: container name/token, owner PID, and hard deadline.
- `heartbeat.json`: last owner heartbeat.
- `stop.json`: an explicit stop reason, when requested.
- `status.json`: armed/removing/removed state, reason, and cleanup attempts.

Successful helper responses and failed program invocations include policy and
watchdog confirmation in their persisted JSON invocation logs. Failed watchdog
startup or cleanup can be diagnosed from the audit directory named in the failure.
Records are retained for audit; automated retention/garbage collection is future work.

If Docker cannot be contacted, restore access and inspect the reported container
by its exact name and ownership label. Containers are labelled
`compiler-optimizer.baseline=true` and `compiler-optimizer.watchdog=<token>`.
Do not delete containers belonging to other leases.

These controls isolate processes on a trusted Docker host. They do not provide
host/disk replication, protection against an administrator controlling Docker,
or GPU hang recovery. [Scheduler retries and scoped blacklisting](fault-recovery.md)
are implemented outside the sandbox by the local recovery controller. Compose
worker services continue to have no Docker socket and do not execute submissions.

## Verification

```sh
python -m pytest tests/test_sandbox.py tests/test_baseline.py
BASELINE_DOCKER_TESTS=1 python -m pytest tests/test_baseline_docker.py tests/test_watchdog_docker.py
```

Tests reject weakened Docker settings, validate bounded host-side output handling,
exercise C/C++ correctness/resource failures, and check UID/groups/capabilities,
kernel seccomp/no-new-privileges state, denied IP sockets/forks/namespaces/signals,
and protected filesystem paths. Live watchdog tests stop a container's PID 1 and
kill the owner process, then verify independent container removal. CI runs the
same Docker suites on native Linux x86-64.

Policy format follows [Docker seccomp documentation](https://docs.docker.com/engine/security/seccomp/).

## Native RSS measurement v2

Native x86-64 and ARM64 compiler images include a trusted C `wait4` launcher.
The supervisor and launcher run as container root; only their child executes
submitted code, after dropping supplementary groups and UID/GID to 10001 and
setting RLIMIT_NPROC=1. The root-owned 0755 run directory and 0600 usage file
prevent submitted code from forging measured RSS. The container PIDs limit is
three for supervisor, launcher and program; no additional untrusted process
is permitted. Native child RSS excludes the Python pre-exec memory floor.
Legacy measurement-v1 images keep their previous two-process execution policy,
but the evaluation runner requires version 2. Compile and RISC-V policies retain
their separate process limits. External watchdog removal remains the final bound
for the entire container, including hangs.
