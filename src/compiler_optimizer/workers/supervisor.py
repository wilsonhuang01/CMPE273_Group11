"""Trusted per-invocation Linux supervisor, copied into the Clang image.

Only the stdlib is available in that image. No submitted program runs as root.
"""

import base64
import json
import math
import os
import platform
import resource
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from .instrumentation import BUILD_PROFILES, SANITIZER_ENV
    from .policy import POLICY_DIGEST, POLICY_VERSION
    from .targets import X86, compiler_flags, target_profile
else:
    from instrumentation import BUILD_PROFILES, SANITIZER_ENV
    from policy import POLICY_DIGEST, POLICY_VERSION
    from targets import X86, compiler_flags, target_profile

MAX_BINARY_BYTES = 16 * 1024**2
FLAGS = list(X86.flags)


def _oom_count():
    path = Path("/sys/fs/cgroup/memory.events")
    if not path.is_file():
        return 0
    values = dict(line.split() for line in path.read_text().splitlines())
    return int(values.get("oom_kill", 0))


def _limits(wall, compile_mode, emulated=False, measured=False):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cpu = max(1, math.ceil(wall) + 1)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BINARY_BYTES, MAX_BINARY_BYTES))
    # RLIMIT_NPROC is host-UID-wide, including other containers. QEMU's local
    # thread budget is enforced by pids.max=4 plus a no-child-process filter.
    process_limit = 64 if compile_mode else 4096 if emulated or measured else 1
    resource.setrlimit(resource.RLIMIT_NPROC, (process_limit, process_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256) if compile_mode else (64, 64))
    if emulated and not compile_mode:
        if __package__:
            from .emulator_policy import install
        else:
            from emulator_policy import install
        install()


def execute(
    argv, stdin, wall, output_limit, *, compile_mode, build_profile="normal", emulated=False
):
    started = time.perf_counter()
    oom_before = _oom_count()
    measured = not compile_mode and not emulated and Path("/opt/compiler-worker/measure").is_file()
    process = subprocess.Popen(
        ["/opt/compiler-worker/measure", *argv] if measured else argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/work/build" if compile_mode else "/tmp",
        start_new_session=True,
        user=0 if measured else 10001,
        group=0 if measured else 10001,
        extra_groups=[],
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "HOME": "/tmp",
            **(SANITIZER_ENV if build_profile != "normal" else {}),
        },
        preexec_fn=lambda: _limits(wall, compile_mode, emulated, measured),
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    reason = None
    consumed = 0
    input_offset = 0
    with selectors.DefaultSelector() as selector:
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if stdin:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while selector.get_map() or process.poll() is None:
            if time.perf_counter() - started >= wall and reason is None:
                reason = "timeout"
            if reason is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # The program cannot leave descendants in execution mode (pids limit).
                process.wait(timeout=2)
                break
            for key, _ in selector.select(timeout=0.02):
                if key.data == "stdin":
                    try:
                        input_offset += os.write(key.fd, stdin[input_offset : input_offset + 65536])
                    except BrokenPipeError:
                        input_offset = len(stdin)
                    if input_offset == len(stdin):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    else:
                        remaining = output_limit - consumed
                        buffers[key.data].extend(chunk[: max(0, remaining)])
                        consumed += len(chunk)
                        if consumed > output_limit:
                            reason = "output_limit"
        process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        stream.close()
    returncode = process.returncode
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
    if measured and reason is None and _oom_count() == oom_before:
        usage = json.loads(Path("/work/run/usage.json").read_text())
        if usage["error"] or process.returncode != 0:
            raise RuntimeError("native measurement launcher failed")
        returncode, peak = usage["returncode"], usage["peak_memory_bytes"]
    if reason is None and _oom_count() > oom_before:
        reason = "memory_limit"
    if reason is None and returncode:
        reason = "signal" if returncode < 0 else "nonzero_exit"
    return {
        "stdout": base64.b64encode(buffers["stdout"]).decode(),
        "stderr": base64.b64encode(buffers["stderr"]).decode(),
        "wall_seconds": time.perf_counter() - started,
        "cpu_seconds": resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
        + resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime,
        "peak_memory_bytes": peak,
        "returncode": returncode,
        "failure": reason,
        "kernel_machine": platform.machine(),
        "policy_version": POLICY_VERSION,
        "policy_digest": POLICY_DIGEST,
        "build_profile": build_profile,
        "command": argv,
    }


def main():
    request = json.load(sys.stdin)
    if request["mode"] not in ("compile", "execute"):
        raise ValueError("invalid invocation mode")
    compile_mode = request["mode"] == "compile"
    profile = request.get("build_profile", "normal")
    target = target_profile(request.get("target", X86.name))
    if target.emulator and profile == "address-undefined":
        raise ValueError("AddressSanitizer is not supported in the emulated RISC-V profile")
    if profile not in BUILD_PROFILES:
        raise ValueError("unknown trusted compiler profile")
    if compile_mode:
        for name, encoded in request["files"].items():
            if any(part in ("", ".", "..") for part in name.split("/")) or "\\" in name:
                raise ValueError("unsafe source path")
            path = Path("/work/src") / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded, validate=True))
            path.chmod(0o444)
        language = request["language"]
        if language not in ("c17", "cpp17"):
            raise ValueError("unsupported language")
        argv = [
            "/usr/bin/clang-18" if language == "c17" else "/usr/bin/clang++-18",
            *compiler_flags(target.name, language),
            *BUILD_PROFILES[profile],
            *(["-fsanitize-trap=all"] if target.emulator and profile != "normal" else []),
            "-I",
            "/work/src",
            *["/work/src/" + name for name in request["sources"]],
            "-lm",
            "-o",
            "/work/build/program",
        ]
        stdin = b""
    else:
        binary = base64.b64decode(request["binary"], validate=True)
        if len(binary) > MAX_BINARY_BYTES:
            raise ValueError("executable too large")
        path = Path("/work/run/program")
        path.write_bytes(binary)
        path.chmod(0o555)
        if target.emulator and (
            binary[:4] != b"\x7fELF"
            or len(binary) < 20
            or int.from_bytes(binary[18:20], "little") != 243
        ):
            raise ValueError("RISC-V execution requires a RISC-V ELF executable")
        argv = [*target.emulator, str(path), *request["args"]]
        stdin = base64.b64decode(request["stdin"], validate=True)
    report = execute(
        argv,
        stdin,
        request["wall_seconds"],
        request["output_bytes"],
        compile_mode=compile_mode,
        build_profile=profile,
        emulated=bool(target.emulator),
    )
    if compile_mode and report["failure"] is None:
        path = Path("/work/build/program")
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > MAX_BINARY_BYTES:
            raise ValueError("invalid compiled executable")
        report["binary"] = base64.b64encode(path.read_bytes()).decode()
    print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"infrastructure_error": type(error).__name__ + ": " + str(error)[:512]}))
        raise SystemExit(1) from None
