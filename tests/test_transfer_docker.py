"""Actual client CLI upload, automatic execution and evidence download without shared state."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import grpc
import pytest
from test_dispatch_docker import ROOT, free_port

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("PIPELINE_INTEGRATION_TESTS") != "1",
        reason="set PIPELINE_INTEGRATION_TESTS=1 with native compiler and services ready",
    ),
]


def test_cli_upload_executes_and_downloads_without_shared_filesystem(tmp_path):
    server_root, client_root = tmp_path / "server", tmp_path / "client"
    client_root.mkdir()
    package = client_root / "package"
    shutil.copytree(ROOT / "examples/sum-array", package)
    port, health = free_port(), free_port()
    env = dict(
        os.environ,
        STATE_DIRECTORY=str(server_root),
        GRPC_PORT=str(port),
        HEALTH_PORT=str(health),
        HEALTH_HOST="127.0.0.1",
        GRPC_HOST="127.0.0.1",
        TRANSFER_ENABLED="true",
        OPTIMIZER_API_TOKEN="integration-token-" + "a" * 32,
    )
    # Ensure the client has no coordinator storage configuration or state handles.
    client_env = dict(env)
    client_env.pop("STATE_DIRECTORY")
    python = str(Path(sys.executable).absolute())
    command = [python, "-m", "compiler_optimizer.client", "--endpoint", f"127.0.0.1:{port}"]
    log = tmp_path / "server.log"
    process = None
    try:
        with log.open("w") as output:
            process = subprocess.Popen(
                [python, str(ROOT / "tools/project.py"), "serve", "--no-start"],
                cwd=ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=15)
            submitted = subprocess.run(
                [*command, "submit", str(package), "--request-file", "request.json", "--wait"],
                cwd=client_root,
                env=client_env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert submitted.returncode == 0, submitted.stderr + log.read_text()[-6000:]
            result = json.loads(submitted.stdout)
            assert result["status"] == "completed"
            task_id = result["task_id"]
            status = subprocess.run(
                [*command, "status", task_id],
                cwd=client_root,
                env=client_env,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            assert json.loads(status.stdout)["status"] == "completed"
            downloaded = subprocess.run(
                [*command, "download", task_id, "--output", "results"],
                cwd=client_root,
                env=client_env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert downloaded.returncode == 0, downloaded.stderr
            assert (client_root / "results/source/main.c").is_file()
            assert (client_root / "results/report.json").is_file()
            assert (client_root / "results/selected-source.zip").is_file()
            assert list((client_root / "results/artifacts").iterdir())
            assert not list(client_root.rglob("*.sqlite3"))
            # The executable shell launcher exposes the same client without a server DB.
            help_result = subprocess.run(
                [str(ROOT / "project"), "client", "--help"],
                cwd=client_root,
                env=client_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert help_result.returncode == 0 and "submit" in help_result.stdout
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
