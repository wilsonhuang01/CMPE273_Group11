"""Actual RPC intake and coordinator-process restart against Docker/RabbitMQ/Redis."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import grpc
import pytest

from compiler_optimizer.evaluation.__main__ import native_target
from compiler_optimizer.monitoring.watchdog import read_json, remove_owned
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("PIPELINE_INTEGRATION_TESTS") != "1",
        reason="set PIPELINE_INTEGRATION_TESTS=1 with native toolchain and services ready",
    ),
]
ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(action, process, log, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, log.read_text()[-8000:]
        value = action()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("coordinator did not reach checkpoint: " + log.read_text()[-8000:])


def test_submission_resumes_after_actual_coordinator_process_loss(tmp_path):
    repo = Repository(tmp_path / "state")
    package = read_package(ROOT / "examples/sum-array")
    target = native_target()
    compiler = DockerRunner(DEFAULT_IMAGES[target], target=target).compiler(
        package.manifest.language
    )
    request = pb.Submission(
        meta=metadata(str(uuid4())),
        manifest=package.manifest,
        package=repo.artifacts.put(package.package_bundle),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(package.source_bundle)),
        compiler=compiler,
    )
    port, health = free_port(), free_port()
    env = dict(
        os.environ,
        STATE_DIRECTORY=str(repo.root),
        GRPC_PORT=str(port),
        HEALTH_PORT=str(health),
        HEALTH_HOST="127.0.0.1",
        GRPC_HOST="127.0.0.1",
        EXECUTION_ENABLED="true",
    )
    log = tmp_path / "coordinator.log"
    process = None

    def start(output):
        return subprocess.Popen(
            [sys.executable, str(ROOT / "tools/project.py"), "serve", "--no-start"],
            cwd=ROOT,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )

    try:
        with log.open("w") as output:
            process = start(output)
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=15)
                client = rpc.CoordinatorServiceStub(channel)
                client.SubmitSubmission(request, timeout=10)
                task = request.meta.task_id
                verified = wait_for(
                    lambda: next(
                        (
                            row
                            for row in repo.list_results(task)
                            if row.WhichOneof("subject") == "proposal_id"
                            and row.WhichOneof("outcome") == "success"
                        ),
                        None,
                    ),
                    process,
                    log,
                )
                assert (
                    len(
                        [
                            r
                            for r in repo.list_results(task)
                            if r.WhichOneof("subject") == "baseline"
                        ]
                    )
                    == 1
                )
                process.kill()
                process.wait(timeout=10)
            process = start(output)
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=15)
                client = rpc.CoordinatorServiceStub(channel)
                # Repeated intake after restart must not reset policies or attempts.
                client.SubmitSubmission(request, timeout=10)
                wait_for(lambda: repo.get_execution(task)["finished"], process, log)
                result = client.GetOptimizationResult(pb.OptimizationQuery(task_id=task), timeout=5)
                assert result.status == "completed", result.report_json
                assert result.stage == "completed"
                assert result.selected_source.bundle.sha256
                assert repo.get_execution(task)["attempts"] == 2
                assert len(repo.list_tasks()) == 1
                assert (
                    len(
                        [
                            r
                            for r in repo.list_results(task)
                            if r.WhichOneof("subject") == "baseline"
                        ]
                    )
                    == 1
                )
                counts = len(repo.list_jobs(task)), len(repo.list_benchmarks(task))
                assert counts[1] >= 1
                assert [
                    row for row in repo.list_results(task) if row.job_id == verified.job_id
                ] == [verified]
            process.terminate()
            process.wait(timeout=15)
            # A further process restart must reuse the final decision unchanged.
            process = start(output)
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=15)
                final = rpc.CoordinatorServiceStub(channel).GetOptimizationResult(
                    pb.OptimizationQuery(task_id=task), timeout=5
                )
                assert final == result
                assert counts == (len(repo.list_jobs(task)), len(repo.list_benchmarks(task)))
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for path in (repo.root / "submission-watchdogs").glob("*/*/lease.json"):
            remove_owned(read_json(path))
