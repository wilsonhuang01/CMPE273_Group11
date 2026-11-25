"""Docker CLI adapter for fresh, bounded Clang/program invocations."""

import base64
import json
import math
import re
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.monitoring.attachment import AttachmentError, attach
from compiler_optimizer.monitoring.watchdog import ContainerWatchdog
from compiler_optimizer.shared.lease_context import EXECUTION_LEASE
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.benchmark_environment import PROBE
from compiler_optimizer.workers.instrumentation import BUILD_PROFILES
from compiler_optimizer.workers.policy import (
    POLICY_DIGEST,
    POLICY_JSON,
    POLICY_VERSION,
    validate_host_config,
)
from compiler_optimizer.workers.targets import compiler_flags, target_profile

MAX_BINARY_BYTES = 16 * 1024**2


class InfrastructureError(RuntimeError):
    """Docker/toolchain or supervisor failed independently of the submitted program."""


@dataclass
class Invocation:
    stdout: bytes
    stderr: bytes
    wall_seconds: float
    peak_memory_bytes: int
    returncode: int
    failure: str | None = None
    binary: bytes | None = None
    sandbox: dict | None = None
    build_profile: str = "normal"
    command: list[str] | None = None
    cpu_seconds: float | None = None

    def log(self):
        return {
            "stdout_base64": base64.b64encode(self.stdout).decode(),
            "stderr_base64": base64.b64encode(self.stderr).decode(),
            "wall_seconds": self.wall_seconds,
            "peak_memory_bytes": self.peak_memory_bytes,
            "returncode": self.returncode,
            "failure": self.failure,
            "sandbox": self.sandbox,
            "build_profile": self.build_profile,
            "command": self.command,
            "cpu_seconds": self.cpu_seconds,
        }


