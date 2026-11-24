"""Evaluate a frozen cohort through RabbitMQ, then select by reviews and weighted measurements."""

import argparse
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.agents.architecture import ArchitectureAgent
from compiler_optimizer.agents.generation import generate_record, unpack_generation
from compiler_optimizer.agents.journal import AgentJournal
from compiler_optimizer.agents.memory import MemoryAgent
from compiler_optimizer.agents.performance import (
    FilePatchBackend,
    PerformanceAgent,
)
from compiler_optimizer.agents.rules import (
    ArchitectureRuleBackend,
    MemoryRuleBackend,
    PerformanceRuleBackend,
)
from compiler_optimizer.coordinator.selection import SelectionCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.proposal_queue import DeliveryError, RabbitProposalQueue
from compiler_optimizer.scheduler.recovery import RecoveryController
from compiler_optimizer.scheduler.resources import reservation_mib, runner_capacity
from compiler_optimizer.shared.agent_roles import generation_role, role_spec
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.c_syntax import TransformationAbstained
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
    pb.PERFORMANCE: (PerformanceAgent, PerformanceRuleBackend),
    pb.MEMORY: (MemoryAgent, MemoryRuleBackend),
    pb.ARCHITECTURE: (ArchitectureAgent, ArchitectureRuleBackend),
}


class FrozenPatchBackend:
    def __init__(self, artifacts, ref):
        self.artifacts, self.ref = artifacts, ref

    def generate(self, request, sources):
        return self.artifacts.get(self.ref)


