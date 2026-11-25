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
    from .policy import FUZZ_POLICY_DIGEST, POLICY_DIGEST, POLICY_VERSION
    from .targets import X86, compiler_flags, target_profile
else:
    from instrumentation import BUILD_PROFILES, SANITIZER_ENV
    from policy import FUZZ_POLICY_DIGEST, POLICY_DIGEST, POLICY_VERSION
    from targets import X86, compiler_flags, target_profile

MAX_BINARY_BYTES = 16 * 1024**2
FLAGS = list(X86.flags)


def _oom_count():
    path = Path("/sys/fs/cgroup/memory.events")
    if not path.is_file():
        return 0
    values = dict(line.split() for line in path.read_text().splitlines())
    return int(values.get("oom_kill", 0))


def _limits(wall, compile_mode, emulated=False, measured=False, fuzz=False):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cpu = max(1, math.ceil(wall) + 1)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BINARY_BYTES, MAX_BINARY_BYTES))
    # RLIMIT_NPROC is host-UID-wide, including other containers. QEMU's local
    # thread budget is enforced by pids.max=4 plus a no-child-process filter.
    process_limit = 64 if compile_mode else 4096 if emulated or measured or fuzz else 1
    resource.setrlimit(resource.RLIMIT_NPROC, (process_limit, process_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256) if compile_mode else (64, 64))
    if emulated and not compile_mode:
        if __package__:
            from .emulator_policy import install
        else:
            from emulator_policy import install
        install()


def execute(
    argv,
    stdin,
    wall,
    output_limit,
    *,
    compile_mode,
    build_profile="normal",
    emulated=False,
    fuzz=False,
):
    started = time.perf_counter()
    oom_before = _oom_count()
    measured = (
        not compile_mode
        and not emulated
        and not fuzz
        and Path("/opt/compiler-worker/measure").is_file()
    )
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
            **({"LLVM_PROFILE_FILE": "/tmp/coverage.profraw"} if fuzz else {}),
            **(SANITIZER_ENV if build_profile != "normal" else {}),
        },
        preexec_fn=lambda: _limits(wall, compile_mode, emulated, measured, fuzz),
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
        "policy_digest": FUZZ_POLICY_DIGEST if fuzz else POLICY_DIGEST,
        "build_profile": build_profile,
        "command": argv,
    }


