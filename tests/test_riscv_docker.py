"""Real RV64GC C/C++ execution under QEMU inside a host-native isolated container."""

import json
import os
from pathlib import Path

import pytest
from test_agent_rpc import agents as agents

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.scheduler.recovery import RecoveryController
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.benchmark import run_benchmark
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import package_from_files, read_package
from compiler_optimizer.workers.verification_plan import ArrayGenerator, VerificationPlan
from compiler_optimizer.workers.verify import register_candidate, run_verification

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RISCV_DOCKER_TESTS") != "1",
        reason="build compiler-riscv and set RISCV_DOCKER_TESTS=1",
    ),
]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    return DockerRunner(
        "cmpe273-riscv:18",
        target="linux-riscv64",
        watchdog_directory=tmp_path_factory.mktemp("riscv-watchdogs"),
    )


def prepare(tmp_path, runner):
    repo = Repository(tmp_path / "state")
    package = read_package(EXAMPLES / "sum-array")
    baseline = run_baseline(package, repo, runner)
    assert baseline.WhichOneof("outcome") == "success", baseline.failure
    assert baseline.compiler.target == "linux-riscv64"
    return repo, baseline.meta.task_id


def candidate(repo, task, tmp_path, source):
    path = tmp_path / "candidate"
    path.mkdir()
    (path / "main.c").write_bytes(source)
    return register_candidate(repo, task, path)


def test_real_riscv_differential_ubsan_generated_cases_and_performance_denial(tmp_path, runner):
    repo, task = prepare(tmp_path, runner)
    proposal = candidate(
        repo, task, tmp_path, (EXAMPLES / "sum-array-optimized/main.c").read_bytes()
    )
    result = run_verification(
        repo,
        task,
        proposal,
        runner,
        VerificationPlan(generator=ArrayGenerator(273, 1, 1, 0, 8, -100, 100)),
    )
    assert result.WhichOneof("outcome") == "success", result.failure
    assert (
        json.loads(repo.artifacts.get(result.correctness_report))["execution"]["sanitizer_mode"]
        == "trap"
    )
    logs = json.loads(repo.artifacts.get(result.log))["invocations"]
    assert any("/usr/bin/qemu-riscv64" in item["command"] for item in logs)
    assert any("-fsanitize-trap=all" in item["command"] for item in logs)
    with pytest.raises(ContractError, match="correctness-only"):
        run_benchmark(repo, task, result.job_id, runner)
    assert not repo.list_benchmarks(task)


@pytest.mark.parametrize("mode", ["wrong", "ub", "hang", "compiler"])
def test_riscv_rejects_bad_candidates_without_losing_the_baseline(tmp_path, runner, mode):
    repo, task = prepare(tmp_path, runner)
    source = (EXAMPLES / "sum-array/main.c").read_bytes()
    old, new = {
        "wrong": (b'printf("%lld\\n", sum);', b'printf("%lld\\n", sum+1);'),
        "ub": (
            b"int main(void) {",
            b"int main(void) { volatile int maximum=2147483647; "
            b"volatile int overflow=maximum+1; (void)overflow;",
        ),
        "hang": (b"int main(void) {", b"int main(void) { volatile unsigned v=0; for(;;) v++;"),
        "compiler": (b"int main(void) {", b"int main(void) { this is invalid C;"),
    }[mode]
    proposal = candidate(repo, task, tmp_path, source.replace(old, new))
    result = run_verification(repo, task, proposal, runner)
    assert result.WhichOneof("outcome") == "failure"
    assert (
        result.failure.reason
        in {
            "wrong": {pb.OUTPUT_MISMATCH},
            "ub": {pb.SIGNAL_TERMINATION, pb.SANITIZER_FAILURE},
            "hang": {pb.TIMEOUT},
            "compiler": {pb.COMPILATION_ERROR},
        }[mode]
    ), result.failure
    assert repo.get_task(task)["status"] == "running"


def test_real_riscv_cpp17_and_emulator_process_network_isolation(tmp_path, runner):
    files = dict(read_package(EXAMPLES / "sum-array").files)
    manifest = json.loads(files["submission.json"])
    manifest.update(language="cpp17", sources=["main.cpp"])
    files["submission.json"] = json.dumps(manifest).encode()
    del files["main.c"]
    files["main.cpp"] = b"""#include <iostream>
#include <unistd.h>
#include <sys/socket.h>
#include <errno.h>
int main() {
    if(geteuid()!=10001) return 10;
    errno=0; if(fork()!=-1 || errno!=EPERM) return 11;
    errno=0; if(socket(AF_INET,SOCK_STREAM,0)!=-1 || errno!=EPERM) return 12;
    int n,x; long long total=0; std::cin>>n;
    while(n--){std::cin>>x; total+=x;} std::cout<<total<<std::endl;
}"""
    result = run_baseline(package_from_files(files), Repository(tmp_path / "state"), runner)
    assert result.WhichOneof("outcome") == "success", result.failure


