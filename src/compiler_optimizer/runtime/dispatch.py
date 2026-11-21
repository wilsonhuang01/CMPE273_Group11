"""Sequential durable submission execution on a trusted, Docker-capable coordinator host."""

import json
import logging
import re

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.monitoring.watchdog import read_json, remove_owned
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.scheduler.recovery import RecoveryController
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.recovery import RecoveryPolicy
from compiler_optimizer.shared.selection import SelectionPolicy
from compiler_optimizer.storage.errors import ArtifactError, StateError
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import package_from_files, unbundle
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
        self.runner_factory = runner_factory or self._runner
        self.broker_factory = broker_factory or (lambda: RabbitProposalQueue(settings))
        self.recovery_factory = recovery_factory or (
            lambda runner, policy: RecoveryController.local(
                repository,
                runner,
                settings,
                policy=policy,
                watchdog_directory=runner.watchdog_directory,
            )
        )

    def _runner(self, submission):
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
        for path in self.watchdogs(task_id).glob("*/lease.json"):
            status = path.with_name("status.json")
            if status.exists() and read_json(status).get("state") == "removed":
                continue
            lease = read_json(path)
            require(
                re.fullmatch(r"compiler-baseline-[0-9a-f]{32}", lease["container"]) is not None,
                "invalid stored watchdog container",
            )
            # The pipeline lock ensures that no other orchestrator owns active work.
            path.with_name("stop.json").write_text(json.dumps({"reason": "dispatcher_resume"}))
            if remove_owned(lease) != "removed":
                raise InfrastructureError("previous sandbox cleanup unconfirmed")

    def run_once(self, stop=None):
        """Process one due task. A process crash leaves its execution row resumable."""
        repo = self.repository
        try:
            with repo.pipeline_lock():
                pending = repo.pending_executions()
                if not pending or (stop is not None and stop.is_set()):
                    return False
                task_id = pending[0]
                task = repo.get_task(task_id)
                if task["status"] in {"completed", "failed"}:
                    repo.finish_execution(task_id)
                    return True
                if not repo.start_execution(task_id):
                    repo.finish_execution(task_id, error_code="execution_attempts_exhausted")
                    return True
                try:
                    self._execute(task_id, stop)
                except (ContractError, ArtifactError) as error:
                    repo.finish_execution(task_id, error_code=type(error).__name__)
                except Exception as error:
                    # Transport, daemon and interrupted work retry under a durable total cap.
                    # Store only the class, never connection strings or credentials.
                    repo.finish_execution(task_id, error_code=type(error).__name__, retry=True)
                return True
        except StateError as error:
            if str(error) != "another pipeline owns this state directory":
                raise
            return False

    def _execute(self, task_id, stop):
        repo = self.repository
        config = repo.get_execution(task_id)["configuration"]
        self._cleanup(task_id)
        submission = repo.get_task(task_id)["submission"]
        package = package_from_files(unbundle(repo.artifacts.get(submission.package)))
        runner = self.runner_factory(submission)
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
        coordinator = CohortCoordinator(
            repo,
            runner,
            self.broker_factory(),
            recovery=self.recovery_factory(runner, RecoveryPolicy.parse(config["recovery_policy"])),
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