def llvm_main(request):
    if __package__:
        from .llvm_policy import PIPELINES, STAGES, TRIPLES
    else:
        from llvm_policy import PIPELINES, STAGES, TRIPLES
    stage, target, profile = request["stage"], request["target"], request["build_profile"]
    if stage not in STAGES or target not in TRIPLES or profile not in BUILD_PROFILES:
        raise ValueError("unsupported LLVM stage/target/profile")
    pipeline = request["pipeline_id"]
    if pipeline not in PIPELINES:
        raise ValueError("unsupported LLVM pipeline")
    language = request["language"]
    if language not in ("c17", "cpp17"):
        raise ValueError("unsupported LLVM language")
    compiler = "/usr/bin/clang-18" if language == "c17" else "/usr/bin/clang++-18"
    tool = (
        compiler
        if stage in ("emit", "lower")
        else "/usr/bin/llvm-link-18"
        if stage == "link"
        else "/usr/bin/opt-18"
    )
    identity = subprocess.run(
        [tool, "--version"], capture_output=True, timeout=5, check=True
    ).stdout.decode()
    if request["llvm_version"] not in identity.split():
        raise ValueError("LLVM tool version differs from pinned Clang")
    output = "/work/build/program"
    inputs = []
    for i, encoded in enumerate(request.get("modules", [])):
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > MAX_BINARY_BYTES or len(inputs) >= 100:
            raise ValueError("invalid LLVM module size/count")
        path = Path(f"/work/src/module-{i}.bc")
        path.write_bytes(data)
        path.chmod(0o444)
        inputs.append(str(path))
    if sum(Path(path).stat().st_size for path in inputs) > 64 * 1024**2:
        raise ValueError("LLVM inputs exceed aggregate limit")
    flags = [*compiler_flags(target, language), *BUILD_PROFILES[profile]]
    if stage == "emit":
        for name, encoded in request["files"].items():
            if (
                name.startswith("/")
                or "\\" in name
                or any(p in ("", ".", "..") for p in name.split("/"))
            ):
                raise ValueError("unsafe LLVM source path")
            path = Path("/work/src") / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded, validate=True))
            path.chmod(0o444)
        source = request["source"]
        if source not in request["files"]:
            raise ValueError("missing LLVM translation unit")
        argv = [
            tool,
            *flags,
            "-target",
            TRIPLES[target],
            "-Xclang",
            "-disable-llvm-passes",
            "-emit-llvm",
            "-c",
            "-I",
            "/work/src",
            "/work/src/" + source,
            "-o",
            output,
        ]
    elif stage == "link":
        if not inputs:
            raise ValueError("LLVM link needs modules")
        argv = [tool, *inputs, "-o", output]
    else:
        if len(inputs) != 1:
            raise ValueError("LLVM stage requires exactly one module")
        if stage == "verify":
            argv = [tool, "-passes=verify", *inputs, "-o", output]
        elif stage == "optimize":
            argv = [tool, "-verify-each", "-passes=" + PIPELINES[pipeline], *inputs, "-o", output]
        else:
            argv = [
                tool,
                *flags,
                "-target",
                TRIPLES[target],
                "-x",
                "ir",
                *inputs,
                "-lm",
                "-o",
                output,
            ]
    report = execute(
        argv,
        b"",
        request["wall_seconds"],
        request["output_bytes"],
        compile_mode=True,
        build_profile=profile,
    )
    if report["failure"] is None:
        path = Path(output)
        if (
            not stat.S_ISREG(path.lstat().st_mode)
            or not 0 < path.stat().st_size <= MAX_BINARY_BYTES
        ):
            raise ValueError("invalid LLVM output")
        data = path.read_bytes()
        if stage == "lower" and (
            data[:4] != b"\x7fELF"
            or len(data) < 20
            or int.from_bytes(data[18:20], "little") != (62 if target == "linux-x86_64" else 183)
        ):
            raise ValueError("LLVM lowering produced the wrong native executable target")
        report["binary"] = base64.b64encode(data).decode()
    print(json.dumps(report))


def mlir_main(request):
    if __package__:
        from .mlir_policy import PIPELINES
    else:
        from mlir_policy import PIPELINES
    recipe = PIPELINES[request["stage"]]
    identity = subprocess.run(
        [recipe[0], "--version"], capture_output=True, timeout=5, check=True
    ).stdout.decode()
    if request["llvm_version"] not in identity.split():
        raise ValueError("MLIR tool differs from pinned Clang version")
    data = base64.b64decode(request["module"], validate=True)
    if not 0 < len(data) <= MAX_BINARY_BYTES:
        raise ValueError("invalid MLIR input size")
    source, output = Path("/work/src/kernel.mlir"), Path("/work/build/program")
    source.write_bytes(data)
    source.chmod(0o444)
    report = execute(
        [*recipe, str(source), "-o", str(output)],
        b"",
        request["wall_seconds"],
        request["output_bytes"],
        compile_mode=True,
    )
    if report["failure"] is None:
        if not output.is_file() or not 0 < output.stat().st_size <= MAX_BINARY_BYTES:
            raise ValueError("invalid MLIR output")
        report["binary"] = base64.b64encode(output.read_bytes()).decode()
    print(json.dumps(report))


def main():
    request = json.load(sys.stdin)
    if request["mode"] == "fuzz":
        from fuzz_runtime import fuzz_main

        return fuzz_main(request, execute)
    if request["mode"] == "mlir":
        return mlir_main(request)
    if request["mode"] == "llvm":
        return llvm_main(request)
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
