"""Independent host process that removes an owned sandbox on deadline/owner loss."""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

HEARTBEAT_TIMEOUT = 3.0
STARTUP_GRACE = 5.0
TERMINAL = {"removed", "cleanup_failed", "ownership_mismatch"}


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def owner_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_reason(lease, heartbeat, *, alive, now):
    if not alive:
        return "owner_lost"
    if now >= lease["deadline"]:
        return "external_deadline"
    if now - heartbeat > HEARTBEAT_TIMEOUT:
        return "heartbeat_expired"
    return None


def remove_owned(lease) -> str:
    """Delete only this run's labelled container; missing daemon is never success."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", lease["container"]],
        capture_output=True,
        timeout=3,
        check=False,
    )
    if result.returncode:
        if any(
            marker in result.stderr.lower()
            for marker in (b"no such object:", b"no such container:")
        ):
            return "removed"
        raise OSError("cannot contact Docker for sandbox cleanup")
    labels = json.loads(result.stdout)
    if labels.get("compiler-optimizer.watchdog") != lease["token"]:
        return "ownership_mismatch"
    result = subprocess.run(
        ["docker", "rm", "--force", lease["container"]], capture_output=True, timeout=3, check=False
    )
    if result.returncode:
        raise OSError("Docker sandbox removal failed")
    return "removed"


def monitor(directory: Path) -> None:
    lease = read_json(directory / "lease.json")
    if not re.fullmatch(r"compiler-baseline-[0-9a-f]{32}", lease["container"]):
        raise ValueError("invalid sandbox name")
    status = directory / "status.json"
    atomic_json(status, {"state": "armed", "container": lease["container"]})
    while True:
        request = directory / "stop.json"
        reason = (
            read_json(request)["reason"]
            if request.exists()
            else stop_reason(
                lease,
                read_json(directory / "heartbeat.json")["monotonic"],
                alive=owner_alive(lease["owner_pid"]),
                now=time.monotonic(),
            )
        )
        if reason is not None:
            break
        time.sleep(0.1)
    for attempt in range(1, 6):
        try:
            state = remove_owned(lease)
            atomic_json(
                status,
                {
                    "state": state,
                    "reason": reason,
                    "attempts": attempt,
                    "container": lease["container"],
                },
            )
            return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            atomic_json(
                status,
                {
                    "state": "removing",
                    "reason": reason,
                    "attempts": attempt,
                    "container": lease["container"],
                },
            )
            time.sleep(0.2)
    atomic_json(
        status, {"state": "cleanup_failed", "reason": reason, "container": lease["container"]}
    )


class ContainerWatchdog:
    """Per-container watchdog lease; the watchdog survives a killed/stopped owner."""

    def __init__(self, root: Path, container: str, *, wall_seconds: float):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = root / uuid4().hex
        self.directory.mkdir(mode=0o700)
        self.token = uuid4().hex
        self.container = container
        self.lease = {
            "container": container,
            "token": self.token,
            "owner_pid": os.getpid(),
            "deadline": time.monotonic() + wall_seconds + STARTUP_GRACE,
        }
        self._stop = threading.Event()
        self._thread = None
        self.process = None

    def start(self):
        atomic_json(self.directory / "lease.json", self.lease)
        atomic_json(self.directory / "heartbeat.json", {"monotonic": time.monotonic()})
        self.process = subprocess.Popen(
            [sys.executable, "-m", "compiler_optimizer.monitoring.watchdog", str(self.directory)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 3
        while not (self.directory / "status.json").exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"external watchdog did not start: {self.directory}")
            time.sleep(0.02)
        return self

    def _heartbeat(self):
        while not self._stop.wait(0.25):
            atomic_json(self.directory / "heartbeat.json", {"monotonic": time.monotonic()})

    def status(self):
        path = self.directory / "status.json"
        return read_json(path) if path.exists() else {"state": "starting"}

    def request_stop(self, reason):
        atomic_json(self.directory / "stop.json", {"reason": reason})

    def finish(self, reason="completed"):
        self.request_stop(reason)
        try:
            self.process.wait(timeout=15)
            report = self.status()
            if report["state"] != "removed":
                raise RuntimeError(f"sandbox cleanup unconfirmed: {self.directory}")
            return report
        finally:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    monitor(parser.parse_args().directory)
