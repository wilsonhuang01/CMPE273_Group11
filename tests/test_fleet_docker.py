"""Real coordinator/scheduler/two-worker processes with worker-loss reassignment."""

import os
import subprocess
import sys
import time
from pathlib import Path

import grpc
import pytest
from test_dispatch_docker import ROOT, free_port

from compiler_optimizer.client import OptimizerClient, prepare_upload
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.package import read_package

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("PIPELINE_INTEGRATION_TESTS") != "1",
        reason="set PIPELINE_INTEGRATION_TESTS=1 with toolchains, RabbitMQ and Redis ready",
    ),
]


def until(action, processes, logs, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in processes:
            assert process.poll() is None, "\n".join(p.read_text()[-5000:] for p in logs)
        result = action()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail(
        "fleet did not reach checkpoint:\n" + "\n".join(p.read_text()[-5000:] for p in logs)
    )


def test_independent_workers_reassign_after_process_loss_without_coordinator_docker(tmp_path):
    token = "fleet-integration-" + "b" * 32
    state = tmp_path / "coordinator"
    repo = Repository(state)
    port = free_port()
    endpoint = f"127.0.0.1:{port}"
    env = dict(
        os.environ,
        EXECUTION_BACKEND="remote",
        COORDINATOR_ENDPOINT=endpoint,
        OPTIMIZER_API_TOKEN=token,
        GRPC_HOST="127.0.0.1",
        GRPC_PORT=str(port),
        HEALTH_HOST="127.0.0.1",
        TRANSFER_ENABLED="true",
        FLEET_ENABLED="true",
        SERVICE_ENABLED="true",
    )
    handles = []
    processes = []
    logs = []
    workers = []
    python = str(Path(sys.executable).absolute())

    def start(role, name, extra=None):
        log = tmp_path / (name + ".log")
        output = log.open("w")
        handles.append(output)
        logs.append(log)
        values = dict(env, HEALTH_PORT=str(free_port()))
        values.update(extra or {})
        process = subprocess.Popen(
            [python, str(ROOT / "tools/project.py"), role, "--no-start"],
            cwd=ROOT,
            env=values,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        processes.append(process)
        return process

    try:
        coordinator = start(
            "serve",
            "coordinator",
            {"STATE_DIRECTORY": str(state), "PATH": "/nonexistent-no-docker"},
        )
        with grpc.insecure_channel(endpoint) as channel:
            grpc.channel_ready_future(channel).result(timeout=15)
            scheduler = start("scheduler", "scheduler", {"PATH": "/nonexistent-no-docker"})
            for name in ("worker-a", "worker-b"):
                root = tmp_path / name
                process = start(
                    "worker", name, {"WORKER_DIRECTORY": str(root), "TRANSFER_ENABLED": "false"}
                )
                workers.append((process, root))
            until(lambda: len(repo.fleet_profiles()) == 2, processes, logs, timeout=60)
            client = OptimizerClient(channel, token=token)
            package = read_package(ROOT / "examples/sum-array")
            header = prepare_upload(package, tmp_path / "client/request.json")
            client.submit(header, package.package_bundle)
            task = header.meta.task_id

            def running_container():
                with repo._transaction(write=False) as db:
                    rows = db.execute(
                        "SELECT * FROM fleet_jobs WHERE task_id=? AND state='running'", (task,)
                    ).fetchall()
                for row in rows:
                    existing = subprocess.run(
                        [
                            "docker",
                            "ps",
                            "-a",
                            "--filter",
                            "label=compiler-optimizer.recovery-lease=" + row["token"],
                            "--format",
                            "{{.ID}}",
                        ],
                        capture_output=True,
                        check=True,
                        timeout=5,
                    ).stdout.strip()
                    if existing:
                        return dict(row)
                return None

            interrupted = until(running_container, processes, logs, timeout=60)
            victim = next(
                process
                for process, root in workers
                if (root / "worker-id").read_text().strip() == interrupted["worker_id"]
            )
            victim.kill()
            victim.wait(timeout=10)
            processes.remove(victim)
            until(
                lambda: repo.fleet_job(interrupted["job_id"])["state"] == "failed",
                processes,
                logs,
                timeout=40,
            )
            remaining = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    "label=compiler-optimizer.recovery-lease=" + interrupted["token"],
                    "--format",
                    "{{.ID}}",
                ],
                capture_output=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            assert not remaining
            until(
                lambda: repo.get_task(task)["status"] in {"completed", "failed"},
                processes,
                logs,
                timeout=240,
            )
            result = client.status(task)
            assert result.status == "completed", (
                result.report_json + "\n" + "\n".join(p.read_text()[-4000:] for p in logs)
            )
            with repo._transaction(write=False) as db:
                used = {
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT worker_id FROM fleet_jobs WHERE task_id=? AND state IN "
                        "('completed','failed')",
                        (task,),
                    )
                }
                counts = db.execute(
                    "SELECT COUNT(*) FROM fleet_jobs WHERE task_id=? AND state='completed'", (task,)
                ).fetchone()[0]
            assert len(used) == 2 and counts > 10
            assert len(repo.list_tasks()) == 1
            output = client.download(task, tmp_path / "client/results")
            assert (output / "source/main.c").is_file()
            assert repo.get_decision(task) is not None
            assert any(r["kind"] == "worker-result" for r in repo.list_artifacts(task))
            for _, root in workers:
                assert not list(root.rglob("*.sqlite3"))
            assert coordinator.poll() is None and scheduler.poll() is None
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for output in handles:
            output.close()
