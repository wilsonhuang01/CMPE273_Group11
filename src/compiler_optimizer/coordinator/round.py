"""Evaluate a frozen cohort through RabbitMQ, then select by reviews and weighted measurements."""

import argparse
import base64
import json
from dataclasses import replace
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
from compiler_optimizer.scheduler.recovery import RecoveryController
from compiler_optimizer.shared.agent_roles import generation_role, role_spec
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.messaging import encode_message
from compiler_optimizer.shared.recovery import RecoveryPolicy, RecoveryTerminal
from compiler_optimizer.shared.selection import SelectionPolicy
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import StateError, StorageError
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.baseline import metadata, run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.pipeline import evaluate_proposal
from compiler_optimizer.workers.targets import DEFAULT_IMAGES
from compiler_optimizer.workers.verification_plan import VerificationPlan
from compiler_optimizer.workers.verify import _snapshot

GENERATORS = {
    pb.PERFORMANCE: (PerformanceAgent, ExamplePerformanceBackend),
    pb.MEMORY: (MemoryAgent, ExampleMemoryBackend),
    pb.ARCHITECTURE: (ArchitectureAgent, ExampleArchitectureBackend),
}


class FrozenPatchBackend:
    def __init__(self, artifacts, ref):
        self.artifacts, self.ref = artifacts, ref

    def generate(self, request, sources):
        return self.artifacts.get(self.ref)


