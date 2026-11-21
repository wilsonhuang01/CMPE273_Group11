"""Package validation, baseline gates/persistence, and Docker adapter boundaries."""

import base64
import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from compiler_optimizer.shared.contracts import BASE_FLAGS
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError, Invocation
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.policy import POLICY_DIGEST, POLICY_VERSION


def mock_guard(monkeypatch, runner, command, directory):
    class Guard:
        token = "test-token"
        process = object()

        def start(self):
            return self

        def finish(self, reason):
            return {"state": "removed", "reason": reason}

    guard = Guard()
    guard.directory = directory
    monkeypatch.setattr(runner, "_new_watchdog", lambda *args: guard)
    monkeypatch.setattr(runner, "_validate_container", lambda *args: None)
    monkeypatch.setattr(
        runner,
        "_attach",
        lambda name, payload, guard, wall_seconds: (
            command(["start", "--attach", name], timeout=wall_seconds + 5),
            None,
        ),
    )
    return guard


@pytest.fixture
def submission_dir(tmp_path):
    original = Path(__file__).resolve().parents[1] / "examples/sum-array"
    return Path(shutil.copytree(original, tmp_path / "submission"))


class FakeRunner:
    hardware = "unit-test worker"

    def __init__(self, *, compiled=None, responses=None):
        self.compiled = compiled or Invocation(b"", b"", 0.2, 1024, 0, binary=b"fake executable")
        self.responses = responses or {}
        self.calls = 0

    def compiler(self, language):
        return pb.CompilerSettings(
            executable="clang",
            version="18.0.0",
            language=language,
            image_digest="sha256:" + "a" * 64,
            target="linux-x86_64",
            flags=["-std=c17", *BASE_FLAGS],
        )

    def compile(self, package):
        if isinstance(self.compiled, Exception):
            raise self.compiled
        return self.compiled

    def execute(self, binary, case, package):
        self.calls += 1
        return self.responses.get(
            self.calls, Invocation(package.files[case.expected_stdout], b"", 0.01, 2048, 0)
        )


def test_package_bundles_are_deterministic_and_source_only(submission_dir):
    (submission_dir / "local.h").write_text("#define VALUE 1\n")
    first = read_package(submission_dir)
    os.utime(submission_dir / "main.c", (1000, 2000))
    second = read_package(submission_dir)
    assert first.package_bundle == second.package_bundle
    assert first.source_bundle == second.source_bundle
    with zipfile.ZipFile(io.BytesIO(first.source_bundle)) as source:
        assert source.namelist() == ["local.h", "main.c"]
    with zipfile.ZipFile(io.BytesIO(first.package_bundle)) as package:
        assert "submission.json" in package.namelist()
        assert "tests/mixed.out" in package.namelist()


@pytest.mark.parametrize(
    "change",
    [
        "missing-source",
        "missing-fixture",
        "symlink-file",
        "symlink-directory",
        "fifo",
        "too-many",
        "too-large",
    ],
)
def test_package_rejects_invalid_files(submission_dir, change, tmp_path):
    if change == "missing-source":
        (submission_dir / "main.c").unlink()
    elif change == "missing-fixture":
        (submission_dir / "tests/mixed.in").unlink()
    elif change == "symlink-file":
        (submission_dir / "link").symlink_to(submission_dir / "main.c")
    elif change == "symlink-directory":
        (submission_dir / "link").symlink_to(tmp_path, target_is_directory=True)
    elif change == "fifo":
        os.mkfifo(submission_dir / "fifo")
    elif change == "too-many":
        for index in range(101):
            (submission_dir / f"extra-{index}").touch()
    else:
        (submission_dir / "oversized").write_bytes(b"x" * (10 * 1024**2))
    with pytest.raises(ContractError):
        read_package(submission_dir)