class DockerRunner:
    def __init__(
        self, image="cmpe273-clang:18", *, watchdog_directory="var/watchdogs", target="linux-x86_64"
    ):
        self.target = target_profile(target).name
        self._recovery_lock = threading.Lock()
        self._recovery_guards = {}
        self._recovery_cancelled = set()
        self.watchdog_directory = Path(watchdog_directory).absolute()
        try:
            inspected = self._command(["image", "inspect", image])
            info = json.loads(inspected.stdout)[0]
            self.measurement_version = int(
                info.get("Config", {})
                .get("Labels", {})
                .get("compiler-optimizer.measurement-version", "1")
            )
            if self.measurement_version not in (1, 2):
                raise InfrastructureError("unsupported native measurement profile")
            self.supports_sanitizers = (
                info.get("Config", {})
                .get("Labels", {})
                .get("compiler-optimizer.verification-version")
                == "1"
            )
            architecture = info["Architecture"]
            if info["Os"] != "linux" or architecture not in (
                ("amd64",)
                if self.target == "linux-x86_64"
                else ("arm64",)
                if self.target == "linux-aarch64"
                else ("amd64", "arm64")
            ):
                raise InfrastructureError("toolchain container architecture is unsupported")
            self.platform = "linux/" + architecture
            declared_target = (
                info.get("Config", {})
                .get("Labels", {})
                .get("compiler-optimizer.target", "linux-x86_64")
            )
            if declared_target != self.target:
                raise InfrastructureError("compiler image does not support the requested target")
            if (
                info.get("Config", {}).get("Labels", {}).get("compiler-optimizer.policy-version")
                != "1"
            ):
                raise InfrastructureError("rebuild compiler profile for sandbox policy v1")
            self.image = info["Id"]  # Pin subsequent invocations to immutable content, not a tag.
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image):
                raise InfrastructureError("toolchain image is not content-addressed")
            engine = self._command(
                [
                    "info",
                    "--format",
                    '{"architecture":{{json .Architecture}},"cpus":{{.NCPU}},'
                    '"memory_bytes":{{.MemTotal}},"kernel":{{json .KernelVersion}},'
                    '"cgroup_version":{{json .CgroupVersion}},'
                    '"security":{{json .SecurityOptions}}}',
                ]
            )
            engine_info = json.loads(engine.stdout)
            self.native_amd64 = engine_info["architecture"] in ("x86_64", "amd64")
            self.native_execution = (
                self.native_amd64
                if self.target == "linux-x86_64"
                else engine_info["architecture"] in ("aarch64", "arm64")
                if self.target == "linux-aarch64"
                else False
            )
            if engine_info["cgroup_version"] != "2" or not any(
                option.startswith("name=seccomp") for option in engine_info["security"]
            ):
                raise InfrastructureError("sandbox requires cgroup v2 and Docker seccomp support")
            from compiler_optimizer.scheduler.resources import Capacity, engine_window

            self._admission_engine = (
                self._command(["info", "--format", "{{.ID}}"]).stdout.decode().strip()
            )
            with engine_window(self._admission_engine, Capacity.parse(engine_info)):
                version = self._command(
                    [
                        "run",
                        "--rm",
                        "--platform",
                        self.platform,
                        "--network",
                        "none",
                        "--read-only",
                        "--log-driver",
                        "none",
                        "--entrypoint",
                        "/usr/bin/clang-18",
                        self.image,
                        "--version",
                    ]
                )
                matched = re.search(rb"clang version (18\.\d+\.\d+)", version.stdout)
                if matched is None:
                    raise InfrastructureError("toolchain must provide Clang 18")
                self.version = matched.group(1).decode()
            self.hardware = json.dumps(
                {
                    "target": self.target,
                    "emulator": list(target_profile(self.target).emulator),
                    "performance_qualification": target_profile(
                        self.target
                    ).performance_qualification
                    and self.native_execution,
                    "docker_engine": engine_info,
                    "cpu_quota": 1,
                    "measurement_version": self.measurement_version,
                    "timing": "QEMU wall time and host RSS; correctness-only"
                    if self.target == "linux-riscv64"
                    else "supervised child elapsed seconds; one sample; may be emulated",
                }
            )
            self.emulator_version = None
            if self.target == "linux-riscv64":
                with self.work_window():
                    self.emulator_version = (
                        self._command(
                            [
                                "run",
                                "--rm",
                                "--platform",
                                self.platform,
                                "--network",
                                "none",
                                "--read-only",
                                "--entrypoint",
                                "/usr/bin/qemu-riscv64",
                                self.image,
                                "--version",
                            ]
                        )
                        .stdout.decode()
                        .splitlines()[0][:256]
                    )
                hardware = json.loads(self.hardware)
                hardware["emulator_version"] = self.emulator_version
                self.hardware = json.dumps(hardware)
        except (OSError, ValueError, KeyError, IndexError) as error:
            raise InfrastructureError(
                "cannot inspect toolchain; build the compiler profile first"
            ) from error

    @staticmethod
    def _command(args, **kwargs):
        allow_failure = kwargs.pop("allow_failure", False)
        try:
            result = subprocess.run(
                ["docker", *args],
                capture_output=True,
                timeout=kwargs.pop("timeout", 30),
                check=False,
                **kwargs,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InfrastructureError("Docker command unavailable or timed out") from error
        if result.returncode and not allow_failure:
            raise InfrastructureError("Docker command failed: " + " ".join(args[:2]))
        return result

    def compiler(self, language):
        return pb.CompilerSettings(
            executable="clang" if language == "c17" else "clang++",
            version=self.version,
            image_digest=self.image,
            language=language,
            target=self.target,
            flags=compiler_flags(self.target, language),
        )

    def compile(self, package, *, build_profile="normal"):
        self._check_profile(build_profile)
        limits = package.manifest.limits
        return self._invoke(
            {
                "mode": "compile",
                "target": self.target,
                "build_profile": build_profile,
                "language": package.manifest.language,
                "sources": list(package.manifest.sources),
                "files": {
                    name: base64.b64encode(data).decode() for name, data in package.sources.items()
                },
                "wall_seconds": limits.compile_wall_seconds,
                "output_bytes": limits.output_kib * 1024,
            },
            memory=limits.compile_memory_mib,
            compile_mode=True,
        )

    def mlir_stage(self, package, stage, module):
        from compiler_optimizer.workers.mlir_policy import PIPELINES

        require(stage in PIPELINES and self.native_execution, "unsupported MLIR stage/worker")
        limits = package.manifest.limits
        return self._invoke(
            {
                "mode": "mlir",
                "stage": stage,
                "llvm_version": self.version,
                "module": base64.b64encode(module).decode(),
                "wall_seconds": limits.compile_wall_seconds,
                "output_bytes": limits.output_kib * 1024,
            },
            memory=limits.compile_memory_mib,
            compile_mode=True,
        )

    def llvm_stage(
        self, package, stage, *, pipeline_id, modules=(), source=None, build_profile="normal"
    ):
        from compiler_optimizer.workers.llvm_policy import PIPELINES, STAGES, TRIPLES

        require(stage in STAGES and pipeline_id in PIPELINES, "unsupported LLVM stage/pipeline")
        require(
            self.target in TRIPLES and self.native_execution, "LLVM requires a native CPU worker"
        )
        self._check_profile(build_profile)
        limits = package.manifest.limits
        return self._invoke(
            {
                "mode": "llvm",
                "stage": stage,
                "target": self.target,
                "pipeline_id": pipeline_id,
                "llvm_version": self.version,
                "language": package.manifest.language,
                "build_profile": build_profile,
                "source": source,
                "files": {
                    name: base64.b64encode(data).decode() for name, data in package.sources.items()
                }
                if stage == "emit"
                else {},
                "modules": [base64.b64encode(data).decode() for data in modules],
                "wall_seconds": limits.compile_wall_seconds,
                "output_bytes": limits.output_kib * 1024,
            },
            memory=limits.compile_memory_mib,
            compile_mode=True,
        )

    def execute(self, binary, case, package, *, build_profile="normal", cpu_id=None):
        self._check_profile(build_profile)
        limits = package.manifest.limits
        return self._invoke(
            {
                "mode": "execute",
                "target": self.target,
                "build_profile": build_profile,
                "binary": base64.b64encode(binary).decode(),
                "stdin": base64.b64encode(package.files[case.stdin]).decode(),
                "args": list(case.args),
                "wall_seconds": limits.run_wall_seconds,
                "output_bytes": limits.output_kib * 1024,
            },
            memory=limits.run_memory_mib,
            compile_mode=False,
            cpu_id=cpu_id,
        )

    def benchmark_environment(self, cpu_id=None):
        script = f"POLICY_VERSION={POLICY_VERSION}\nPOLICY_DIGEST={POLICY_DIGEST!r}\n" + PROBE
        invocation = self._invoke(
            {"wall_seconds": 5, "output_bytes": 65536},
            memory=128,
            compile_mode=False,
            trusted_command=[
                "/usr/bin/python3",
                "-c",
                script,
                "auto" if cpu_id is None else str(cpu_id),
            ],
        )
        if invocation.failure:
            raise InfrastructureError("benchmark environment probe failed")
        data = json.loads(invocation.stdout)
        data.update(
            engine_id=self._command(["info", "--format", "{{.ID}}"]).stdout.decode().strip(),
            engine_architecture=json.loads(self.hardware)["docker_engine"]["architecture"],
            native_amd64=self.native_amd64,
        )
        if self.target == "linux-aarch64":
            data.update(execution_target=self.target, native_execution=self.native_execution)
        return data

    def check_health(self):
        with self.work_window():
            return self._check_health()

    def _check_health(self):
        info = json.loads(
            self._command(
                [
                    "info",
                    "--format",
                    '{"architecture":{{json .Architecture}},"cgroup":{{json .CgroupVersion}},'
                    '"security":{{json .SecurityOptions}}}',
                ],
                timeout=5,
            ).stdout
        )
        if (
            info["architecture"] != json.loads(self.hardware)["docker_engine"]["architecture"]
            or info["cgroup"] != "2"
            or not any(s.startswith("name=seccomp") for s in info["security"])
        ):
            raise InfrastructureError("worker engine/security context changed")
        self._command(["image", "inspect", self.image], timeout=5)
        env = self.benchmark_environment()
        self._recovery_environment = {
            k: env[k]
            for k in (
                "engine_id",
                "engine_architecture",
                "allowed_cpus",
                "cpu_id",
                "cpu_model",
                "cpu_features_sha256",
                "kernel",
                "native_amd64",
            )
        }
        if self.target == "linux-riscv64":
            if not hasattr(self, "_riscv_smoke"):
                self._riscv_smoke = self._command(
                    [
                        "run",
                        "--rm",
                        "--platform",
                        self.platform,
                        "--network",
                        "none",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--memory",
                        "64m",
                        "--pids-limit",
                        "2",
                        "--entrypoint",
                        "/bin/cat",
                        self.image,
                        "/opt/compiler-worker/riscv-smoke",
                    ]
                ).stdout
            smoke = self._invoke(
                {
                    "mode": "execute",
                    "target": self.target,
                    "build_profile": "normal",
                    "binary": base64.b64encode(self._riscv_smoke).decode(),
                    "stdin": "",
                    "args": [],
                    "wall_seconds": 3,
                    "output_bytes": 4096,
                },
                memory=128,
                compile_mode=False,
            )
            if smoke.failure or smoke.stdout != b"riscv-ok\n":
                raise InfrastructureError("RISC-V emulator health smoke test failed")

    def recovery_context(self):
        if not hasattr(self, "_recovery_environment"):
            env = self.benchmark_environment()
            self._recovery_environment = {
                k: env[k]
                for k in (
                    "engine_id",
                    "engine_architecture",
                    "allowed_cpus",
                    "cpu_id",
                    "cpu_model",
                    "cpu_features_sha256",
                    "kernel",
                    "native_amd64",
                )
            }
        return {
            "hardware": self._recovery_environment,
            "policy_digest": POLICY_DIGEST,
            "platform": self.platform,
            "target": self.target,
            "emulator": list(target_profile(self.target).emulator),
            "emulator_version": self.emulator_version,
            "cpu_quota": 1,
            "network": "none",
            "measurement_version": self.measurement_version,
        }

    def cancel_recovery_attempt(self, lease_token):
        with self._recovery_lock:
            self._recovery_cancelled.add(lease_token)
            guards = list(self._recovery_guards.get(lease_token, []))
        for guard in guards:
            if guard.process is not None:
                guard.request_stop("recovery_cancelled")
        args = [
            "ps",
            "-a",
            "--filter",
            "label=compiler-optimizer.recovery-lease=" + lease_token,
            "--format",
            "{{.ID}}",
        ]
        ids = self._command(args, timeout=5).stdout.decode().split()
        if any(re.fullmatch(r"[0-9a-f]{12,64}", item) is None for item in ids):
            raise InfrastructureError("invalid cleanup identity")
        if ids:
            self._command(["rm", "--force", *ids], timeout=5)
        remaining = self._command(args, timeout=5).stdout.strip()
        return not remaining and all(
            g.process is not None and g.status()["state"] == "removed" for g in guards
        )

    @contextmanager
    def work_window(self, *, memory_mib=128, exclusive=False):
        from compiler_optimizer.scheduler.resources import Capacity, engine_window

        if not hasattr(self, "_admission_engine"):
            self._admission_engine = (
                self._command(["info", "--format", "{{.ID}}"]).stdout.decode().strip()
            )
        capacity = Capacity.parse(self.hardware)
        with engine_window(
            self._admission_engine, capacity, memory_mib=memory_mib, exclusive=exclusive
        ):
            if exclusive:
                remaining = self._command(
                    ["ps", "--all", "--quiet", "--filter", "label=compiler-optimizer.watchdog"]
                ).stdout.strip()
                if remaining:
                    raise InfrastructureError("measurement blocked by unconfirmed sandbox cleanup")
            yield

    def measurement_window(self):
        from compiler_optimizer.scheduler.resources import Capacity

        return self.work_window(exclusive=True, memory_mib=Capacity.parse(self.hardware).memory_mib)

    def _invoke(self, payload, *, memory, compile_mode, cpu_id=None, trusted_command=None):
        with self.work_window(memory_mib=memory):
            return self._invoke_reserved(
                payload,
                memory=memory,
                compile_mode=compile_mode,
                cpu_id=cpu_id,
                trusted_command=trusted_command,
            )

    def _invoke_reserved(self, payload, *, memory, compile_mode, cpu_id=None, trusted_command=None):
        lease = EXECUTION_LEASE.get()
        lease_token = None if lease is None else lease["lease_token"]
        if lease_token is not None and lease_token in self._recovery_cancelled:
            raise InfrastructureError("execution lease was cancelled")
        if cpu_id is not None and (type(cpu_id) is not int or not 0 <= cpu_id <= 8191):
            raise InfrastructureError("invalid sandbox CPU affinity")
        name = "compiler-baseline-" + uuid4().hex
        created = False
        guard = self._new_watchdog(name, payload["wall_seconds"])
        if lease_token is not None:
            with self._recovery_lock:
                self._recovery_guards.setdefault(lease_token, set()).add(guard)
        profile = guard.directory / "seccomp.json"
        profile.write_text(POLICY_JSON)
        invocation = None
        stop_reason = "completed"
        work_mounts = (
            [
                "--tmpfs",
                "/work/src:rw,noexec,nosuid,size=16m,mode=0755",
                "--tmpfs",
                "/work/build:rw,nosuid,size=128m,uid=10001,gid=10001,mode=0755",
            ]
            if compile_mode
            else ["--tmpfs", "/work/run:rw,exec,nosuid,size=32m,mode=0755"]
        )
        try:
            self._command(
                [
                    "create",
                    "--interactive",
                    "--name",
                    name,
                    "--platform",
                    self.platform,
                    "--label",
                    "compiler-optimizer.baseline=true",
                    *(
                        []
                        if lease is None
                        else [
                            "--label",
                            "compiler-optimizer.worker=" + lease["worker_id"],
                            "--label",
                            "compiler-optimizer.recovery-lease=" + lease_token,
                        ]
                    ),
                    "--label",
                    "compiler-optimizer.watchdog=" + guard.token,
                    "--network",
                    "none",
                    "--ipc",
                    "none",
                    "--cgroupns",
                    "private",
                    "--read-only",
                    "--log-driver",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "SETUID",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "KILL",
                    "--security-opt",
                    "no-new-privileges",
                    "--security-opt",
                    "seccomp=" + str(profile),
                    "--cpus",
                    "1",
                    *([] if cpu_id is None else ["--cpuset-cpus", str(cpu_id)]),
                    "--memory",
                    f"{memory}m",
                    "--memory-swap",
                    f"{memory}m",
                    "--pids-limit",
                    "64"
                    if compile_mode
                    else "4"
                    if self.target == "linux-riscv64"
                    else "3"
                    if self.measurement_version == 2
                    else "2",
                    "--ulimit",
                    "core=0",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                    *work_mounts,
                    *([] if trusted_command is None else ["--entrypoint", trusted_command[0]]),
                    self.image,
                    *([] if trusted_command is None else trusted_command[1:]),
                ]
            )
            created = True
            if lease_token is not None and lease_token in self._recovery_cancelled:
                raise InfrastructureError("execution lease was cancelled")
            if lease is not None and lease.get("assert_live"):
                lease["assert_live"]()
            mounts = {"/tmp": "rw,noexec,nosuid,size=64m,mode=1777"}
            mounts.update(
                {
                    work_mounts[index + 1].split(":", 1)[0]: work_mounts[index + 1].split(":", 1)[1]
                    for index in range(0, len(work_mounts), 2)
                }
            )
            self._validate_container(name, memory, compile_mode, mounts, cpu_id)
            if lease is not None and lease.get("assert_live"):
                lease["assert_live"]()
            try:
                guard.start()
            except (RuntimeError, OSError) as error:
                raise InfrastructureError(
                    f"cannot arm external watchdog; inspect {guard.directory}"
                ) from error
            try:
                if lease is not None and lease.get("assert_live"):
                    lease["assert_live"]()
                completed, stopped = self._attach(
                    name, json.dumps(payload).encode(), guard, wall_seconds=payload["wall_seconds"]
                )
                if stopped:
                    stop_reason = stopped
                    if stopped == "external_deadline":
                        invocation = Invocation(
                            b"", b"external watchdog deadline", 0, 0, -9, "timeout"
                        )
                        return invocation
                    raise InfrastructureError("external watchdog: " + stopped)
            except (AttachmentError, OSError) as error:
                stop_reason = "attachment_failed"
                raise InfrastructureError(str(error)) from error
            except InfrastructureError:
                state = json.loads(
                    self._command(["inspect", "--format", "{{json .State}}", name]).stdout
                )
                if state.get("OOMKilled"):
                    invocation = Invocation(
                        b"",
                        b"supervisor killed by container memory limit",
                        0,
                        0,
                        -9,
                        "memory_limit",
                    )
                    return invocation
                raise
            if completed.returncode:
                state = json.loads(
                    self._command(["inspect", "--format", "{{json .State}}", name]).stdout
                )
                if state.get("OOMKilled"):
                    invocation = Invocation(
                        b"",
                        b"supervisor killed by container memory limit",
                        0,
                        0,
                        -9,
                        "memory_limit",
                    )
                    return invocation
            try:
                report = json.loads(completed.stdout)
                if "infrastructure_error" in report:
                    raise InfrastructureError(
                        "supervisor failed: " + report["infrastructure_error"]
                    )
                if (
                    report.get("policy_version") != POLICY_VERSION
                    or report.get("policy_digest") != POLICY_DIGEST
                ):
                    raise InfrastructureError("worker syscall policy does not match host policy")
                profile = report.get("build_profile", "normal")
                if profile != payload.get("build_profile", "normal"):
                    raise InfrastructureError("worker compiler profile mismatch")
                failure = report["failure"]
                if failure not in (
                    None,
                    "timeout",
                    "memory_limit",
                    "output_limit",
                    "signal",
                    "nonzero_exit",
                ):
                    raise ValueError("unknown failure")
                returncode = report["returncode"]
                if type(returncode) is not int or not -64 <= returncode <= 255:
                    raise ValueError("invalid return code")
                if failure is None and returncode != 0:
                    raise ValueError("unsuccessful invocation reported as successful")
                wall, peak = report["wall_seconds"], report["peak_memory_bytes"]
                if not math.isfinite(wall) or wall <= 0 or type(peak) is not int or peak < 0:
                    raise ValueError("invalid measurements")
                binary = (
                    base64.b64decode(report["binary"], validate=True)
                    if "binary" in report
                    else None
                )
                if binary is not None and len(binary) > MAX_BINARY_BYTES:
                    raise ValueError("binary too large")
                stdout = base64.b64decode(report["stdout"], validate=True)
                stderr = base64.b64decode(report["stderr"], validate=True)
                if len(stdout) + len(stderr) > payload["output_bytes"]:
                    raise ValueError("uncapped output")
                invocation = Invocation(stdout, stderr, wall, peak, returncode, failure, binary)
                invocation.build_profile = profile
                invocation.command = report.get("command")
                cpu = report.get("cpu_seconds")
                if cpu is not None and (
                    type(cpu) not in (int, float) or not math.isfinite(cpu) or cpu < 0
                ):
                    raise ValueError("invalid CPU resource usage")
                invocation.cpu_seconds = cpu
                return invocation
            except (ValueError, KeyError, TypeError) as error:
                diagnostic = guard.directory / "protocol-error.json"
                diagnostic.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "error_type": type(error).__name__,
                            "error": str(error)[:512],
                            "target": self.target,
                            "mode": payload.get("mode", "probe"),
                            "build_profile": payload.get("build_profile", "normal"),
                            "docker_returncode": completed.returncode,
                            "stdout_base64": base64.b64encode(completed.stdout).decode(),
                            "stderr_base64": base64.b64encode(completed.stderr).decode(),
                        },
                        sort_keys=True,
                    )
                )
                raise InfrastructureError(
                    f"invalid supervisor response ({type(error).__name__}; "
                    f"docker exit {completed.returncode}; "
                    f"stdout {len(completed.stdout)} bytes; stderr {len(completed.stderr)} bytes); "
                    f"inspect {diagnostic}"
                ) from error
        finally:
            if created:
                try:
                    self._command(["rm", "--force", name], timeout=3, allow_failure=True)
                finally:
                    if guard.process is not None:
                        try:
                            audit = guard.finish(stop_reason)
                            if invocation is not None:
                                invocation.sandbox = {
                                    "policy_version": POLICY_VERSION,
                                    "policy_digest": POLICY_DIGEST,
                                    "watchdog": audit,
                                    "cpu_id": cpu_id,
                                }
                        except (RuntimeError, subprocess.TimeoutExpired) as error:
                            raise InfrastructureError(
                                f"cleanup unconfirmed; inspect {guard.directory}"
                            ) from error
            if lease_token is not None:
                with self._recovery_lock:
                    self._recovery_guards.get(lease_token, set()).discard(guard)

    @staticmethod
    def _attach(*args, **kwargs):
        return attach(*args, **kwargs)

    def _check_profile(self, profile):
        if self.target == "linux-riscv64" and profile == "address-undefined":
            raise InfrastructureError(
                "RISC-V emulation supports trapping UBSan, not AddressSanitizer"
            )
        if profile not in BUILD_PROFILES:
            raise InfrastructureError("unknown compiler instrumentation profile")
        if profile != "normal" and not self.supports_sanitizers:
            raise InfrastructureError("rebuild the compiler image for sanitizer verification")

    def _new_watchdog(self, name, wall):
        return ContainerWatchdog(self.watchdog_directory, name, wall_seconds=wall)

    def _validate_container(self, name, memory, compile_mode, mounts, cpu_id=None):
        try:
            config = json.loads(
                self._command(["inspect", "--format", "{{json .HostConfig}}", name]).stdout
            )
            validate_host_config(
                config,
                memory=memory,
                compile_mode=compile_mode,
                mounts=mounts,
                cpu_id=cpu_id,
                target=self.target,
                measurement_version=self.measurement_version,
            )
        except (ValueError, TypeError, KeyError) as error:
            raise InfrastructureError(str(error)) from error
