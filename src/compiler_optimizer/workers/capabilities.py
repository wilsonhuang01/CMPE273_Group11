"""Inspect CPU and offline CUDA compilation capabilities; GPU execution remains unavailable."""

import argparse
import json
import platform

from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError


def describe_local(*, probe=False):
    targets = []
    for target, image in (
        ("linux-x86_64", "cmpe273-clang:18"),
        ("linux-aarch64", "cmpe273-arm:18"),
        ("linux-riscv64", "cmpe273-riscv:18"),
    ):
        item = {
            "target": target,
            "image": image,
            "execution": "qemu-user" if target == "linux-riscv64" else "cpu",
            "performance_qualification": False,
            "available": None,
        }
        if probe:
            try:
                runner = DockerRunner(image, target=target)
                runner.check_health()
                item.update(
                    available=True,
                    image_digest=runner.image,
                    container_platform=runner.platform,
                    performance_qualification=runner.native_execution,
                )
            except (InfrastructureError, ValueError, OSError):
                item.update(available=False, reason="toolchain_or_sandbox_unavailable")
        targets.append(item)
    offline = {
        "available": None,
        "image": "cmpe273-cuda-compile:13.0",
        "target": "compute_75",
        "execution_enabled": False,
        "performance_qualification": False,
    }
    if probe:
        from compiler_optimizer.workers.cuda_compile import CUDACompileRunner

        try:
            compiler = CUDACompileRunner()
            smoke = compiler.compile_ptx(
                b'extern "C" __global__ void health(float *p) { p[0]=1.0f; }'
            )
            if smoke.failure:
                raise InfrastructureError("offline CUDA compiler smoke test failed")
            offline.update(
                available=True, image_digest=compiler.image, version=compiler.cuda_version
            )
        except (InfrastructureError, OSError, ValueError):
            offline.update(available=False, reason="offline_cuda_toolchain_unavailable")
    return {
        "host": {"system": platform.system(), "machine": platform.machine()},
        "targets": targets,
        "cuda_compile_only": offline,
        "cuda": {
            "available": False,
            "execution_enabled": False,
            "reason": "NVIDIA GPU execution backend is not implemented",
            "requires": [
                "dedicated Linux NVIDIA GPU host",
                "NVIDIA driver and container toolkit",
                "trusted GPU correctness/timing harness",
                "host recovery supervisor",
            ],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe", action="store_true", help="Inspect installed images and sandboxes"
    )
    args = parser.parse_args()
    print(json.dumps(describe_local(probe=args.probe), sort_keys=True))


if __name__ == "__main__":
    main()
