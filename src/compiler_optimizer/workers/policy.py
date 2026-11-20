"""Versioned daemon-enforced seccomp and Docker sandbox policy.

Docker compiles this profile for its native kernel, including when the x86-64
toolchain is emulated. No unconfined or guest-filter fallback is permitted.
"""

import hashlib
import json

POLICY_VERSION = 1
COMMON_SYSCALLS = frozenset(
    """
read write readv writev pread64 pwrite64 close close_range lseek
fstat newfstatat stat lstat statx statfs fstatfs access faccessat faccessat2
open openat creat getdents getdents64 readlink readlinkat
mmap mprotect munmap mremap madvise brk msync mincore memfd_create membarrier
rt_sigaction rt_sigprocmask rt_sigreturn rt_sigsuspend sigaltstack rt_sigpending
rt_sigtimedwait rt_sigqueueinfo rt_tgsigqueueinfo restart_syscall
getpid getppid gettid getuid geteuid getgid getegid getresuid getresgid getgroups
uname sysinfo gettimeofday clock_gettime clock_getres nanosleep clock_nanosleep
times getrusage getrandom arch_prctl set_tid_address set_robust_list rseq
futex futex_waitv prlimit64 getrlimit setrlimit getpriority
sched_getaffinity sched_yield sched_getparam sched_getscheduler
sched_get_priority_max sched_get_priority_min getcpu
poll ppoll select pselect6 epoll_create epoll_create1 epoll_ctl epoll_wait epoll_pwait
epoll_pwait2 eventfd eventfd2 pipe pipe2 dup dup2 dup3 fcntl ioctl flock fsync fdatasync
sync_file_range ftruncate truncate rename renameat renameat2 unlink unlinkat
mkdir mkdirat rmdir chmod fchmod fchmodat fchmodat2 umask chdir fchdir getcwd
utime utimes futimesat utimensat symlink symlinkat link linkat
execve exit exit_group wait4 waitid
""".split()
)
# The trusted helper needs to fork, drop identity, and terminate its child. The
# program loses all effective capabilities; PID/RLIMIT budgets prevent its forks.
HELPER_SYSCALLS = frozenset(
    """
fork vfork kill tkill tgkill prctl setsid setpgid
setuid setgid setreuid setregid setresuid setresgid setgroups
""".split()
)
PROFILE = {
    "defaultAction": "SCMP_ACT_ERRNO",
    "defaultErrnoRet": 1,
    "archMap": [
        {
            "architecture": "SCMP_ARCH_X86_64",
            "subArchitectures": ["SCMP_ARCH_X86", "SCMP_ARCH_X32"],
        },
        {"architecture": "SCMP_ARCH_AARCH64", "subArchitectures": ["SCMP_ARCH_ARM"]},
    ],
    "syscalls": [
        {"names": sorted(COMMON_SYSCALLS | HELPER_SYSCALLS), "action": "SCMP_ACT_ALLOW"},
        {"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38},
        # Sanitizer startup may query/disable ASLR for its own process only.
        {
            "names": ["personality"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 0xFFFFFFFF, "op": "SCMP_CMP_EQ"}],
        },
        {
            "names": ["personality"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 0xFFFFFFFFFFFFFFFF, "op": "SCMP_CMP_EQ"}],
        },
        {
            "names": ["personality"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 0x40000, "op": "SCMP_CMP_EQ"}],
        },
        {
            "names": ["personality"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 0, "op": "SCMP_CMP_EQ"}],
        },
        {
            "names": ["clone"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 0x7E020000, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}],
        },
        # AF_UNIX is for runtime/emulator-local IPC. No IP sockets can be created.
        {
            "names": ["socket", "socketpair"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 1, "op": "SCMP_CMP_EQ"}],
        },
        {
            "names": [
                "connect",
                "sendmsg",
                "recvmsg",
                "sendto",
                "recvfrom",
                "shutdown",
                "getsockname",
                "getpeername",
                "getsockopt",
                "setsockopt",
            ],
            "action": "SCMP_ACT_ALLOW",
        },
    ],
}
POLICY_JSON = json.dumps(PROFILE, sort_keys=True, separators=(",", ":"))
POLICY_DIGEST = hashlib.sha256(POLICY_JSON.encode()).hexdigest()


def validate_host_config(
    config,
    *,
    memory: int,
    compile_mode: bool,
    mounts: dict,
    cpu_id=None,
    target="linux-x86_64",
    measurement_version=1,
) -> None:
    """Fail closed if Docker's effective create settings weaken the required policy."""
    required = {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Privileged": False,
        "Memory": memory * 1024**2,
        "MemorySwap": memory * 1024**2,
        "NanoCpus": 1_000_000_000,
        "PidsLimit": 64
        if compile_mode
        else 4
        if target == "linux-riscv64"
        else 3
        if measurement_version == 2
        else 2,
        "IpcMode": "none",
        "CgroupnsMode": "private",
    }
    for name, value in required.items():
        if config.get(name) != value:
            raise ValueError(f"Docker sandbox policy mismatch: {name}")
    if config.get("CpusetCpus", "") != ("" if cpu_id is None else str(cpu_id)):
        raise ValueError("Docker sandbox CPU affinity mismatch")
    if {value.removeprefix("CAP_") for value in config.get("CapDrop") or ()} != {"ALL"}:
        raise ValueError("Docker sandbox must drop all capabilities")
    if {value.removeprefix("CAP_") for value in config.get("CapAdd") or ()} != {
        "SETUID",
        "SETGID",
        "KILL",
    }:
        raise ValueError("unexpected helper capabilities")
    security = config.get("SecurityOpt") or []
    profiles = [
        value.removeprefix("seccomp=") for value in security if value.startswith("seccomp=")
    ]
    if len(security) != 2 or "no-new-privileges" not in security or len(profiles) != 1:
        raise ValueError("unexpected Docker security options")
    try:
        if json.loads(profiles[0]) != PROFILE:
            raise ValueError("Docker seccomp profile mismatch")
    except (ValueError, TypeError) as error:
        raise ValueError("Docker seccomp profile mismatch") from error
    if config.get("Tmpfs") != mounts:
        raise ValueError("unexpected writable sandbox mounts")
    if config.get("LogConfig", {}).get("Type") != "none":
        raise ValueError("sandbox daemon log spooling must be disabled")
    for name in ("Binds", "Mounts", "VolumesFrom", "Devices", "DeviceRequests", "PortBindings"):
        if config.get(name):
            raise ValueError(f"forbidden sandbox setting: {name}")
    if config.get("PidMode") or config.get("UTSMode") or config.get("UsernsMode") == "host":
        raise ValueError("host namespaces are forbidden")
    if config.get("OomKillDisable") or config.get("PublishAllPorts"):
        raise ValueError("unsafe sandbox resource/network setting")
