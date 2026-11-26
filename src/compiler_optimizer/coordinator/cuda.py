"""Restartable CUDA source proposals and bounded PTX jobs; never execute submitted code."""

from uuid import UUID, uuid5

from compiler_optimizer.agents.generation import generate_record, unpack_generation
from compiler_optimizer.agents.journal import AgentJournal
from compiler_optimizer.coordinator.round import GENERATORS
from compiler_optimizer.shared.agent_roles import generation_role
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import StateError
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.cuda_compile import compile_artifacts
from compiler_optimizer.workers.package import unbundle


def run_compile_task(repository, submission, runner, agent_client=None, agents=()):
    task_id = submission.meta.task_id
    if repository.cuda_task_report(task_id):
        return
    task = repository.get_task(task_id)
    if task["status"] == "accepted":
        repository.transition(
            task_id,
            expected_revision=task["revision"],
            status="running",
            reason="CUDA compile-only work started",
        )
    require(runner.compiler("cuda17") == submission.compiler, "CUDA build profile changed")

    def compile_source(owner, source):
        cached = repository.cuda_record(task_id, owner)
        if cached:
            return cached[1]
        files = unbundle(repository.artifacts.get(source.bundle))
        require(set(files) == set(submission.manifest.sources), "CUDA requires one source file")
        report = compile_artifacts(
            runner,
            files[submission.manifest.sources[0]],
            repository.artifacts,
            limits=submission.manifest.limits,
        )
        report.update(
            source_version=source.version,
            source_bundle=reference(source.bundle),
            parent_source_sha256=source.parent_source_sha256,
        )
        refs = [
            (kind, pb.ArtifactRef(**report[key]))
            for key, kind in [
                ("source", "cuda-source"),
                ("log", "cuda-diagnostics"),
                ("ptx", "cuda-ptx"),
                ("report", "cuda-compiler-report"),
            ]
            if report[key] is not None
        ]
        repository.save_cuda_record(task_id, owner, report, refs)
        return report

    baseline = compile_source("baseline", submission.source)
    candidates = []
    if baseline["status"] == "compiled":
        for name in agents:
            proposal_id = str(uuid5(UUID(task_id), "cuda-" + name))
            cached = repository.cuda_record(task_id, proposal_id)
            if cached:
                candidates.append(cached[1])
                continue
            request = next(
                (r for r in repository.list_requests(task_id) if r.proposal_id == proposal_id), None
            )
            if request is None:
                request = pb.OptimizationRequest(
                    meta=metadata(task_id),
                    proposal_id=proposal_id,
                    role=generation_role(name),
                    manifest=submission.manifest,
                    source=submission.source,
                    compiler=submission.compiler,
                )
                repository.save_request(request)
            proposal = next(
                (p for p in repository.list_proposals(task_id) if p.proposal_id == proposal_id),
                None,
            )
            if proposal is None:
                agent_type, backend = GENERATORS[request.role]
                agent = (
                    agent_client.generator(request.role)
                    if agent_client
                    else agent_type(repository.artifacts, backend())
                )
                try:
                    record = AgentJournal(repository, request.role).run(
                        task_id,
                        proposal_id,
                        {"request": request.SerializeToString().hex()},
                        lambda agent=agent, request=request: generate_record(agent, request),
                    )
                    proposal, patch = unpack_generation(record, request)
                    # Preserve source edits alongside the immutable source-version relationship.
                    with repository._transaction() as db:
                        repository._artifact(db, task_id, patch, "cuda-patch", owner=proposal_id)
                    repository.save_proposal(proposal)
                except (ContractError, ValueError, StateError) as error:
                    report = {
                        "proposal_id": proposal_id,
                        "role": name,
                        "status": "generation_failed",
                        "error_class": type(error).__name__,
                        "performance_qualified": False,
                    }
                    repository.save_cuda_record(task_id, proposal_id, report)
                    candidates.append(report)
                    continue
            candidates.append(compile_source(proposal_id, proposal.source))
    report = {
        "schema_version": 1,
        "kind": "cuda-ptx-task",
        "task_id": task_id,
        "status": baseline["status"],
        "baseline": baseline,
        "candidates": candidates,
        "execution_enabled": False,
        "correctness_verified": False,
        "performance_qualified": False,
        "selection": "unavailable_compile_only",
    }
    repository.save_cuda_record(task_id, "task", report, final=True)