def test_success_persists_baseline_and_does_not_select_or_start_agents(submission_dir, tmp_path):
    repo = Repository(tmp_path / "state")
    runner = FakeRunner()
    result = run_baseline(read_package(submission_dir), repo, runner)
    assert result.WhichOneof("outcome") == "success"
    assert list(result.success.passed_test_ids) == ["empty", "mixed"]
    assert result.success.benchmarks[0].peak_memory_bytes == 2048
    assert runner.calls == 3
    reopened = Repository(repo.root)
    assert reopened.get_task(result.meta.task_id)["status"] == "running"
    assert reopened.list_proposals(result.meta.task_id) == []
    assert reopened.get_decision(result.meta.task_id) is None
    assert reopened.list_results(result.meta.task_id) == [result]
    assert {item["kind"] for item in reopened.list_artifacts(result.meta.task_id)} == {
        "source",
        "package",
        "binary",
        "log",
        "benchmark",
    }
    log = json.loads(reopened.artifacts.get(result.log))
    assert len(log["invocations"]) == 4
    assert base64.b64decode(log["invocations"][1]["stdout_base64"]) == b"0\n"


def test_compile_failure_rejects_before_test_execution(submission_dir, tmp_path):
    runner = FakeRunner(compiled=Invocation(b"", b"syntax error", 0.1, 1024, 1, "nonzero_exit"))
    repo = Repository(tmp_path / "state")
    result = run_baseline(read_package(submission_dir), repo, runner)
    assert result.failure.reason == pb.COMPILATION_ERROR
    assert result.failure.stage == pb.COMPILE
    assert runner.calls == 0
    assert repo.get_task(result.meta.task_id)["status"] == "failed"


@pytest.mark.parametrize(
    ("invocation", "reason"),
    [
        (Invocation(b"wrong\n", b"", 0.1, 1024, 0), pb.OUTPUT_MISMATCH),
        (Invocation(b"", b"", 0.1, 1024, 7, "nonzero_exit"), pb.NONZERO_EXIT),
        (Invocation(b"", b"", 0.1, 1024, -11, "signal"), pb.SIGNAL_TERMINATION),
        (Invocation(b"", b"", 0.1, 1024, -9, "timeout"), pb.TIMEOUT),
        (Invocation(b"", b"", 0.1, 1024, -9, "memory_limit"), pb.MEMORY_LIMIT),
        (Invocation(b"", b"", 0.1, 1024, -9, "output_limit"), pb.OUTPUT_LIMIT),
    ],
)
def test_failing_test_never_produces_accepted_baseline(
    submission_dir, tmp_path, invocation, reason
):
    repo = Repository(tmp_path / "state")
    runner = FakeRunner(responses={1: invocation})
    result = run_baseline(read_package(submission_dir), repo, runner)
    assert result.failure.reason == reason
    assert result.failure.test_id == "empty"
    assert result.failure.stage == pb.TEST
    assert runner.calls == 1
    assert repo.get_task(result.meta.task_id)["status"] == "failed"
    assert len(repo.list_results(result.meta.task_id)) == 1


def test_measurement_run_also_requires_correct_stdout(submission_dir, tmp_path):
    runner = FakeRunner(responses={3: Invocation(b"changed\n", b"", 0.1, 1024, 0)})
    result = run_baseline(read_package(submission_dir), Repository(tmp_path / "state"), runner)
    assert result.failure.reason == pb.OUTPUT_MISMATCH
    assert result.failure.stage == pb.MEASURE


def test_infrastructure_failure_is_persisted_distinctly(submission_dir, tmp_path):
    runner = FakeRunner(compiled=InfrastructureError("Docker connection lost"))
    repo = Repository(tmp_path / "state")
    result = run_baseline(read_package(submission_dir), repo, runner)
    assert result.failure.reason == pb.INFRASTRUCTURE_ERROR
    assert result.failure.stage == pb.INFRASTRUCTURE
    assert repo.list_results(result.meta.task_id) == [result]