class CohortCoordinator:
    def __init__(
        self, repository, runner, broker, *, backends=None, review_backends=None, recovery=None
    ):
        self.repository, self.runner, self.broker = repository, runner, broker
        self.backends, self.review_backends = dict(backends or {}), dict(review_backends or {})
        require(
            set(self.backends) <= {"performance", "memory", "architecture"},
            "unknown generator backend",
        )
        require(set(self.review_backends) <= {"correctness", "critic"}, "unknown reviewer backend")
        self.last_task_id = None
        self.recovery = recovery

    def optimize(
        self, package, *, agents=None, policy=None, verification_plan=None, benchmark_plan=None
    ):
        with self.repository.pipeline_lock():
            baseline = run_baseline(package, self.repository, self.runner)
            self.last_task_id = baseline.meta.task_id
            if baseline.WhichOneof("outcome") != "success":
                return {
                    "task_id": self.last_task_id,
                    "status": "blocked",
                    "reason": "baseline_rejected",
                }
            self._prepare(self.last_task_id, agents, policy, verification_plan, benchmark_plan)
            return self._run(self.last_task_id)

    def run(
        self, task_id, *, agents=None, policy=None, verification_plan=None, benchmark_plan=None
    ):
        self.last_task_id = task_id
        with self.repository.pipeline_lock():
            session = self.repository.get_selection_session(task_id)
            if session is None:
                self._prepare(task_id, agents, policy, verification_plan, benchmark_plan)
            else:
                require(
                    agents is None
                    and policy is None
                    and verification_plan is None
                    and benchmark_plan is None,
                    "resume uses the frozen cohort and policies",
                )
            return self._run(task_id)

    def _prepare(self, task_id, agents, policy, verification_plan, benchmark_plan):
        repository = self.repository
        policy = policy or SelectionPolicy()
        policy_ref = repository.artifacts.put(policy.encode())
        names = list(agents if agents is not None else ("performance", "memory", "architecture"))
        require(
            1 <= len(names) <= policy.maximum_candidates and len(names) == len(set(names)),
            "cohort needs distinct generating roles within its candidate limit",
        )
        task, original, _ = _snapshot(repository, task_id)
        require(
            self.runner.compiler(original.manifest.language) == task["submission"].compiler,
            "cohort requires the original compiler image/settings",
        )
        vref = repository.artifacts.put((verification_plan or VerificationPlan()).encode())
        base_plan = benchmark_plan or BenchmarkPlan()
        base_plan.encode()
        members = []
        for name in names:
            role = generation_role(name)
            request = pb.OptimizationRequest(
                meta=metadata(task_id),
                proposal_id=str(uuid4()),
                role=role,
                manifest=task["submission"].manifest,
                source=task["submission"].source,
                compiler=task["submission"].compiler,
            )
            repository.save_request(request)
            plan = replace(
                base_plan,
                schema_version=2 if role == pb.MEMORY else base_plan.schema_version,
                objective=role_spec(role).objective,
            )
            bref = repository.artifacts.put(plan.encode())
            backend = self.backends.get(name)
            patch_ref = None
            kind = "starter" if backend is None else "custom"
            if isinstance(backend, FilePatchBackend):
                patch_ref = repository.artifacts.put(backend.generate(request, {}))
                kind = "patch"
            members.append(
                {
                    "proposal_id": request.proposal_id,
                    "role": role,
                    "verification_plan": reference(vref),
                    "benchmark_plan": reference(bref),
                    "backend_kind": kind,
                    "backend_patch": None if patch_ref is None else reference(patch_ref),
                }
            )
        repository.begin_selection(task_id, policy_ref, members)

    def _run(self, task_id):
        repository = self.repository
        if self.recovery is not None:
            self.recovery.flush_outbox(task_id, self.broker)
        else:
            with repository._transaction(write=False) as db:
                require(
                    db.execute("SELECT 1 FROM recovery_jobs WHERE task_id=?", (task_id,)).fetchone()
                    is None,
                    "resume this task with its recovery controller",
                )
        cached = repository.selection_report(task_id)
        if cached is not None:
            return cached[1]
        session = repository.get_selection_session(task_id)
        for member in session["members"]:
            if repository.get_task(task_id)["status"] == "failed":
                break
            progress = {p["proposal_id"]: p for p in repository.selection_progress(task_id)}
            if member["proposal_id"] in progress:
                if self.recovery is not None and repository.get_recovery_job(
                    task_id, member["proposal_id"]
                ):
                    # A previous owner may have committed progress and lost the ack.
                    # Settle that scoped delivery using the durable cached result.
                    proposal = next(
                        p
                        for p in repository.list_proposals(task_id)
                        if p.proposal_id == member["proposal_id"]
                    )
                    self.broker.exchange(proposal, self.consume, lambda: None)
                continue
            request = next(
                r
                for r in repository.list_requests(task_id)
                if r.proposal_id == member["proposal_id"]
            )
            agent_type, starter = GENERATORS[request.role]
            name = role_spec(request.role).name
            backend = self.backends.get(name)
            if member["backend_patch"] is not None:
                backend = FrozenPatchBackend(
                    repository.artifacts, pb.ArtifactRef(**member["backend_patch"])
                )
            elif member["backend_kind"] == "custom":
                require(backend is not None, "resume requires the original custom backend adapter")
            backend = backend or starter()
            try:
                agent = agent_type(repository.artifacts, backend)
                record = AgentJournal(repository, request.role).run(
                    task_id,
                    request.proposal_id,
                    {"assignment_base64": base64.b64encode(encode_message(request)).decode()},
                    lambda agent=agent, request=request: generate_record(agent, request),
                )
                proposal, patch_ref = unpack_generation(record, request)
            except (ContractError, ValueError, StateError) as error:
                evidence = {
                    "task_id": task_id,
                    "proposal_id": request.proposal_id,
                    "reason": "generation_failed",
                    "message": str(error)[:4096],
                }
                ref = repository.artifacts.put(json.dumps(evidence, sort_keys=True).encode())
                repository.record_selection_progress(
                    task_id, request.proposal_id, "generation_failed", ref
                )
                continue
            repository.save_proposal(proposal)
            repository.register_selection_patch(task_id, proposal.proposal_id, patch_ref)
            self.broker.exchange(proposal, self.consume, lambda: None)
        if self.recovery is not None:
            self.recovery.flush_outbox(task_id, self.broker)
        return SelectionCoordinator(repository).choose(task_id)

    def consume(self, proposal):
        repository = self.repository
        task_id = proposal.meta.task_id
        member = next(
            (
                m
                for m in repository.get_selection_session(task_id)["members"]
                if m["proposal_id"] == proposal.proposal_id
            ),
            None,
        )
        require(member is not None, "unassigned cohort delivery")
        stored = next(
            p for p in repository.list_proposals(task_id) if p.proposal_id == proposal.proposal_id
        )
        require(encode_message(stored) == encode_message(proposal), "cohort proposal was altered")
        progress = next(
            (
                p
                for p in repository.selection_progress(task_id)
                if p["proposal_id"] == proposal.proposal_id
            ),
            None,
        )
        if progress is not None:
            from compiler_optimizer.monitoring.events import emit

            emit(
                "delivery_duplicate",
                task_id=task_id,
                proposal_id=proposal.proposal_id,
                status=progress["status"],
                duplicate=True,
            )
            return {
                "task_id": task_id,
                "proposal_id": proposal.proposal_id,
                "status": progress["status"],
            }
        config = {
            "schema_version": 2,
            "required_reviews": ["correctness", "critic"],
            "verification_plan": member["verification_plan"],
            "benchmark_plan": member["benchmark_plan"],
        }
        try:
            verified, benchmark, reviews, work = evaluate_proposal(
                repository,
                task_id,
                proposal.proposal_id,
                self.runner,
                review_backends=self.review_backends,
                configuration=config,
                recovery=self.recovery,
            )
        except RecoveryTerminal as error:
            evidence = {
                "task_id": task_id,
                "proposal_id": proposal.proposal_id,
                "reason": "recovery_terminal",
                "recovery": error.receipt,
            }
            ref = repository.artifacts.put(json.dumps(evidence, sort_keys=True).encode())
            repository.record_selection_progress(
                task_id, proposal.proposal_id, "assessment_failed", ref
            )
            return self.recovery.deadletter_result(error.receipt, evidence)
        except (ContractError, StateError) as error:
            evidence = {
                "task_id": task_id,
                "proposal_id": proposal.proposal_id,
                "reason": "assessment_failed",
                "message": str(error)[:4096],
            }
            ref = repository.artifacts.put(json.dumps(evidence, sort_keys=True).encode())
            repository.record_selection_progress(
                task_id, proposal.proposal_id, "assessment_failed", ref
            )
            return evidence
        evidence = {
            "schema_version": 1,
            "task_id": task_id,
            "proposal_id": proposal.proposal_id,
            "verification_job_id": verified.job_id,
            "verification_attempt": verified.attempt,
            "correctness_report": reference(verified.correctness_report),
            "reviews": reviews,
            "benchmark": benchmark,
            "benchmark_work": work,
            "recovery": None
            if self.recovery is None
            else {
                "job": repository.get_recovery_job(task_id, proposal.proposal_id),
                "attempts": repository.recovery_attempts(task_id, proposal.proposal_id),
            },
        }
        ref = repository.artifacts.put(
            json.dumps(evidence, sort_keys=True, allow_nan=False).encode()
        )
        repository.record_selection_progress(task_id, proposal.proposal_id, "evaluated", ref)
        return evidence


