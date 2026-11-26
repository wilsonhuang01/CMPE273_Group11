"""Actual NVCC PTX generation on a CPU-only Docker worker; no NVIDIA device is requested."""

import os
from pathlib import Path

import pytest

from compiler_optimizer.storage.artifacts import ArtifactStore
from compiler_optimizer.workers.cuda_compile import CUDACompileRunner, compile_artifacts

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("CUDA_COMPILE_TESTS") != "1",
        reason="build compiler-cuda and set CUDA_COMPILE_TESTS=1",
    ),
]


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    return CUDACompileRunner(watchdog_directory=tmp_path_factory.mktemp("cuda-compile-watchdogs"))


def test_real_cuda_compiles_to_ptx_without_gpu_execution(tmp_path, runner):
    code = (Path(__file__).resolve().parents[1] / "examples/cuda/vector_add.cu").read_bytes()
    invocation = runner.compile_ptx(code)
    assert invocation.failure is None, invocation.log()
    assert b".target sm_75" in invocation.binary and b".entry vector_add" in invocation.binary
    assert invocation.sandbox["watchdog"]["state"] == "removed"
    assert "--ptx" in invocation.command and "--gpu-architecture=compute_75" in invocation.command


def test_real_cuda_syntax_error_is_rejected_and_unqualified(tmp_path, runner):
    report = compile_artifacts(
        runner, b"__global__ void bad( { this is invalid; }", ArtifactStore(tmp_path / "artifacts")
    )
    assert report["status"] == "rejected" and report["ptx"] is None
    assert not report["correctness_verified"] and not report["execution_enabled"]
    assert not report["performance_qualified"]


def test_real_cuda_submitted_api_compiles_versioned_rpc_candidate(tmp_path, runner):
    import json
    from concurrent.futures import ThreadPoolExecutor

    import grpc
    from test_action_n import PatchBackend, cuda_package

    from compiler_optimizer.agents.rpc_service import AgentService
    from compiler_optimizer.client import OptimizerClient, prepare_upload
    from compiler_optimizer.runtime.config import Settings
    from compiler_optimizer.runtime.coordinator import CoordinatorService
    from compiler_optimizer.runtime.dispatch import SubmissionDispatcher
    from compiler_optimizer.runtime.transfer import ArtifactTransferService
    from compiler_optimizer.shared.agent_roles import ROLES
    from compiler_optimizer.shared.v1 import contracts_pb2 as pb
    from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
    from compiler_optimizer.shared.workflow import default_workflow
    from compiler_optimizer.storage import Repository

    repository = Repository(tmp_path / "state")
    agent = AgentService(tmp_path / "agent", pb.PERFORMANCE, backend=PatchBackend())
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    rpc.add_AgentServiceServicer_to_server(agent, server)
    config = {**default_workflow(), "agents": ["performance"], "agent_backend": "rpc"}
    service = CoordinatorService(repository, workflow_configuration=config)
    rpc.add_CoordinatorServiceServicer_to_server(service, server)
    rpc.add_ArtifactTransferServiceServicer_to_server(
        ArtifactTransferService(repository, service),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        settings = Settings(
            role="coordinator",
            agent_backend="rpc",
            agent_endpoints=json.dumps({spec.name: f"127.0.0.1:{port}" for spec in ROLES.values()}),
        )
        package = cuda_package()
        header = prepare_upload(package, tmp_path / "request.json", "cuda-ptx")
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            client = OptimizerClient(channel)
            client.submit(header, package.package_bundle)
            dispatcher = SubmissionDispatcher(repository, settings)
            assert dispatcher.run_once()
            result = client.status(header.meta.task_id)
            assert result.status == "completed", result.report_json
            report = json.loads(result.report_json)
            assert report["baseline"]["status"] == "compiled"
            assert report["candidates"][0]["status"] == "compiled", report["candidates"]
            proposal = repository.list_proposals(header.meta.task_id)[0]
            assert proposal.HasField("agent_provenance")
            assert proposal.source.version == 2
            assert not report["performance_qualified"] and not result.HasField("selected_source")
            output = client.download(header.meta.task_id, tmp_path / "output")
            for compiled in [report["baseline"], *report["candidates"]]:
                ptx = (output / "artifacts" / compiled["ptx"]["sha256"]).read_bytes()
                assert b".target sm_75" in ptx and b".entry kernel" in ptx
            assert repository.get_decision(header.meta.task_id) is None
    finally:
        server.stop(0).wait()
        agent.close()
