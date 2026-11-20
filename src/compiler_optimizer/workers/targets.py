"""Controlled target profiles shared by the host and the trusted container supervisor."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TargetProfile:
    name: str
    flags: tuple[str, ...]
    emulator: tuple[str, ...] = ()
    performance_qualification: bool = True


X86 = TargetProfile("linux-x86_64", ("-O2", "-march=x86-64", "-mtune=generic", "-Wall", "-Wextra"))
RISCV = TargetProfile(
    "linux-riscv64",
    (
        "-O2",
        "--target=riscv64-linux-gnu",
        "--gcc-toolchain=/usr",
        "-march=rv64gc",
        "-mabi=lp64d",
        "-Wall",
        "-Wextra",
    ),
    ("/usr/bin/qemu-riscv64", "-cpu", "rv64", "-L", "/usr/riscv64-linux-gnu"),
    False,
)
ARM = TargetProfile(
    "linux-aarch64", ("-O2", "-march=armv8-a", "-mtune=generic", "-Wall", "-Wextra")
)
TARGETS = {p.name: p for p in (X86, ARM, RISCV)}
DEFAULT_IMAGES = {
    "linux-x86_64": "cmpe273-clang:18",
    "linux-aarch64": "cmpe273-arm:18",
    "linux-riscv64": "cmpe273-riscv:18",
}


def target_profile(name):
    if name not in TARGETS:
        raise ValueError("unsupported execution target")
    return TARGETS[name]


def compiler_flags(target, language):
    if language not in ("c17", "cpp17"):
        raise ValueError("unsupported compiler language")
    return ["-std=c17" if language == "c17" else "-std=c++17", *target_profile(target).flags]