@pytest.mark.skipif(
    os.environ.get("RECOVERY_INTEGRATION_TESTS") != "1", reason="requires local RabbitMQ and Redis"
)
def test_real_riscv_cohort_uses_recovery_reviews_and_retains_baseline(
    tmp_path, runner, monkeypatch
):
    from compiler_optimizer.workers.docker_runner import InfrastructureError

    repo, task = prepare(tmp_path, runner)
    original_compile = runner.compile
    fail = [True]

    def unavailable_once(*args, **kwargs):
        if fail[0]:
            fail[0] = False
            raise InfrastructureError("injected RISC-V compiler transport loss")
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(runner, "compile", unavailable_once)
    settings = Settings.from_env("coordinator")
    coordinator = CohortCoordinator(
        repo,
        runner,
        RabbitProposalQueue(settings),
        recovery=RecoveryController.local(repo, runner, settings),
    )
    audit = coordinator.run(task, agents=["performance"])
    assert audit["status"] == "retained_baseline", audit
    assert audit["candidates"][0]["reasons"] == ["emulated_target_correctness_only"]
    assert audit["candidates"][0]["votes"] == {"correctness": "approve", "critic": "approve"}
    assert not repo.list_benchmarks(audit["task_id"])
    proposal = repo.list_proposals(audit["task_id"])[0]
    job = repo.get_recovery_job(audit["task_id"], proposal.proposal_id)
    assert job["state"] == "completed"
    attempts = repo.recovery_attempts(task, proposal.proposal_id)
    assert [a["status"] for a in attempts] == ["infrastructure_failure", "completed"]
    assert attempts[0]["worker_id"] != attempts[1]["worker_id"]
    assert json.loads(job["execution_context_json"])["compiler"]["target"] == "linux-riscv64"
    assert coordinator.run(audit["task_id"]) == audit


@pytest.mark.skipif(
    os.environ.get("RECOVERY_INTEGRATION_TESTS") != "1", reason="requires local RabbitMQ and Redis"
)
@pytest.mark.parametrize("language", ["c17", "cpp17"])
def test_riscv_submission_uses_agent_rpc_broker_and_correctness_audit(
    tmp_path, runner, agents, language
):
    from dataclasses import replace

    from compiler_optimizer.client import prepare_upload, upload_frames
    from compiler_optimizer.runtime.coordinator import CoordinatorService
    from compiler_optimizer.runtime.dispatch import SubmissionDispatcher
    from compiler_optimizer.runtime.transfer import ArtifactTransferService
    from compiler_optimizer.shared.workflow import default_workflow

    repo = Repository(tmp_path / "state")
    package = read_package(EXAMPLES / "sum-array")
    if language == "cpp17":
        files = dict(package.files)
        manifest = json.loads(files["submission.json"])
        manifest.update(language=language, sources=["main.cpp"])
        files["submission.json"] = json.dumps(manifest).encode()
        files["main.cpp"] = files.pop("main.c")
        package = package_from_files(files)
    settings = replace(
        Settings.from_env("coordinator"),
        agent_backend="rpc",
        agent_endpoints=agents[1].agent_endpoints,
        agent_token=agents[1].agent_token,
    )
    config = {**default_workflow(), "agents": ["architecture"], "agent_backend": "rpc"}
    service = CoordinatorService(repo, workflow_configuration=config)
    transfer = ArtifactTransferService(
        repo, service, compiler_resolver=lambda t, lang: runner.compiler(lang)
    )
    from types import SimpleNamespace

    context = SimpleNamespace(time_remaining=lambda: 60, is_active=lambda: True)
    header = prepare_upload(package, tmp_path / "request.json", "linux-riscv64")
    transfer.UploadPackage(upload_frames(header, package.package_bundle), context)
    dispatcher = SubmissionDispatcher(repo, settings, runner_factory=lambda _: runner)
    assert dispatcher.run_once()
    task = header.meta.task_id
    result = service.GetOptimizationResult(pb.OptimizationQuery(task_id=task), context)
    assert result.status == "completed", result.report_json
    report = json.loads(result.report_json)
    assert report["status"] == "retained_baseline"
    candidate = report["candidates"][0]
    assert candidate["reasons"] == ["emulated_target_correctness_only"], candidate
    assert candidate["votes"] == {"correctness": "approve", "critic": "approve"}
    proposal = repo.list_proposals(task)[0]
    assert proposal.HasField("agent_provenance")
    assert proposal.compiler.target == "linux-riscv64"
    verified = next(r for r in repo.list_results(task) if r.proposal_id == proposal.proposal_id)
    evidence = json.loads(repo.artifacts.get(verified.correctness_report))
    assert evidence["correctness_passed"]
    assert evidence["execution"]["mode"] == "qemu-user"
    assert not repo.list_benchmarks(task)
