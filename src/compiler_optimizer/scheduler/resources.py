"""Docker-engine capacity and cross-process admission for local sandbox clients."""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.shared.lease_context import EXECUTION_LEASE
from compiler_optimizer.shared.manifest import require

HELD = ContextVar("engine_admission", default=None)
HEADROOM_CPUS = 1
HEADROOM_MIB = 512
AGENT_MIB = 256


@dataclass(frozen=True)
class Capacity:
    cpus: int
    memory_mib: int

    @classmethod
    def parse(cls, hardware):
        value = json.loads(hardware) if isinstance(hardware, str) else hardware
        engine = value.get("docker_engine", value)
        cpus, memory = engine.get("cpus"), engine.get("memory_bytes")
        require(type(cpus) is int and cpus > HEADROOM_CPUS, "insufficient Docker CPU headroom")
        require(
            type(memory) is int and memory // 1024**2 > HEADROOM_MIB,
            "insufficient Docker memory headroom",
        )
        return cls(cpus - HEADROOM_CPUS, memory // 1024**2 - HEADROOM_MIB)

    def workers(self, memory_mib, maximum=8):
        require(type(maximum) is int and 1 <= maximum <= 8, "worker limit must be 1–8")
        count = min(maximum, self.cpus, self.memory_mib // memory_mib)
        require(count > 0, "Docker capacity cannot fit one reserved worker plus headroom")
        return count


def reservation_mib(manifest):
    return max(manifest.limits.compile_memory_mib, manifest.limits.run_memory_mib, 128) + AGENT_MIB


def runner_capacity(runner):
    try:
        value = json.loads(runner.hardware)
    except (ValueError, TypeError, AttributeError):
        return None  # Trusted SDK adapters without engine telemetry remain sequential.
    if not isinstance(value, dict) or "docker_engine" not in value:
        return None
    return Capacity.parse(value)


@contextmanager
def engine_window(engine_id, capacity, *, memory_mib=128, exclusive=False, timeout=60):
    """Hold a VM-wide read/write gate and explicit CPU/memory reservations.

    Separate state directories and worker processes share these locks. A writer
    announces intent before waiting so new readers cannot starve measurement.
    Dead process reservations are pruned via OS locks; measurement callers must
    also confirm that abandoned sandboxes have been removed.
    """
    require(
        type(memory_mib) is int and 0 < memory_mib <= capacity.memory_mib,
        "work exceeds Docker memory reservation",
    )
    held = HELD.get() or {}
    if engine_id in held:
        owning_measurement, reserved_mib = held[engine_id]
        require(not exclusive or owning_measurement, "cannot upgrade shared work to measurement")
        require(
            owning_measurement or memory_mib <= reserved_mib,
            "nested work exceeds memory reservation",
        )
        yield
        return
    root = Path(tempfile.gettempdir()) / f"compiler-optimizer-admission-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    require(not root.is_symlink() and root.stat().st_uid == os.getuid(), "unsafe admission root")
    key = hashlib.sha256(engine_id.encode()).hexdigest()
    directory = root / key
    directory.mkdir(mode=0o700, exist_ok=True)
    require(not directory.is_symlink(), "unsafe engine admission directory")

    def open_lock(name):
        return os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

    gate, turn, mutex = (open_lock(name) for name in ("gate", "turn", "allocation"))
    owner_path = uuid4().hex
    owner = open_lock(owner_path)
    fcntl.flock(owner, fcntl.LOCK_EX)
    deadline = time.monotonic() + timeout
    admitted = False

    def acquire(fd, mode):
        while True:
            lease = EXECUTION_LEASE.get()
            if lease is not None:
                lease["assert_live"]()
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Docker engine admission deadline") from None
                time.sleep(0.05)

    try:
        acquire(turn, fcntl.LOCK_EX)
        acquire(gate, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        fcntl.flock(turn, fcntl.LOCK_UN)
        while not admitted:
            acquire(mutex, fcntl.LOCK_EX)
            try:
                path = directory / "reservations.json"
                rows = json.loads(path.read_text()) if path.exists() else []
                live = []
                for row in rows:
                    require(
                        set(row) == {"token", "memory_mib"}
                        and type(row["memory_mib"]) is int
                        and row["memory_mib"] > 0
                        and len(row["token"]) == 32
                        and all(c in "0123456789abcdef" for c in row["token"]),
                        "invalid resource reservation",
                    )
                    fd = open_lock(row["token"])
                    try:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            live.append(row)
                        else:
                            (directory / row["token"]).unlink(missing_ok=True)
                    finally:
                        os.close(fd)
                admitted = (
                    len(live) < capacity.cpus
                    and sum(r["memory_mib"] for r in live) + memory_mib <= capacity.memory_mib
                )
                if admitted:
                    live.append({"token": owner_path, "memory_mib": memory_mib})
                temporary = directory / (owner_path + ".json")
                temporary.write_text(json.dumps(live, sort_keys=True))
                os.replace(temporary, path)
            finally:
                fcntl.flock(mutex, fcntl.LOCK_UN)
            if not admitted:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Docker resource reservation deadline")
                time.sleep(0.05)
        token = HELD.set({**held, engine_id: (exclusive, memory_mib)})
        try:
            yield
        finally:
            HELD.reset(token)
    finally:
        for fd in (owner, mutex, turn, gate):
            os.close(fd)
        (directory / owner_path).unlink(missing_ok=True)
