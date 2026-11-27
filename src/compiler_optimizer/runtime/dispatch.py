"""Durable submission orchestration with local or independent sandbox executors.

Each dispatcher executes one task at a time under a database claim (see
``storage/execution.py``). Coordinators on several hosts can share one PostgreSQL
database: a standby takes over a task when its owner's claim expires, and the previous
owner's writes are rejected from then on.
"""

import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import replace
from uuid import uuid4

from compiler_optimizer.agents.rpc_client import configured_client
from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.monitoring.events import emit
from compiler_optimizer.monitoring.watchdog import read_json, remove_owned
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.scheduler.blacklist import RedisBlacklist
from compiler_optimizer.scheduler.recovery import RecoveryController, Worker
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.lease_context import DISPATCH_CLAIM
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.recovery import RecoveryPolicy
from compiler_optimizer.shared.selection import SelectionPolicy
from compiler_optimizer.storage.errors import ArtifactError, ClaimLost, StateError
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import package_from_files, unbundle
from compiler_optimizer.workers.remote import RemoteRunner, matching_workers
from compiler_optimizer.workers.targets import DEFAULT_IMAGES
from compiler_optimizer.workers.verification_plan import VerificationPlan


class SubmissionDispatcher:
    def __init__(
        self,
        repository,
        settings,
        *,
        runner_factory=None,
        broker_factory=None,
        recovery_factory=None,
    ):
        self.repository, self.settings = repository, settings
        self.owner_id = f"{socket.gethostname()[:64]}:{os.getpid()}:{uuid4().hex[:12]}"
        self.lease_ms = settings.dispatch_lease_seconds * 1000
        self.runner_factory = runner_factory or self._runner
        self.broker_factory = broker_factory or (lambda: RabbitProposalQueue(settings))
        self.recovery_factory = recovery_factory or self._recovery

    def _recovery(self, runner, policy):
        if isinstance(runner, RemoteRunner):
            rows = matching_workers(
                self.repository, self.repository.get_task(runner.task_id)["submission"].compiler
            )
            if not rows:
                raise InfrastructureError("no healthy remote recovery workers")
            known = {row["worker_id"]: row for row in self.repository.recovery_workers()}
            workers = [
                Worker(
                    runner=RemoteRunner(self.repository, runner.task_id, row),
                    worker_id=row["worker_id"],
                    incarnation_id=row["incarnation_id"],
                    sequence=known.get(row["worker_id"], {}).get("sequence", 0),
                )
                for row in rows[:8]
            ]
            return RecoveryController(
                self.repository, workers, RedisBlacklist.from_settings(self.settings), policy=policy
            )
        return RecoveryController.local(
            self.repository,
            runner,
            self.settings,
            policy=policy,
            watchdog_directory=runner.watchdog_directory,
        )

    def _runner(self, submission):
        if submission.manifest.language == "cuda17":
            from compiler_optimizer.workers.cuda_compile import CUDACompileRunner

            require(
                self.settings.execution_backend == "local",
                "CUDA compile jobs require a local executor",
            )
            runner = CUDACompileRunner(watchdog_directory=self.watchdogs(submission.meta.task_id))
            require(
                runner.compiler("cuda17") == submission.compiler, "CUDA compiler profile changed"
            )
            return runner
        if self.settings.execution_backend == "remote":
            rows = matching_workers(self.repository, submission.compiler)
            if not rows:
                raise InfrastructureError("no healthy remote compiler worker")
            return RemoteRunner(self.repository, submission.meta.task_id, rows[0])
        # A client cannot select an arbitrary installed image for trusted execution.
        runner = DockerRunner(
            DEFAULT_IMAGES[submission.compiler.target],
            target=submission.compiler.target,
            watchdog_directory=self.watchdogs(submission.meta.task_id),
        )
        require(
            runner.compiler(submission.manifest.language) == submission.compiler,
            "submission must match the installed trusted compiler profile",
        )
        return runner

    def watchdogs(self, task_id):
        return self.repository.root / "submission-watchdogs" / task_id

    def _cleanup(self, task_id):
        """Confirm removal of a previous owner's sandboxes before resuming its work."""
        if self.settings.execution_backend == "remote":
            deadline = time.monotonic() + 5
            while not self.repository.cancel_fleet_task(task_id):
                if time.monotonic() >= deadline:
                    raise InfrastructureError("remote sandbox cleanup is unconfirmed")
                time.sleep(0.1)
            return
        for path in self.watchdogs(task_id).glob("*/lease.json"):
            status = path.with_name("status.json")
            if status.exists() and read_json(status).get("state") == "removed":
                continue
            lease = read_json(path)
            require(
                re.fullmatch(r"compiler-baseline-[0-9a-f]{32}", lease["container"]) is not None,
                "invalid stored watchdog container",
            )
            # The dispatch claim ensures that no other coordinator owns this task's work.
            path.with_name("stop.json").write_text(json.dumps({"reason": "dispatcher_resume"}))
            if remove_owned(lease) != "removed":
                raise InfrastructureError("previous sandbox cleanup unconfirmed")

    def run_once(self, stop=None):
        """Claim and process one due task. A crash leaves the task claimable after its lease."""
        repo = self.repository
        try:
            with repo.pipeline_lock(shared=True):
                if stop is not None and stop.is_set():
                    return False
                claim = repo.claim_execution(
                    self.owner_id,
                    backend=self.settings.execution_backend,
                    lease_ms=self.lease_ms,
                    max_active=self.settings.dispatch_concurrency,
                )
                if claim is None:
                    return False
                self._claimed(claim)
                done, task_stop = threading.Event(), threading.Event()
                renewer = threading.Thread(
                    target=self._renew, args=(claim, stop, done, task_stop), daemon=True
                )
                renewer.start()
                authority = DISPATCH_CLAIM.set(claim)
                try:
                    self._dispatch(claim["task_id"], task_stop)
                except ClaimLost:
                    self._event("dispatch_claim_lost", claim)
                finally:
                    DISPATCH_CLAIM.reset(authority)
                    done.set()
                    renewer.join()
                    try:
                        repo.release_execution_claim(claim)  # A no-op once taken over.
                    except Exception as error:  # The lease still expires on its own.
                        self._event(
                            "dispatch_release_failed", claim, error_class=type(error).__name__
                        )
                return True
        except StateError as error:
            if str(error) != "another pipeline owns this state directory":
                raise
            return False

    def _dispatch(self, task_id, stop):
        repo = self.repository
        task = repo.get_task(task_id)
        if task["status"] in {"completed", "failed"}:
            repo.finish_execution(task_id)
            return
        if not repo.start_execution(task_id):
            repo.finish_execution(task_id, error_code="execution_attempts_exhausted")
            return
        try:
            self._execute(task_id, stop)
        except ClaimLost:
            raise  # The new owner records the outcome; this coordinator may not write.
        except (ContractError, ArtifactError) as error:
            repo.finish_execution(task_id, error_code=type(error).__name__)
        except Exception as error:
            # Transport, daemon and interrupted work retry under a durable total cap.
            # Store only the class, never connection strings or credentials.
            repo.finish_execution(task_id, error_code=type(error).__name__, retry=True)

    def _renew(self, claim, stop, done, task_stop):
        """Keep the claim alive; stop the task's work on shutdown or once the claim is lost."""
        interval = self.lease_ms / 3000
        due = time.monotonic() + interval
        while not done.wait(min(0.5, interval)):
            if stop is not None and stop.is_set():
                task_stop.set()
            if time.monotonic() < due:
                continue
            due = time.monotonic() + interval
            try:
                renewed = self.repository.renew_execution_claim(claim, self.lease_ms)
            except Exception as error:  # Retry; writes are fenced if the lease runs out.
                self._event("dispatch_renew_failed", claim, error_class=type(error).__name__)
                continue
            if not renewed:
                task_stop.set()
                self._event("dispatch_claim_lost", claim)
                return

    def _claimed(self, claim):
        if claim["previous_owner"] is not None:
            self._event("dispatch_takeover", claim, previous_owner=claim["previous_owner"])
        else:
            self._event("dispatch_claimed", claim)

    def _event(self, event, claim, **fields):
        emit(
            event,
            task_id=claim["task_id"],
            owner_id=self.owner_id,
            fence=claim["fence"],
            **fields,
        )

    def _execute(self, task_id, stop):
        repo = self.repository
        config = repo.get_execution(task_id)["configuration"]
        self._cleanup(task_id)
        submission = repo.get_task(task_id)["submission"]
        package = package_from_files(unbundle(repo.artifacts.get(submission.package)))
        runner = self.runner_factory(submission)
        if submission.manifest.language == "cuda17":
            from compiler_optimizer.coordinator.cuda import run_compile_task

            repo.checkpoint_execution(task_id, "compiling")
            run_compile_task(
                repo,
                submission,
                runner,
                configured_client(
                    repo, replace(self.settings, agent_backend=config.get("agent_backend", "local"))
                ),
                config["agents"],
            )
            repo.finish_execution(task_id)
            return
        repo.checkpoint_execution(task_id, "baseline")
        baseline = run_baseline(package, repo, runner, task_id=task_id)
        if baseline.WhichOneof("outcome") != "success":
            if repo.get_task(task_id)["status"] == "failed":
                repo.finish_execution(task_id)
                return
            raise InfrastructureError("baseline infrastructure failure")
        repo.checkpoint_execution(task_id, "optimizing")
        if stop is not None and stop.is_set():
            return
        coordinator_type, debate_options = CohortCoordinator, {}
        if "debate_policy" in config:
            from compiler_optimizer.coordinator.debate import DebateCoordinator
            from compiler_optimizer.shared.debate import DebatePolicy

            coordinator_type = DebateCoordinator
            debate_options["debate_policy"] = DebatePolicy.parse(config["debate_policy"])
        coordinator = coordinator_type(
            repo,
            runner,
            self.broker_factory(),
            recovery=self.recovery_factory(runner, RecoveryPolicy.parse(config["recovery_policy"])),
            agent_client=configured_client(
                repo, replace(self.settings, agent_backend=config.get("agent_backend", "local"))
            ),
            **debate_options,
        )
        options = {}
        if repo.get_selection_session(task_id) is None:
            options = {
                "agents": config["agents"],
                "policy": SelectionPolicy.parse(config["selection_policy"]),
                "verification_plan": VerificationPlan.parse(config["verification_plan"]),
                "benchmark_plan": BenchmarkPlan.parse(config["benchmark_plan"]),
            }
        coordinator.resume_locked(task_id, **options)
        repo.finish_execution(task_id)

    def run(self, stop):
        while not stop.is_set():
            try:
                self.run_once(stop)
            except Exception as error:
                logging.error(
                    json.dumps(
                        {
                            "event": "submission_dispatch_error",
                            "error_class": type(error).__name__,
                        }
                    )
                )
            stop.wait(1)
