"""Candidate agent → RabbitMQ → verification/reviews → organized benchmarks → coordinator result."""

import argparse
import base64
import json
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.agents.architecture import ArchitectureAgent, ExampleArchitectureBackend
from compiler_optimizer.agents.generation import generate_record, unpack_generation
from compiler_optimizer.agents.journal import AgentJournal
from compiler_optimizer.agents.memory import ExampleMemoryBackend, MemoryAgent
from compiler_optimizer.agents.performance import (
    ExamplePerformanceBackend,
    FilePatchBackend,
    PerformanceAgent,
)
from compiler_optimizer.coordinator.selection import SelectionCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.proposal_queue import DeliveryError, RabbitProposalQueue
from compiler_optimizer.shared.agent_roles import generation_role, role_spec
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.messaging import encode_message
from compiler_optimizer.shared.patches import apply_patch
from compiler_optimizer.shared.recovery import RecoveryTerminal
from compiler_optimizer.shared.selection import SelectionPolicy
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import StorageError
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.baseline import metadata, run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import bundle, read_package, unbundle
from compiler_optimizer.workers.pipeline import evaluate_proposal
from compiler_optimizer.workers.verification_plan import VerificationPlan
from compiler_optimizer.workers.verify import _snapshot


class PipelineCoordinator:
    def __init__(
        self,
        repository,
        runner,
        broker,
        *,
        review_backends=None,
        selection_policy=None,
        recovery=None,
    ):
        self.repository, self.runner, self.broker = repository, runner, broker
        self.recovery = recovery
        self.last_task_id = None
        self.selection_policy = selection_policy
        if selection_policy is not None:
            selection_policy.encode()
        self.review_backends = dict(review_backends or {})
        require(
            set(self.review_backends) <= {"correctness", "critic"},
            "review backends are only supported for correctness and critic",
        )

    def run(
        self, task_id, backend=None, verification_plan=None, benchmark_plan=None, *, agent=None
    ):
        self.last_task_id = task_id
        with self.repository.pipeline_lock():
            return self._run(task_id, backend, verification_plan, benchmark_plan, agent)

    def optimize(
        self, package, backend=None, verification_plan=None, benchmark_plan=None, *, agent=None
    ):
        with self.repository.pipeline_lock():
            baseline = run_baseline(package, self.repository, self.runner)
            task_id = baseline.meta.task_id
            self.last_task_id = task_id
            if baseline.WhichOneof("outcome") != "success":
                return {
                    "schema_version": 1,
                    "task_id": task_id,
                    "status": "failed",
                    "reason": "baseline_rejected",
                    "failure": pb.FailureReason.Name(baseline.failure.reason),
                    "selected_source": None,
                }
            return self._run(task_id, backend, verification_plan, benchmark_plan, agent)

    def _run(self, task_id, backend, verification_plan, benchmark_plan, agent):
        repository = self.repository
        if self.recovery is not None:
            self.recovery.publish_quarantines(task_id)
        session = repository.get_selection_session(task_id)
        if self.selection_policy is not None and session is not None:
            require(
                repository.artifacts.get(pb.ArtifactRef(**session["policy"]))
                == self.selection_policy.encode(),
                "selection policy is frozen",
            )
        cached = repository.pipeline_report(task_id)
        if cached is not None:
            return cached[1]
        run = repository.get_pipeline(task_id)
        if run is None:
            role = generation_role(agent or "performance")
            require(
                role in (pb.PERFORMANCE, pb.MEMORY, pb.ARCHITECTURE),
                "generation role is not implemented",
            )
            benchmark_plan = benchmark_plan or (
                BenchmarkPlan(schema_version=2, objective="memory")
                if role == pb.MEMORY
                else BenchmarkPlan()
            )
            require(
                benchmark_plan.objective == role_spec(role).objective,
                "benchmark objective does not match generator role",
            )
            benchmark_plan.encode()
            (verification_plan or VerificationPlan()).encode()
            task, original, _ = _snapshot(repository, task_id)
            require(
                self.runner.compiler(original.manifest.language) == task["submission"].compiler,
                "pipeline requires the original compiler image/settings",
            )
            existing = {p.proposal_id for p in repository.list_proposals(task_id)}
            pending = [
                r
                for r in repository.list_requests(task_id)
                if r.role == role
                and r.proposal_id not in existing
                and r.source == task["submission"].source
            ]
            request = (
                pending[-1]
                if pending
                else pb.OptimizationRequest(
                    meta=metadata(task_id),
                    proposal_id=str(uuid4()),
                    role=role,
                    manifest=task["submission"].manifest,
                    source=task["submission"].source,
                    compiler=task["submission"].compiler,
                )
            )
            repository.save_request(request)
            agent_type, default_backend = {
                pb.PERFORMANCE: (PerformanceAgent, ExamplePerformanceBackend),
                pb.MEMORY: (MemoryAgent, ExampleMemoryBackend),
                pb.ARCHITECTURE: (ArchitectureAgent, ExampleArchitectureBackend),
            }[role]
            generator = agent_type(repository.artifacts, backend or default_backend())
            window = (
                self.runner.work_window(memory_mib=256)
                if hasattr(self.runner, "work_window")
                else nullcontext()
            )
            with window:
                record = AgentJournal(repository, role).run(
                    task_id,
                    request.proposal_id,
                    {"assignment_base64": base64.b64encode(encode_message(request)).decode()},
                    lambda: generate_record(generator, request),
                )
            proposal, patch_ref = unpack_generation(record, request)
            repository.enqueue_pipeline(
                proposal,
                patch_ref,
                repository.artifacts.put((verification_plan or VerificationPlan()).encode()),
                repository.artifacts.put(benchmark_plan.encode()),
                selection_policy_ref=repository.artifacts.put(
                    (self.selection_policy or SelectionPolicy()).encode()
                ),
            )
        else:
            require(
                backend is None
                and verification_plan is None
                and benchmark_plan is None
                and agent is None,
                "resume uses stored patch and plans; do not supply replacement options",
            )
            proposal = next(
                proposal
                for proposal in repository.list_proposals(task_id)
                if proposal.proposal_id == run["proposal_id"]
            )
        self._ensure_selection(proposal)
        # Persisted terminal selection may precede the report if the owner crashed during commit.
        if repository.get_task(task_id)["status"] in {"completed", "failed"}:
            return self.consume(proposal)
        return self.broker.exchange(
            proposal, self.consume, lambda: repository.pipeline_phase(task_id, "published")
        )

    def _ensure_selection(self, proposal):
        repository = self.repository
        config = repository.get_pipeline(proposal.meta.task_id)["configuration"]
        if config["schema_version"] >= 3:
            pref = pb.ArtifactRef(**config["selection_policy"])
            if self.selection_policy is not None:
                require(
                    repository.artifacts.get(pref) == self.selection_policy.encode(),
                    "selection policy is frozen",
                )
            repository.begin_selection(
                proposal.meta.task_id,
                pref,
                [
                    {
                        "proposal_id": proposal.proposal_id,
                        "role": proposal.role,
                        "verification_plan": config["verification_plan"],
                        "benchmark_plan": config["benchmark_plan"],
                    }
                ],
            )

    def consume(self, proposal):
        repository = self.repository
        task_id = proposal.meta.task_id
        cached = repository.pipeline_report(task_id)
        if cached is not None:
            return cached[1]
        run = repository.get_pipeline(task_id)
        require(
            run is not None and run["proposal_id"] == proposal.proposal_id,
            "unassigned pipeline delivery",
        )
        stored = next(
            value
            for value in repository.list_proposals(task_id)
            if value.proposal_id == proposal.proposal_id
        )
        require(encode_message(proposal) == encode_message(stored), "pipeline proposal was altered")
        self._ensure_selection(proposal)
        if self.recovery is not None:
            self.recovery.publish_quarantines(task_id)
        submission = repository.get_task(task_id)["submission"]
        patch, changed = apply_patch(
            repository.artifacts.get(pb.ArtifactRef(**run["configuration"]["patch"])),
            unbundle(repository.artifacts.get(submission.source.bundle)),
            submission.source.bundle.sha256,
        )
        require(
            bundle(changed) == repository.artifacts.get(proposal.source.bundle)
            and patch["rationale"] == proposal.rationale
            and patch["transformation"] == proposal.transformation,
            "stored patch and proposal differ",
        )
        try:
            verified, benchmark, reviews, work = evaluate_proposal(
                repository,
                task_id,
                proposal.proposal_id,
                self.runner,
                review_backends=self.review_backends,
                recovery=self.recovery,
            )
        except RecoveryTerminal as error:
            return self._finish_terminal(proposal, run, error.receipt)
        baselines = [
            result
            for result in repository.list_results(task_id)
            if result.WhichOneof("subject") == "baseline"
            and result.WhichOneof("outcome") == "success"
        ]
        reason = "correctness_rejected"
        selected = baselines[-1]
        if verified.WhichOneof("outcome") == "success":
            reason = "review_rejected" if benchmark is None else "benchmark_retained_baseline"
            if benchmark is not None and benchmark["analysis"]["qualified"]:
                selected, reason = (
                    verified,
                    (
                        "reliable_memory_gain"
                        if proposal.role == pb.MEMORY
                        else "reliable_performance_gain"
                    ),
                )
        task = repository.get_task(task_id)
        selection = None
        if repository.get_selection_session(task_id) is not None:
            if repository.selection_report(task_id) is None:
                marker = {
                    "task_id": task_id,
                    "proposal_id": proposal.proposal_id,
                    "verification_job_id": verified.job_id,
                    "verification_attempt": verified.attempt,
                }
                repository.record_selection_progress(
                    task_id,
                    proposal.proposal_id,
                    "evaluated",
                    repository.artifacts.put(json.dumps(marker, sort_keys=True).encode()),
                )
            selection = SelectionCoordinator(repository).choose(task_id)
            reason = selection["reason"]
            selected = next(
                (
                    r
                    for r in repository.list_results(task_id)
                    if r.job_id == selection["selected_job_id"]
                    and r.attempt == selection["selected_attempt"]
                ),
                None,
            )
        elif task["status"] == "running":
            repository.select_result(
                task_id,
                selected.job_id,
                selected.attempt,
                expected_revision=task["revision"],
                reason=reason,
            )
        decision = repository.get_decision(task_id)
        if decision is None:
            selected = None
            reason = "baseline_invalidated"
        else:
            require(decision["job_id"] == selected.job_id, "pipeline decision changed")
            reason = decision["reason"]
        report = {
            "schema_version": run["configuration"]["schema_version"],
            "task_id": task_id,
            "proposal_id": proposal.proposal_id,
            "status": repository.get_task(task_id)["status"],
            "reason": reason,
            "agent": role_spec(proposal.role).name,
            "rationale": proposal.rationale,
            "transformation": proposal.transformation,
            "patch": run["configuration"]["patch"],
            "selected_job_id": None if selected is None else selected.job_id,
            "selected_source": None if selected is None else reference(selected.source.bundle),
            "candidate_selected": selected is not None and selected.job_id == verified.job_id,
            "verification_job_id": verified.job_id,
            "verification": "passed" if verified.WhichOneof("outcome") == "success" else "rejected",
            "correctness_report": reference(verified.correctness_report),
            "failure": None
            if verified.WhichOneof("outcome") == "success"
            else {
                "reason": pb.FailureReason.Name(verified.failure.reason),
                "message": verified.failure.message,
            },
            "benchmark": benchmark,
            "reviews": reviews,
            "benchmark_work": work,
            "selection": selection,
        }
        ref = repository.artifacts.put(json.dumps(report, sort_keys=True, allow_nan=False).encode())
        repository.finish_pipeline(task_id, ref)
        return report

    def _finish_terminal(self, proposal, run, receipt):
        repository = self.repository
        task_id = proposal.meta.task_id
        selection = None
        if repository.get_selection_session(task_id) is not None:
            evidence = repository.artifacts.put(json.dumps(receipt, sort_keys=True).encode())
            repository.record_selection_progress(
                task_id, proposal.proposal_id, "assessment_failed", evidence
            )
            selection = SelectionCoordinator(repository).choose(task_id)
        elif repository.get_task(task_id)["status"] == "running":
            baselines = [
                r
                for r in repository.list_results(task_id)
                if r.WhichOneof("subject") == "baseline" and r.WhichOneof("outcome") == "success"
            ]
            baseline = baselines[-1]
            repository.select_result(
                task_id,
                baseline.job_id,
                baseline.attempt,
                expected_revision=repository.get_task(task_id)["revision"],
                reason="recovery_terminal",
            )
        decision = repository.get_decision(task_id)
        selected = next(
            (
                r
                for r in repository.list_results(task_id)
                if decision and r.job_id == decision["job_id"] and r.attempt == decision["attempt"]
            ),
            None,
        )
        report = {
            "schema_version": run["configuration"]["schema_version"],
            "task_id": task_id,
            "proposal_id": proposal.proposal_id,
            "status": repository.get_task(task_id)["status"],
            "reason": decision["reason"] if decision else "baseline_invalidated",
            "agent": role_spec(proposal.role).name,
            "rationale": proposal.rationale,
            "transformation": proposal.transformation,
            "patch": run["configuration"]["patch"],
            "selected_job_id": selected.job_id if selected else None,
            "selected_source": reference(selected.source.bundle) if selected else None,
            "candidate_selected": False,
            "verification_job_id": None,
            "verification": "recovery_terminal",
            "correctness_report": None,
            "failure": {"reason": "recovery_terminal", "message": receipt["reason"]},
            "benchmark": None,
            "reviews": {},
            "benchmark_work": None,
            "selection": selection,
            "recovery": receipt,
        }
        repository.finish_pipeline(
            task_id, repository.artifacts.put(json.dumps(report, sort_keys=True).encode())
        )
        self.recovery.publish_quarantines(task_id)
        return self.recovery.deadletter_result(receipt, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--package", help="Validate and optimize a new local submission")
    source.add_argument("--task-id", help="Continue a passing baseline or resume a stored pipeline")
    parser.add_argument("--state", default="var/pipeline")
    parser.add_argument("--image", default="cmpe273-clang:18")
    parser.add_argument(
        "--patch", help="Replay structured model/operator patch instead of starter rule"
    )
    parser.add_argument("--verification-plan")
    parser.add_argument("--benchmark-plan")
    parser.add_argument("--agent", choices=("performance", "memory", "architecture"))
    parser.add_argument("--selection-policy")
    args = parser.parse_args()
    coordinator = None
    try:
        repository = Repository(args.state)
        runner = DockerRunner(args.image, watchdog_directory=Path(args.state) / "watchdogs")
        from compiler_optimizer.workers.execution import local_recovery

        coordinator = PipelineCoordinator(
            repository,
            runner,
            RabbitProposalQueue(Settings.from_env("coordinator")),
            recovery=local_recovery(repository, runner),
            selection_policy=SelectionPolicy.parse(Path(args.selection_policy).read_bytes())
            if args.selection_policy
            else None,
        )
        backend = FilePatchBackend(args.patch) if args.patch else None
        verification = (
            VerificationPlan.parse(Path(args.verification_plan).read_bytes())
            if args.verification_plan
            else None
        )
        benchmark = (
            BenchmarkPlan.parse(Path(args.benchmark_plan).read_bytes())
            if args.benchmark_plan
            else None
        )
        report = (
            coordinator.optimize(
                read_package(args.package), backend, verification, benchmark, agent=args.agent
            )
            if args.package
            else coordinator.run(args.task_id, backend, verification, benchmark, agent=args.agent)
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "completed" else 1
    except (
        ContractError,
        RecoveryTerminal,
        StorageError,
        InfrastructureError,
        DeliveryError,
        OSError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {
                    "error": str(error),
                    "task_id": None if coordinator is None else coordinator.last_task_id,
                    "state": str(Path(args.state).absolute()),
                    "resume": "Use --task-id with the task listed in this state directory",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
