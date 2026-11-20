"""Extra native-kernel seccomp filter: QEMU may create threads, never child processes.

Installed only in the container child before exec. Never call install() on the host.
The outer daemon filter still enforces filesystem/network/namespace restrictions.
"""

import ctypes
import platform


def instructions(machine, resolve):
    arch = {"aarch64": 0xC00000B7, "x86_64": 0xC000003E}[machine]
    allow, denied = 0x7FFF0000, 0x00050001
    # Load seccomp_data.arch, kill ABI mismatches, then inspect syscall number.
    program = [(0x20, 0, 0, 4), (0x15, 1, 0, arch), (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0)]
    if machine == "x86_64":
        program.extend([(0x45, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)])
    for name in ("fork", "vfork", "clone3"):
        number = resolve(name)
        if number >= 0:
            program.extend(
                [(0x15, 0, 1, number), (0x06, 0, 0, 0x00050026 if name == "clone3" else denied)]
            )
    # clone is allowed only with CLONE_THREAD; Linux additionally requires CLONE_VM.
    program.extend(
        [
            (0x15, 0, 3, resolve("clone")),
            (0x20, 0, 0, 16),
            (0x45, 1, 0, 0x00010000),
            (0x06, 0, 0, denied),
            (0x06, 0, 0, allow),
        ]
    )
    return program


def install():
    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    library = ctypes.CDLL("libseccomp.so.2")
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    rows = instructions(
        platform.machine(), lambda n: library.seccomp_syscall_resolve_name(n.encode())
    )
    filters = (Filter * len(rows))(*(Filter(*r) for r in rows))
    program = Program(len(rows), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    if (
        libc.prctl(38, 1, 0, 0, 0) != 0
        or libc.prctl(22, 2, ctypes.cast(ctypes.pointer(program), ctypes.c_void_p).value, 0, 0) != 0
    ):
        raise OSError(ctypes.get_errno(), "cannot install emulator child policy")
