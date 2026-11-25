"""LLVM recipe admission, immutable stage chains, and exact-executable selection."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_selection import CohortBroker, MeasuredRunner

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.shared.contracts import validate_message
from compiler_optimizer.shared.correctness import validate_evidence
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import ConflictError
from compiler_optimizer.workers.baseline import metadata, run_baseline
from compiler_optimizer.workers.docker_runner import InfrastructureError, Invocation
from compiler_optimizer.workers.llvm_ir import (
    IRPassAgent,
    IRPassBackend,
    descriptor,
    encode,
    validate_artifacts,
)
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.verify import run_verification


class IRRunner(MeasuredRunner):
    native_execution = True

    def __init__(self, package):
        super().__init__(package)
        self.stages = []
        self.fail_stage = None

    def llvm_stage(
        self, package, stage, *, pipeline_id, modules=(), source=None, build_profile="normal"
    ):
        self.stages.append((stage, build_profile))
        if stage == self.fail_stage:
            return Invocation(b"", b"deadline", 0.01, 1024, -9, failure="timeout")
        if stage == "emit":
            binary = b"BC" + build_profile.encode() + package.sources[source]
        elif stage == "link":
            binary = b"linked:" + b"".join(modules)
        elif stage == "optimize":
            binary = b"optimized:" + modules[0]
        elif stage == "lower":
            candidate = modules[0].startswith(b"optimized:")
            binary = (
                "llvm-candidate:" if candidate else "llvm-baseline:"
            ).encode() + build_profile.encode()
            self.binaries[binary] = "candidate" if candidate else "baseline"
            self.kinds[binary] = "performance" if candidate else "baseline"
        else:
            binary = modules[0]
        return Invocation(
            b"",
            b"",
            0.01,
            1024,
            0,
            binary=binary,
            build_profile=build_profile,
            sandbox={"policy_version": 1},
        )


@pytest.fixture
def generated(tmp_path):
    package = read_package(Path(__file__).parents[1] / "examples/sum-array")
    repo, runner = Repository(tmp_path / "state"), IRRunner(package)
    baseline = run_baseline(package, repo, runner)
    submission = repo.get_task(baseline.meta.task_id)["submission"]
    request = pb.OptimizationRequest(
        meta=metadata(baseline.meta.task_id),
        proposal_id=str(uuid4()),
        role=pb.PERFORMANCE,
        manifest=submission.manifest,
        source=submission.source,
        compiler=submission.compiler,
    )
    repo.save_request(request)
    agent = IRPassAgent(repo, runner, "scalar-v1", "undefined")
    proposal, ref = agent.generate(request)
    return repo, runner, request, proposal, ref, agent


def test_immutable_stage_cache_and_typed_wire_roundtrip(generated):
    repo, runner, request, proposal, ref, agent = generated
    validate_message(proposal)
    assert pb.Proposal.FromString(proposal.SerializeToString()) == proposal
    assert ref == proposal.source.bundle
    assert validate_artifacts(repo.artifacts, proposal)
    before = list(runner.stages)
    second, second_ref = agent.generate(request)
    assert second_ref == ref and second.ir == proposal.ir
    assert runner.stages == before
    repo.save_proposal(proposal)
    assert all(repo.artifacts.get(r) for r in validate_artifacts(repo.artifacts, proposal))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("pipeline_id", "default<O3>"),
        ("pipeline_id", "-load-pass-plugin=/tmp/evil.so"),
        ("llvm_version", "18.1.4"),
        ("target_triple", "riscv64-unknown-linux-gnu"),
        ("baseline_source_sha256", "0" * 64),
    ],
)
def test_ir_contract_rejects_untrusted_recipes_and_identity_changes(generated, field, value):
    *_, proposal, ref, agent = generated
    setattr(proposal.ir, field, value)
    with pytest.raises(ContractError):
        validate_message(proposal)


@pytest.mark.parametrize("tamper", ["chain", "failed", "image", "pipeline", "baseline", "missing"])
def test_stage_evidence_cannot_be_rebound(generated, tamper):
    repo, _, _, proposal, _, _ = generated
    variant = proposal.ir.variants[0]
    report = json.loads(repo.artifacts.get(variant.stage_report))
    if tamper == "chain":
        report["stages"][-1]["inputs"] = ["0" * 64]
    elif tamper == "failed":
        report["stages"][0]["invocation"]["failure"] = "timeout"
    elif tamper == "image":
        report["compiler_image"] = "sha256:" + "b" * 64
    elif tamper == "pipeline":
        report["pipeline_id"] = "sroa-v1"
    elif tamper == "baseline":
        variant.baseline_bitcode.CopyFrom(variant.optimized_bitcode)
    else:
        report["stages"].pop()
    variant.stage_report.CopyFrom(repo.artifacts.put(encode(report)))
    proposal.source.bundle.CopyFrom(repo.artifacts.put(descriptor(proposal.ir)))
    with pytest.raises(ContractError):
        validate_artifacts(repo.artifacts, proposal)


def test_verification_cannot_drop_ir_or_substitute_lowering_input(generated):
    repo, runner, request, proposal, _, _ = generated
    repo.save_proposal(proposal)
    result = run_verification(repo, request.meta.task_id, proposal.proposal_id, runner)
    assert result.WhichOneof("outcome") == "success"
    job = repo.list_jobs(request.meta.task_id)[-1]
    changed = pb.VerificationJob()
    changed.CopyFrom(job)
    changed.meta.CopyFrom(metadata(request.meta.task_id))
    changed.job_id = str(uuid4())
    changed.ClearField("ir")
    with pytest.raises(ConflictError, match="LLVM"):
        repo.save_job(changed)
    report = json.loads(repo.artifacts.get(result.correctness_report))
    original = json.loads(json.dumps(report))
    row = next(c for c in report["checks"] if c["phase"] == "candidate_compile")
    row["ir_bitcode_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="LLVM bitcode"):
        validate_evidence(
            report,
            result,
            job,
            repo.get_task(request.meta.task_id)["submission"],
            json.loads(repo.artifacts.get(job.verification_plan)),
        )
    report = original
    report["ir"]["pipeline_id"] = "sroa-v1"
    with pytest.raises(ContractError, match="LLVM"):
        validate_evidence(
            report,
            result,
            job,
            repo.get_task(request.meta.task_id)["submission"],
            json.loads(repo.artifacts.get(job.verification_plan)),
        )


def test_ir_candidate_can_win_the_existing_cohort_and_resume(tmp_path):
    package = read_package(Path(__file__).parents[1] / "examples/sum-array")
    repo, runner = Repository(tmp_path / "state"), IRRunner(package)
    coordinator = CohortCoordinator(
        repo,
        runner,
        CohortBroker(repo),
        concurrency=1,
        backends={"performance": IRPassBackend("scalar-v1")},
    )
    report = coordinator.optimize(package, agents=["performance"])
    assert report["status"] == "selected", report
    (proposal,) = repo.list_proposals(coordinator.last_task_id)
    assert report["selected_source"]["sha256"] == proposal.source.bundle.sha256
    assert coordinator.run(coordinator.last_task_id) == report


def test_compiler_timeout_cannot_produce_proposal(generated):
    repo, runner, request, _, _, _ = generated
    runner.fail_stage = "optimize"
    # A second recipe reuses baseline stages, but cannot reuse its optimization stage.
    with pytest.raises(InfrastructureError, match="timeout"):
        IRPassAgent(repo, runner, "sroa-v1", "undefined").generate(request)
    assert repo.list_proposals(request.meta.task_id) == []


def test_unsupported_passes_rejected_before_compiler_work():
    with pytest.raises(ContractError, match="unsupported"):
        IRPassBackend("default<O3>")


def test_selected_ir_download_exports_bitcode_instead_of_extracting_source(generated, tmp_path):
    from unittest.mock import Mock

    from compiler_optimizer.client import OptimizerClient

    repo, _, request, proposal, _, _ = generated
    repo.save_proposal(proposal)
    client = OptimizerClient(Mock())
    client.status = Mock(
        return_value=pb.OptimizationResult(
            task_id=request.meta.task_id,
            status="completed",
            selected_source=proposal.source,
            report_json=json.dumps(
                {
                    "selected_representation": "llvm-ir-v1",
                    "selected_ir": json.loads(descriptor(proposal.ir)),
                }
            ),
        )
    )
    client.transfer.ListArtifacts = Mock(
        return_value=pb.TaskArtifacts(
            task_id=request.meta.task_id,
            artifacts=[
                pb.TaskArtifact(
                    artifact=pb.ArtifactRef(**{k: row[k] for k in ("key", "sha256", "size_bytes")}),
                    kind=row["kind"],
                )
                for row in repo.list_artifacts(request.meta.task_id)
            ],
        )
    )
    client.fetch = lambda task_id, ref, path: path.write_bytes(repo.artifacts.get(ref))
    output = tmp_path / "download"
    client.download(request.meta.task_id, output)
    assert (output / "selected-ir.json").read_bytes() == descriptor(proposal.ir)
    assert (output / "ir/normal-optimized.bc").read_bytes() == repo.artifacts.get(
        proposal.ir.variants[0].optimized_bitcode
    )
    assert not (output / "selected-source.zip").exists()


def test_ir_download_rejects_unsafe_profile_before_writing(generated, tmp_path):
    from compiler_optimizer.client import _write_selected_ir

    _, _, _, proposal, _, _ = generated
    proposal.ir.variants[0].build_profile = "../escaped"
    with pytest.raises(ContractError, match="LLVM descriptor"):
        _write_selected_ir(descriptor(proposal.ir), {}, {}, tmp_path, tmp_path)
    assert not (tmp_path / "selected-ir.json").exists()
