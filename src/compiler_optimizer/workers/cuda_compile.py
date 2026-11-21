"""Offline CUDA-to-PTX compilation using a CPU-only, isolated NVIDIA toolchain container."""

import argparse
import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.storage.artifacts import ArtifactStore
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError


class CUDACompileRunner(DockerRunner):
    def __init__(
        self, image="cmpe273-cuda-compile:13.0", *, watchdog_directory="var/cuda-watchdogs"
    ):
        super().__init__(image, watchdog_directory=watchdog_directory)
        info = json.loads(self._command(["image", "inspect", self.image]).stdout)[0]
        if (
            info.get("Config", {}).get("Labels", {}).get("compiler-optimizer.cuda-compile")
            != "13.0-ptx-v1"
        ):
            raise InfrastructureError("image does not provide the controlled offline CUDA compiler")
        with self.work_window():
            version = self._command(
                [
                    "run",
                    "--rm",
                    "--platform",
                    self.platform,
                    "--network",
                    "none",
                    "--read-only",
                    "--entrypoint",
                    "/usr/local/cuda-13.0/bin/nvcc",
                    self.image,
                    "--version",
                ]
            ).stdout
        matched = re.search(rb"V(13\.0\.\d+)", version)
        if matched is None:
            raise InfrastructureError("offline compiler requires NVCC 13.0")
        self.cuda_version = matched.group(1).decode()

    def compile_ptx(self, source):
        require(
            type(source) is bytes and 0 < len(source) <= 1024**2, "CUDA source must be 1 byte–1 MiB"
        )
        invocation = self._invoke(
            {
                "source": base64.b64encode(source).decode(),
                "wall_seconds": 60,
                "output_bytes": 65536,
            },
            memory=2048,
            compile_mode=True,
            trusted_command=["/usr/bin/python3", "/opt/compiler-worker/cuda_supervisor.py"],
        )
        if invocation.failure is None and (
            not invocation.binary or b".target sm_75" not in invocation.binary
        ):
            raise InfrastructureError("NVCC did not return controlled PTX output")
        return invocation

    def compiler(self, language):
        raise InfrastructureError("compile-only PTX cannot be admitted as a CPU optimization")

    def compile(self, *args, **kwargs):
        raise InfrastructureError("use compile_ptx for the offline CUDA compiler")

    def execute(self, *args, **kwargs):
        raise InfrastructureError("offline CUDA compilation cannot execute programs or GPU kernels")


def compile_artifacts(runner, source, store):
    invocation = runner.compile_ptx(source)

    def reference(r):
        return {"key": r.key, "sha256": r.sha256, "size_bytes": r.size_bytes}

    report = {
        "schema_version": 1,
        "kind": "cuda-ptx-compile-only",
        "status": "compiled" if invocation.failure is None else "rejected",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source": reference(store.put(source)),
        "image_digest": runner.image,
        "compiler": "nvcc",
        "version": runner.cuda_version,
        "target": "compute_75",
        "execution_enabled": False,
        "correctness_verified": False,
        "performance_qualified": False,
        "ptx": reference(store.put(invocation.binary)) if invocation.binary else None,
        "log": reference(store.put(json.dumps(invocation.log(), sort_keys=True).encode())),
    }
    report_ref = store.put(json.dumps(report, sort_keys=True).encode())
    return {**report, "report": reference(report_ref)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="One CUDA .cu source file; compile only")
    parser.add_argument("--state", default="var/cuda-compile")
    parser.add_argument("--image", default="cmpe273-cuda-compile:13.0")
    args = parser.parse_args()
    try:
        path = Path(args.source)
        require(
            path.suffix == ".cu" and path.is_file() and not path.is_symlink(),
            "expected a regular .cu source file",
        )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            require(
                stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "expected a regular source file"
            )
            source = stream.read(1024**2 + 1)
        runner = CUDACompileRunner(args.image, watchdog_directory=Path(args.state) / "watchdogs")
        report = compile_artifacts(runner, source, ArtifactStore(Path(args.state) / "artifacts"))
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "compiled" else 1
    except (ValueError, OSError, InfrastructureError) as error:
        print(json.dumps({"status": "unavailable", "error_class": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
