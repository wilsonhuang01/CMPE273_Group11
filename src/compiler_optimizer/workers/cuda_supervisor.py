"""Trusted compile-only NVCC harness. No submitted binary or GPU kernel is executed."""

import base64
import json
import sys
from pathlib import Path

if __package__:
    from .supervisor import MAX_BINARY_BYTES, execute
else:
    from supervisor import MAX_BINARY_BYTES, execute


def main():
    request = json.load(sys.stdin)
    if set(request) != {"source", "wall_seconds", "output_bytes"}:
        raise ValueError("invalid compile-only request")
    source = base64.b64decode(request["source"], validate=True)
    if not 0 < len(source) <= 1024**2:
        raise ValueError("CUDA source exceeds compile-only limit")
    path = Path("/work/src/kernel.cu")
    path.write_bytes(source)
    path.chmod(0o444)
    report = execute(
        [
            "/usr/local/cuda-13.0/bin/nvcc",
            "--ptx",
            "--std=c++17",
            "--gpu-architecture=compute_75",
            "-O2",
            "--compiler-bindir=/usr/bin/g++-13",
            str(path),
            "-o",
            "/work/build/kernel.ptx",
        ],
        b"",
        request["wall_seconds"],
        request["output_bytes"],
        compile_mode=True,
    )
    if report["failure"] is None:
        ptx = Path("/work/build/kernel.ptx").read_bytes()
        if len(ptx) > MAX_BINARY_BYTES or b".version" not in ptx or b".target sm_75" not in ptx:
            raise ValueError("invalid PTX output")
        report["binary"] = base64.b64encode(ptx).decode()
    print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"infrastructure_error": type(error).__name__}))
        raise SystemExit(1) from None
