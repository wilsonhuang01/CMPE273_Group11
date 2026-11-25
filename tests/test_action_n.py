"""Common API compile tasks and simulated device fencing cannot imply hardware performance."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import grpc
import pytest

from compiler_optimizer.agents.performance import PerformanceAgent
from compiler_optimizer.client import OptimizerClient, prepare_upload
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.dispatch import SubmissionDispatcher
from compiler_optimizer.runtime.transfer import ArtifactTransferService
from compiler_optimizer.shared.contracts import validate_message
from compiler_optimizer.shared.gpu_recovery import SimulatedGPURegistry
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.cuda_compile import CUDACompileRunner
from compiler_optimizer.workers.docker_runner import Invocation
from compiler_optimizer.workers.package import package_from_files


def cuda_package(source=b'extern "C" __global__ void kernel(float *p) { p[0] = 1.0f; }'):
    return package_from_files(
        {
            "submission.json": json.dumps(
                {
                    "schema_version": 1,
                    "name": "cuda-test",
                    "language": "cuda17",
                    "sources": ["kernel.cu"],
                    "tests": [],
                    "benchmark_test_ids": [],
                    "limits": {
                        "compile_wall_seconds": 60,
                        "compile_memory_mib": 2048,
                        "run_wall_seconds": 1,
                        "run_memory_mib": 128,
                        "output_kib": 64,
                    },
                }
            ).encode(),
            "kernel.cu": source,
        }
    )


class CompileRunner:
    compiler = CUDACompileRunner.compiler
    image = "sha256:" + "a" * 64
    cuda_version = "13.0.88"

    def __init__(self):
        self.calls = []

    def compile_ptx(self, source, *, limits):
        self.calls.append(source)
        assert limits.compile_wall_seconds == 60
        bad = b"invalid" in source
        return Invocation(
            b"",
            b"syntax error" if bad else b"",
            0.1,
            1024,
            1 if bad else 0,
            failure="nonzero_exit" if bad else None,
            binary=None if bad else b".version 9.0\n.target sm_75\n",
        )


class PatchBackend:
    def generate(self, request, sources):
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": "cuda-test-edit",
                "rationale": "Compile a versioned CUDA proposal",
                "edits": [{"path": "kernel.cu", "old": "1.0f", "new": "2.0f"}],
            }
        ).encode()


class CandidateClient:
    def __init__(self, repo):
        self.repo = repo

    def generator(self, role):
        assert role == pb.PERFORMANCE
        return PerformanceAgent(self.repo.artifacts, PatchBackend())


def intake(repo, runner, package, tmp_path):
    header = prepare_upload(package, tmp_path / "upload.json", "cuda-ptx")
    from compiler_optimizer.client import upload_frames

    context = SimpleNamespace(time_remaining=lambda: 60, is_active=lambda: True)
    transfer = ArtifactTransferService(
        repo, compiler_resolver=lambda t, lang: runner.compiler(lang)
    )
    transfer.UploadPackage(upload_frames(header, package.package_bundle), context)
    return header.meta.task_id


def test_cuda_api_versions_candidates_and_resumes_after_compile_commit(tmp_path, monkeypatch):
    repo, runner = Repository(tmp_path / "state"), CompileRunner()
    task = intake(repo, runner, cuda_package(), tmp_path)
    from compiler_optimizer.coordinator.cuda import run_compile_task

    original = repo.save_cuda_record

    class Crash(BaseException):
        pass

    def crash_after_commit(task_id, owner, report, *args, **kwargs):
        ref = original(task_id, owner, report, *args, **kwargs)
        if owner == "baseline":
            raise Crash()
        return ref

    monkeypatch.setattr(repo, "save_cuda_record", crash_after_commit)
    with pytest.raises(Crash):
        run_compile_task(
            repo, repo.get_task(task)["submission"], runner, CandidateClient(repo), ["performance"]
        )
    repo = Repository(repo.root)
    run_compile_task(
        repo, repo.get_task(task)["submission"], runner, CandidateClient(repo), ["performance"]
    )
    report = repo.cuda_task_report(task)[1]
    assert len(runner.calls) == 2
    assert report["candidates"][0]["source_version"] == 2
    assert (
        report["candidates"][0]["parent_source_sha256"]
        == repo.get_task(task)["submission"].source.bundle.sha256
    )
    assert len(repo.list_proposals(task)) == 1
    assert repo.get_task(task)["status"] == "completed"
    assert not report["performance_qualified"] and not report["correctness_verified"]
    assert repo.get_decision(task) is None and repo.list_results(task) == []
    assert {r["kind"] for r in repo.list_artifacts(task)} >= {
        "cuda-patch",
        "cuda-ptx",
        "cuda-diagnostics",
    }
    run_compile_task(repo, repo.get_task(task)["submission"], runner)
    assert len(runner.calls) == 2


@pytest.mark.parametrize("bad", [False, True])
def test_cuda_live_submit_status_and_download(tmp_path, bad):
    repo, runner = Repository(tmp_path / "state"), CompileRunner()
    package = cuda_package(b"invalid CUDA" if bad else b"__global__ void kernel() {}")
    header = prepare_upload(package, tmp_path / "upload.json", "cuda-ptx")
    service = CoordinatorService(repo)
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    rpc.add_CoordinatorServiceServicer_to_server(service, server)
    rpc.add_ArtifactTransferServiceServicer_to_server(
        ArtifactTransferService(
            repo, service, compiler_resolver=lambda t, lang: runner.compiler(lang)
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            client = OptimizerClient(channel)
            client.submit(header, package.package_bundle)
            worker = SubmissionDispatcher(
                repo, Settings(role="coordinator"), runner_factory=lambda _: runner
            )
            assert worker.run_once()
            result = client.status(header.meta.task_id)
            assert result.status == ("failed" if bad else "completed")
            assert result.HasField("report") and not result.HasField("selected_source")
            output = client.download(header.meta.task_id, tmp_path / "download")
            report = json.loads((output / "report.json").read_bytes())
            assert report["selection"] == "unavailable_compile_only"
            assert not report["performance_qualified"]
            assert len(runner.calls) == 1
            client.submit(header, package.package_bundle)
            assert not worker.run_once()
    finally:
        server.stop(0).wait()


def test_cuda_cannot_enter_cpu_verification_or_smuggle_cpu_tests(tmp_path):
    package = cuda_package()
    files = dict(package.files)
    manifest = json.loads(files["submission.json"])
    manifest["language"] = "c17"
    files["submission.json"] = json.dumps(manifest).encode()
    with pytest.raises(ContractError):
        package_from_files(files)
    repo, runner = Repository(tmp_path / "state"), CompileRunner()
    task = intake(repo, runner, package, tmp_path)
    from compiler_optimizer.workers.baseline import metadata

    submission = repo.get_task(task)["submission"]
    job = pb.VerificationJob(
        meta=metadata(task),
        job_id=task,
        baseline=True,
        source=submission.source,
        compiler=submission.compiler,
        manifest=submission.manifest,
        attempt=1,
    )
    with pytest.raises(ContractError, match="compile-only"):
        validate_message(job)


def test_simulated_device_leases_fence_expired_work_and_require_all_host_checks():
    registry = SimulatedGPURegistry()
    registry.register("gpu-0", "host")
    registry.register("gpu-1", "host")
    with pytest.raises(ContractError):
        registry.acquire("gpu-0", "job", now_ms=0, ttl_ms=100)
    registry.readmit("gpu-0", diagnostics_passed=True, smoke_test_passed=True)
    registry.readmit("gpu-1", diagnostics_passed=True, smoke_test_passed=True)
    lease = registry.acquire("gpu-0", "job", now_ms=0, ttl_ms=100)
    with pytest.raises(ContractError, match="already leased"):
        registry.acquire("gpu-0", "other", now_ms=1, ttl_ms=100)
    assert not registry.check(lease, now_ms=100)
    report = registry.readmit("gpu-0", diagnostics_passed=True, smoke_test_passed=True)
    assert report["host_drained"]
    report = registry.readmit("gpu-1", diagnostics_passed=True, smoke_test_passed=True)
    assert report["can_schedule_simulated"] and not report["actual_device_recovery"]
    assert report["simulation"] and not report["hardware_validated"]
    new = registry.acquire("gpu-0", "new-job", now_ms=100, ttl_ms=100)
    assert new.fence > lease.fence
    assert not registry.check(lease, now_ms=101)
    registry.release(new, now_ms=102)
    with pytest.raises(ContractError):
        registry.acquire("gpu-0", "other", now_ms=103, ttl_ms=100)
    report = registry.readmit("gpu-0", diagnostics_passed=True, smoke_test_passed=False)
    assert not report["can_schedule_simulated"] and report["host_drained"]
