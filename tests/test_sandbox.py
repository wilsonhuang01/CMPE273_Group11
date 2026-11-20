"""Fail-closed policy, watchdog decisions, and bounded host attachment checks."""

import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from compiler_optimizer.monitoring import attachment, watchdog
from compiler_optimizer.workers.policy import COMMON_SYSCALLS, POLICY_JSON, validate_host_config


def config():
    return {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Privileged": False,
        "Memory": 256 * 1024**2,
        "MemorySwap": 256 * 1024**2,
        "NanoCpus": 1_000_000_000,
        "PidsLimit": 2,
        "IpcMode": "none",
        "CgroupnsMode": "private",
        "CapDrop": ["ALL"],
        "CapAdd": ["SETUID", "SETGID", "KILL"],
        "SecurityOpt": ["no-new-privileges", "seccomp=" + POLICY_JSON],
        "Tmpfs": {"/tmp": "bounded"},
        "LogConfig": {"Type": "none"},
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("NetworkMode", "host"),
        ("ReadonlyRootfs", False),
        ("Privileged", True),
        ("Memory", 0),
        ("MemorySwap", -1),
        ("NanoCpus", 0),
        ("PidsLimit", -1),
        ("IpcMode", "host"),
        ("CgroupnsMode", "host"),
        ("CapDrop", []),
        ("CapAdd", ["SYS_ADMIN"]),
        ("SecurityOpt", ["seccomp=unconfined"]),
        ("Binds", ["/:/host"]),
        ("Devices", [{"PathOnHost": "/dev/sda"}]),
        ("Mounts", [{"Source": "/"}]),
        ("VolumesFrom", ["coordinator"]),
        ("DeviceRequests", [{"Count": -1}]),
        ("PortBindings", {"80/tcp": []}),
        ("PidMode", "host"),
        ("UTSMode", "host"),
        ("UsernsMode", "host"),
        ("OomKillDisable", True),
        ("PublishAllPorts", True),
        ("Tmpfs", {}),
        ("LogConfig", {"Type": "json-file"}),
    ],
)
def test_effective_docker_policy_rejects_weakened_settings(field, value):
    current = config()
    current[field] = value
    with pytest.raises(ValueError):
        validate_host_config(current, memory=256, compile_mode=False, mounts={"/tmp": "bounded"})


def test_effective_docker_policy_accepts_exact_required_settings():
    validate_host_config(config(), memory=256, compile_mode=False, mounts={"/tmp": "bounded"})
    canonical = config()
    canonical["CapAdd"] = ["CAP_SETUID", "CAP_SETGID", "CAP_KILL"]
    validate_host_config(canonical, memory=256, compile_mode=False, mounts={"/tmp": "bounded"})
    assert not COMMON_SYSCALLS & {
        "socket",
        "connect",
        "ptrace",
        "mount",
        "unshare",
        "setns",
        "bpf",
        "perf_event_open",
        "fork",
        "clone",
        "setuid",
        "capset",
    }


@pytest.mark.parametrize(
    ("alive", "heartbeat", "now", "expected"),
    [
        (True, 5, 6, None),
        (False, 5, 6, "owner_lost"),
        (True, 0, 6, "heartbeat_expired"),
        (True, 9, 10, "external_deadline"),
    ],
)
def test_watchdog_cannot_be_disabled_by_a_live_but_stalled_owner(alive, heartbeat, now, expected):
    assert watchdog.stop_reason({"deadline": 10}, heartbeat, alive=alive, now=now) == expected


def test_watchdog_does_not_delete_a_container_with_another_token(monkeypatch):
    command = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, b'{"compiler-optimizer.watchdog":"other"}', b""
        )
    )
    monkeypatch.setattr(watchdog.subprocess, "run", command)
    assert watchdog.remove_owned({"container": "test", "token": "ours"}) == "ownership_mismatch"
    assert command.call_count == 1


def test_watchdog_does_not_confuse_unavailable_daemon_with_missing_container(monkeypatch):
    command = Mock(return_value=subprocess.CompletedProcess([], 1, b"", b"connection refused"))
    monkeypatch.setattr(watchdog.subprocess, "run", command)
    with pytest.raises(OSError):
        watchdog.remove_owned({"container": "test", "token": "ours"})


@pytest.mark.parametrize(
    "message", [b"Error: No such object: test", b"error: no such object: test"]
)
def test_watchdog_confirms_missing_container(monkeypatch, message):
    monkeypatch.setattr(
        watchdog.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1, b"", message)),
    )
    assert watchdog.remove_owned({"container": "test", "token": "ours"}) == "removed"


def test_atomic_watchdog_records_round_trip(tmp_path):
    path = tmp_path / "status.json"
    watchdog.atomic_json(path, {"state": "armed"})
    watchdog.atomic_json(path, {"state": "removed"})
    assert json.loads(path.read_text()) == {"state": "removed"}
    assert not path.with_suffix(".tmp").exists()


def test_host_attachment_caps_even_a_broken_supervisors_protocol_output(monkeypatch):
    original = subprocess.Popen
    monkeypatch.setattr(
        attachment.subprocess,
        "Popen",
        lambda *args, **kwargs: original(
            [sys.executable, "-c", "import os; os.write(1,b'x'*8192)"], **kwargs
        ),
    )
    monkeypatch.setattr(attachment, "MAX_PROTOCOL_BYTES", 1024)
    guard = Mock()
    guard.status.return_value = {"state": "armed"}
    guard.process.poll.return_value = None
    completed, reason = attachment.attach("unused", b"", guard, wall_seconds=10)
    assert reason == "protocol_output_limit"
    assert len(completed.stdout) <= 1024
    guard.request_stop.assert_called_once_with("protocol_output_limit")


def test_watchdog_crash_interrupts_host_attachment(monkeypatch):
    original = subprocess.Popen
    monkeypatch.setattr(
        attachment.subprocess,
        "Popen",
        lambda *args, **kwargs: original(
            [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs
        ),
    )
    guard = Mock()
    guard.status.return_value = {"state": "armed"}
    guard.process.poll.return_value = 1
    with pytest.raises(attachment.AttachmentError, match="watchdog exited"):
        attachment.attach("unused", b"", guard, wall_seconds=10)
