"""Real watchdog independence tests; no submitted code runs on the host."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from compiler_optimizer.monitoring.watchdog import ContainerWatchdog, read_json

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("BASELINE_DOCKER_TESTS") != "1", reason="requires opt-in Docker checks"
    ),
]


def wait_for_cleanup(directory):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        status = read_json(directory / "status.json")
        if status["state"] == "removed":
            return status
        assert status["state"] not in ("cleanup_failed", "ownership_mismatch"), status
        time.sleep(0.05)
    pytest.fail(f"watchdog did not remove container: {directory}")


def create_stalled_container(name, token):
    subprocess.run(
        [
            "docker",
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--label",
            "compiler-optimizer.watchdog=" + token,
            "--label",
            "compiler-optimizer.baseline=true",
            "--memory",
            "64m",
            "--pids-limit",
            "2",
            "--entrypoint",
            "/bin/sleep",
            os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
            "300",
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    subprocess.run(["docker", "start", name], capture_output=True, check=True, timeout=10)


def test_external_deadline_removes_unresponsive_container(tmp_path):
    name = "compiler-baseline-" + uuid4().hex
    guard = ContainerWatchdog(tmp_path, name, wall_seconds=30)
    create_stalled_container(name, guard.token)
    subprocess.run(
        ["docker", "kill", "--signal", "STOP", name], capture_output=True, check=True, timeout=5
    )
    guard.lease["deadline"] = time.monotonic() + 0.8
    guard.start()
    try:
        status = wait_for_cleanup(guard.directory)
        assert status["reason"] == "external_deadline"
        assert subprocess.run(["docker", "inspect", name], capture_output=True).returncode != 0
    finally:
        guard.finish()
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=5)


def test_watchdog_survives_owner_process_exit(tmp_path):
    metadata = tmp_path / "owner.json"
    code = """
import json, os, subprocess, sys
from pathlib import Path
from uuid import uuid4
from compiler_optimizer.monitoring.watchdog import ContainerWatchdog
name="compiler-baseline-"+uuid4().hex
guard=ContainerWatchdog(Path(sys.argv[1]), name, wall_seconds=60)
subprocess.run(["docker","create","--name",name,"--network","none","--read-only",
    "--label","compiler-optimizer.watchdog="+guard.token,
    "--label","compiler-optimizer.baseline=true","--memory","64m","--pids-limit","2",
    "--entrypoint","/bin/sleep",sys.argv[3],"300"],capture_output=True,check=True,timeout=10)
subprocess.run(["docker","start",name],capture_output=True,check=True,timeout=10)
guard.start()
Path(sys.argv[2]).write_text(json.dumps({"directory":str(guard.directory),"container":name}))
os._exit(0)
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path),
            str(metadata),
            os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        ],
        check=True,
        timeout=15,
    )
    details = json.loads(metadata.read_text())
    try:
        status = wait_for_cleanup(Path(details["directory"]))
        assert status["reason"] == "owner_lost"
    finally:
        subprocess.run(
            ["docker", "rm", "--force", details["container"]], capture_output=True, timeout=5
        )


def test_watchdog_removes_created_container_when_owner_dies_during_policy_inspection(tmp_path):
    """A created, not-yet-started sandbox must already have independent cleanup."""
    metadata = tmp_path / "inspecting.json"
    code = """
import json, sys, time
from pathlib import Path
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.monitoring.watchdog import atomic_json
runner = object.__new__(DockerRunner)
runner.image = sys.argv[3]
runner.target, runner.platform = "linux-x86_64", "linux/amd64"
runner.measurement_version = 2
runner.hardware = json.dumps({"docker_engine": {"cpus": 4, "memory_bytes": 4 * 1024**3}})
runner._admission_engine = "startup-cleanup-test-" + Path(sys.argv[1]).name
runner.watchdog_directory = Path(sys.argv[1])
new_watchdog = runner._new_watchdog
active = []
def make_watchdog(*args):
    guard = new_watchdog(*args)
    active.append(guard)
    return guard
runner._new_watchdog = make_watchdog
def inspect(name, *args, **kwargs):
    atomic_json(Path(sys.argv[2]), {
        "directory": str(active[0].directory), "container": name
    })
    time.sleep(60)
runner._validate_container = inspect
runner._invoke({"wall_seconds": 60, "output_bytes": 1024}, memory=64, compile_mode=False)
"""
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path),
            str(metadata),
            os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    details = None
    try:
        deadline = time.monotonic() + 15
        while not metadata.exists():
            assert owner.poll() is None, owner.communicate()[1].decode()
            assert time.monotonic() < deadline, "owner did not reach policy inspection"
            time.sleep(0.05)
        details = json.loads(metadata.read_text())
        state = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Status}}", details["container"]],
            text=True,
            timeout=5,
        ).strip()
        assert state == "created"
        owner.kill()
        owner.wait(timeout=5)
        status = wait_for_cleanup(Path(details["directory"]))
        assert status["reason"] == "owner_lost"
        assert (
            subprocess.run(
                ["docker", "inspect", details["container"]], capture_output=True, timeout=5
            ).returncode
            != 0
        )
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
        if details:
            subprocess.run(
                ["docker", "rm", "--force", details["container"]], capture_output=True, timeout=5
            )