class CohortCoordinator:
    def __init__(
        self,
        repository,
        runner,
        broker,
        *,
        backends=None,
        review_backends=None,
        recovery=None,
        concurrency=8,
        agent_client=None,
    ):
        self.repository, self.runner, self.broker = repository, runner, broker
        self.backends, self.review_backends = dict(backends or {}), dict(review_backends or {})
        require(
            set(self.backends) <= {"performance", "memory", "architecture"},
            "unknown generator backend",
        )
        require(set(self.review_backends) <= {"correctness", "critic"}, "unknown reviewer backend")
        if agent_client is not None:
            require(
                not self.review_backends, "RPC review adapters are configured by their services"
            )
            self.review_backends = {
                name: agent_client.reviewer(role)
                for name, role in (("correctness", pb.CORRECTNESS), ("critic", pb.CRITIC))
            }
        self.last_task_id = None
        self.agent_client = agent_client
        self.recovery = recovery
        require(type(concurrency) is int and 1 <= concurrency <= 8, "concurrency must be 1–8")
        self.concurrency = concurrency

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
            return self.resume_locked(
                task_id,
                agents=agents,
                policy=policy,
                verification_plan=verification_plan,
                benchmark_plan=benchmark_plan,
            )

    def resume_locked(
        self, task_id, *, agents=None, policy=None, verification_plan=None, benchmark_plan=None
    ):
        """Resume while the caller holds pipeline_lock across baseline and cohort work."""
        self.last_task_id = task_id
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
        names = self._candidate_names(names, policy)
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
        debate_ref = getattr(self, "_debate_policy_ref", None)
        if debate_ref is None:
            repository.begin_selection(task_id, policy_ref, members)
        else:
            repository.begin_selection(task_id, policy_ref, members, debate_policy_ref=debate_ref)

    def _candidate_names(self, names, policy):
        return names

    def _generation_request(self, task_id, member, request):
        return request

    def _run(self, task_id):
        repository = self.repository
        if repository.debate_run(task_id) is not None:
            from compiler_optimizer.coordinator.debate import DebateCoordinator

            return DebateCoordinator(
                repository,
                self.runner,
                self.broker,
                backends=self.backends,
                review_backends=None if self.agent_client else self.review_backends,
                recovery=self.recovery,
                agent_client=self.agent_client,
            )._run(task_id)
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
            work = repository.cohort_work(task_id)
            if work is not None:
                for member in work["members"]:
                    repository.checkpoint_cohort_member(task_id, member["proposal_id"], "completed")
                repository.checkpoint_cohort(task_id, "completed")
            return cached[1]
        session = repository.get_selection_session(task_id)
        manifest = repository.get_task(task_id)["submission"].manifest
        self._reservation_mib = reservation_mib(manifest)
        capacity = runner_capacity(self.runner)
        count = capacity.workers(self._reservation_mib, self.concurrency) if capacity else 1
        # Spare engine capacity must not split a single candidate's recovery
        # history between preparation and evaluation scopes.
        count = min(count, len(session["members"]))
        if self.recovery is not None:
            count = min(count, len(self.recovery.workers))
        work = repository.begin_cohort_work(task_id, count, self._reservation_mib)
        count = min(count, work["workers"])
        if self._cancellation_requested(task_id):
            return self._finish_cancelled(task_id)
        if work["workers"] > 1:
            with ThreadPoolExecutor(max_workers=count, thread_name_prefix="cohort") as executor:
                futures = [
                    executor.submit(self._preflight_member, task_id, m) for m in session["members"]
                ]
                for future in futures:
                    future.result()
        if self._cancellation_requested(task_id):
            return self._finish_cancelled(task_id)
        repository.checkpoint_cohort(task_id, "measuring")
        for member in session["members"]:
            if self._cancellation_requested(task_id):
                return self._finish_cancelled(task_id)
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
            proposal = self._generate_member(task_id, member)
            if proposal is None:
                continue
            if self._cancellation_requested(task_id):
                return self._finish_cancelled(task_id)
            self.broker.exchange(proposal, self.consume, lambda: None)
        if self.recovery is not None:
            self.recovery.flush_outbox(task_id, self.broker)
        if self._cancellation_requested(task_id):
            return self._finish_cancelled(task_id)
        try:
            result = SelectionCoordinator(repository).choose(task_id)
        except StateError:
            if self._cancellation_requested(task_id):
                return self._finish_cancelled(task_id)
            raise
        for member in session["members"]:
            repository.checkpoint_cohort_member(task_id, member["proposal_id"], "completed")
        repository.checkpoint_cohort(task_id, "completed")
        return result

    def _generate_member(self, task_id, member):
        repository = self.repository
        request = next(
            r for r in repository.list_requests(task_id) if r.proposal_id == member["proposal_id"]
        )
        request = self._generation_request(task_id, member, request)
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
            if self.agent_client is not None:
                require(
                    member["backend_kind"] != "custom",
                    "RPC generators require a frozen replay patch for custom proposals",
                )
                patch = (
                    pb.ArtifactRef(**member["backend_patch"]) if member["backend_patch"] else None
                )
                agent = self.agent_client.generator(request.role, patch)
            else:
                agent = agent_type(repository.artifacts, backend)
            window = (
                self.runner.work_window(memory_mib=self._reservation_mib)
                if hasattr(self.runner, "work_window")
                else nullcontext()
            )
            with window:
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
                "reason": "generation_abstained"
                if isinstance(error, TransformationAbstained)
                else "generation_failed",
                "message": str(error)[:4096],
            }
            ref = repository.artifacts.put(json.dumps(evidence, sort_keys=True).encode())
            repository.record_selection_progress(
                task_id, request.proposal_id, "generation_failed", ref
            )
            return None
        repository.save_proposal(proposal)
        repository.register_selection_patch(task_id, proposal.proposal_id, patch_ref)
        return proposal

    def _preflight_member(self, task_id, member):
        repository = self.repository
        if self._cancellation_requested(task_id):
            repository.checkpoint_cohort_member(task_id, member["proposal_id"], "cancelled")
            return
        if any(
            p["proposal_id"] == member["proposal_id"]
            for p in repository.selection_progress(task_id)
        ):
            return
        work = repository.cohort_work(task_id)
        state = next(
            m["state"] for m in work["members"] if m["proposal_id"] == member["proposal_id"]
        )
        if state in ("ready", "completed", "cancelled"):
            return
        repository.checkpoint_cohort_member(task_id, member["proposal_id"], "preparing")
        proposal = self._generate_member(task_id, member)
        if proposal is not None and not self._cancellation_requested(task_id):
            if self.recovery is not None:
                from compiler_optimizer.workers.execution import wait_for_execution

                wait_for_execution(
                    lambda: self.consume(proposal, verification_only=True), self.recovery, task_id
                )
            else:
                self.consume(proposal, verification_only=True)
        repository.checkpoint_cohort_member(task_id, member["proposal_id"], "ready")

    def _cancellation_requested(self, task_id):
        run = self.repository.cohort_work(task_id)
        return run is not None and run["state"] in ("cancelling", "cancelled")

    def _finish_cancelled(self, task_id):
        repository = self.repository
        for member in repository.cohort_work(task_id)["members"]:
            repository.checkpoint_cohort_member(task_id, member["proposal_id"], "cancelled")
        task = repository.get_task(task_id)
        if task["status"] == "running":
            repository.transition(
                task_id,
                expected_revision=task["revision"],
                status="failed",
                reason="cohort_cancelled",
            )
        repository.checkpoint_cohort(task_id, "cancelled")
        return {"task_id": task_id, "status": "cancelled", "reason": "cohort_cancelled"}

    def consume(self, proposal, *, verification_only=False):
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
        require(
            proposal.HasField("agent_provenance") == (self.agent_client is not None),
            "resume requires the original local/RPC agent transport",
        )
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
                agent_client=self.agent_client,
                verification_only=verification_only,
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
        if self._cancellation_requested(task_id):
            return {"task_id": task_id, "proposal_id": proposal.proposal_id, "status": "cancelled"}
        if verification_only:
            return {"task_id": task_id, "proposal_id": proposal.proposal_id, "status": "ready"}
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
    parser.add_argument("--debate-policy", help="Operator revision limits and held-out cases JSON")
    parser.add_argument("--patch", action="append", default=[], help="ROLE=structured-patch.json")
    parser.add_argument("--image")
    parser.add_argument(
        "--target", choices=tuple(DEFAULT_IMAGES), help="Defaults to the Docker engine's native CPU"
    )
    parser.add_argument("--recovery-policy")
    parser.add_argument(
        "--workers", type=int, default=8, help="Upper bound; capacity preserves headroom"
    )
    parser.add_argument(
        "--cancel", action="store_true", help="Cancel --task-id after active work drains"
    )
    args = parser.parse_args()
    coordinator = None
    try:
        backends = {}
        for option in args.patch:
            name, path = option.split("=", 1)
            require(name not in backends, "duplicate role patch")
            backends[name] = FilePatchBackend(path)
        repository = Repository(args.state)
        if args.cancel:
            require(args.task_id is not None, "--cancel requires --task-id")
            repository.cancel_cohort(args.task_id)
            print(json.dumps({"task_id": args.task_id, "status": "cancellation_requested"}))
            return 0
        if args.target is None:
            from compiler_optimizer.evaluation.__main__ import native_target

            args.target = native_target()
        from compiler_optimizer.agents.rpc_client import configured_client

        coordinator_type, debate_options = CohortCoordinator, {}
        if args.debate_policy:
            from compiler_optimizer.coordinator.debate import DebateCoordinator
            from compiler_optimizer.shared.debate import DebatePolicy

            coordinator_type = DebateCoordinator
            debate_options["debate_policy"] = DebatePolicy.parse(
                Path(args.debate_policy).read_bytes()
            )
        coordinator = coordinator_type(
            repository,
            DockerRunner(
                args.image or DEFAULT_IMAGES[args.target],
                target=args.target,
                watchdog_directory=Path(args.state) / "watchdogs",
            ),
            RabbitProposalQueue(Settings.from_env("coordinator")),
            backends=backends,
            agent_client=configured_client(repository),
            **debate_options,
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