def test_docker_adapter_bounds_resources_and_cleans_up(monkeypatch, tmp_path):
    calls = []
    runner = object.__new__(DockerRunner)
    runner.hardware = json.dumps({"docker_engine": {"cpus": 4, "memory_bytes": 4 * 1024**3}})
    runner._admission_engine = str(tmp_path)
    runner.image = "sha256:" + "a" * 64
    runner.target, runner.platform = "linux-x86_64", "linux/amd64"
    runner.measurement_version = 1

    def command(args, **kwargs):
        calls.append((args, kwargs))
        report = {
            "stdout": "",
            "stderr": "",
            "wall_seconds": 0.01,
            "peak_memory_bytes": 4096,
            "returncode": 0,
            "failure": None,
            "policy_version": POLICY_VERSION,
            "policy_digest": POLICY_DIGEST,
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(report).encode(), b"")

    monkeypatch.setattr(runner, "_command", command)
    mock_guard(monkeypatch, runner, command, tmp_path)
    runner._invoke({"wall_seconds": 2, "output_bytes": 1024}, memory=256, compile_mode=False)
    create = calls[0][0]
    assert create[create.index("--network") + 1] == "none"
    assert "--read-only" in create and "--security-opt" in create
    assert create[create.index("--memory") + 1] == "256m"
    assert create[create.index("--memory-swap") + 1] == "256m"
    assert create[create.index("--pids-limit") + 1] == "2"
    assert "--volume" not in create and "--mount" not in create
    assert not any(arg.startswith(("/work/src:", "/work/build:")) for arg in create)
    assert any(arg.startswith("/work/run:rw,exec,") for arg in create)
    assert calls[1][1]["timeout"] == 7
    assert calls[-1][0][:2] == ["rm", "--force"]


@pytest.mark.parametrize(
    "reply",
    [
        b"not JSON",
        json.dumps(
            {
                "failure": None,
                "returncode": 7,
                "policy_version": POLICY_VERSION,
                "policy_digest": POLICY_DIGEST,
                "wall_seconds": 0.1,
                "peak_memory_bytes": 1024,
                "stdout": "",
                "stderr": "",
            }
        ).encode(),
    ],
)
def test_invalid_supervisor_reply_still_removes_container(monkeypatch, reply, tmp_path):
    calls = []
    runner = object.__new__(DockerRunner)
    runner.hardware = json.dumps({"docker_engine": {"cpus": 4, "memory_bytes": 4 * 1024**3}})
    runner._admission_engine = str(tmp_path)
    runner.image = "sha256:" + "a" * 64
    runner.target, runner.platform = "linux-x86_64", "linux/amd64"
    runner.measurement_version = 1

    def command(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, reply, b"")

    monkeypatch.setattr(runner, "_command", command)
    mock_guard(monkeypatch, runner, command, tmp_path)
    with pytest.raises(InfrastructureError, match="response") as caught:
        runner._invoke({"wall_seconds": 2, "output_bytes": 1024}, memory=256, compile_mode=False)
    assert calls[-1][:2] == ["rm", "--force"]
    diagnostic = json.loads((tmp_path / "protocol-error.json").read_text())
    assert base64.b64decode(diagnostic["stdout_base64"]) == reply
    assert diagnostic["docker_returncode"] == 0
    assert diagnostic["error_type"] == type(caught.value.__cause__).__name__
    assert diagnostic["error_type"] in str(caught.value)
    assert reply.decode() not in str(caught.value)


def test_success_is_rejected_when_watchdog_cannot_confirm_cleanup(monkeypatch, tmp_path):
    runner = object.__new__(DockerRunner)
    runner.hardware = json.dumps({"docker_engine": {"cpus": 4, "memory_bytes": 4 * 1024**3}})
    runner._admission_engine = str(tmp_path)
    runner.image = "sha256:" + "a" * 64
    runner.target, runner.platform = "linux-x86_64", "linux/amd64"
    runner.measurement_version = 1
    report = json.dumps(
        {
            "stdout": "",
            "stderr": "",
            "wall_seconds": 0.01,
            "peak_memory_bytes": 4096,
            "returncode": 0,
            "failure": None,
            "policy_version": POLICY_VERSION,
            "policy_digest": POLICY_DIGEST,
        }
    ).encode()

    def command(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, report, b"")

    monkeypatch.setattr(runner, "_command", command)
    guard = mock_guard(monkeypatch, runner, command, tmp_path)

    def failed_cleanup(reason):
        raise RuntimeError("cleanup_failed")

    guard.finish = failed_cleanup
    with pytest.raises(InfrastructureError, match="cleanup unconfirmed"):
        runner._invoke({"wall_seconds": 2, "output_bytes": 1024}, memory=256, compile_mode=False)