def main():
    from compiler_optimizer.monitoring.events import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--package")
    source.add_argument("--task-id")
    parser.add_argument("--state", default="var/cohort")
    parser.add_argument("--agents", help="Comma-separated roles; defaults to all three generators")
    parser.add_argument("--policy")
    parser.add_argument("--verification-plan")
    parser.add_argument("--benchmark-plan")
    parser.add_argument("--patch", action="append", default=[], help="ROLE=structured-patch.json")
    parser.add_argument("--image")
    parser.add_argument("--target", choices=tuple(DEFAULT_IMAGES), default="linux-x86_64")
    parser.add_argument("--recovery-policy")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    coordinator = None
    try:
        backends = {}
        for option in args.patch:
            name, path = option.split("=", 1)
            require(name not in backends, "duplicate role patch")
            backends[name] = FilePatchBackend(path)
        repository = Repository(args.state)
        coordinator = CohortCoordinator(
            repository,
            DockerRunner(
                args.image or DEFAULT_IMAGES[args.target],
                target=args.target,
                watchdog_directory=Path(args.state) / "watchdogs",
            ),
            RabbitProposalQueue(Settings.from_env("coordinator")),
            backends=backends,
        )
        require(1 <= args.workers <= 8, "worker pool size must be 1–8")
        coordinator.recovery = RecoveryController.local(
            repository,
            coordinator.runner,
            Settings.from_env("coordinator"),
            worker_count=args.workers,
            policy=RecoveryPolicy.parse(Path(args.recovery_policy).read_bytes())
            if args.recovery_policy
            else None,
        )
        options = {
            "agents": args.agents.split(",") if args.agents else None,
            "policy": SelectionPolicy.parse(Path(args.policy).read_bytes())
            if args.policy
            else None,
            "verification_plan": VerificationPlan.parse(Path(args.verification_plan).read_bytes())
            if args.verification_plan
            else None,
            "benchmark_plan": BenchmarkPlan.parse(Path(args.benchmark_plan).read_bytes())
            if args.benchmark_plan
            else None,
        }
        report = (
            coordinator.optimize(read_package(args.package), **options)
            if args.package
            else coordinator.run(args.task_id, **options)
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] in ("selected", "retained_baseline") else 1
    except (
        ContractError,
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
                    "task_id": coordinator.last_task_id if coordinator else None,
                    "state": str(Path(args.state).absolute()),
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
