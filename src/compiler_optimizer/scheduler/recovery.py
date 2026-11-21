"""Leased, heartbeat-monitored execution on a bounded pool of healthy sandbox workers."""

import base64
import json
import queue
import threading
import time
from contextvars import copy_context
from dataclasses import dataclass, field
from uuid import uuid4

from compiler_optimizer.monitoring.events import emit
from compiler_optimizer.scheduler.blacklist import BlacklistUnavailable, RedisBlacklist
from compiler_optimizer.shared.lease_context import RECOVERY_SCOPE
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.messaging import decode_message, encode_message
from compiler_optimizer.shared.recovery import (
    DeadLetterResult,
    ProgramCrash,
    RecoveryPolicy,
    RecoveryTerminal,
    RetryableExecution,
    RetryDelivery,
    canonical,
    digest,
    quarantine_key,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import StorageError
from compiler_optimizer.storage.recovery import LeaseLost
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError


@dataclass
class Worker:
    runner: object = None
    factory: object = None
    worker_id: str = field(default_factory=lambda: str(uuid4()))
    incarnation_id: str = field(default_factory=lambda: str(uuid4()))
    sequence: int = 0
    retired: bool = False

    def materialize(self):
        if self.runner is None:
            self.runner = self.factory()
        return self.runner

    def heartbeat(self, repository, task_id, job_id=None):
        self.sequence += 1
        message = pb.WorkerHeartbeat(
            meta=metadata(task_id if job_id is not None else ""),
            worker_id=self.worker_id,
            incarnation_id=self.incarnation_id,
            sequence=self.sequence,
            state=pb.IDLE if job_id is None else pb.BUSY,
            targets=[getattr(self.runner, "target", "linux-x86_64")],
        )
        if job_id is not None:
            message.current_job_id = job_id
        repository.report_worker_heartbeat(message)


class GuardedRunner:
    def __init__(self, repository, proposal, worker):
        self.repository, self.proposal, self.worker = repository, proposal, worker
        self.worker_id = worker.worker_id

    def __getattr__(self, name):
        value = getattr(self.worker.runner, name)
        if name not in ("compile", "execute", "benchmark_environment"):
            return value

        def guarded(*args, **kwargs):
            self.repository.assert_execution_lease(
                self.proposal.meta.task_id, self.proposal.proposal_id
            )
            return value(*args, **kwargs)

        return guarded


class RecoveryController:
    def __init__(self, repository, workers, blacklist, *, policy=None):
        require(1 <= len(workers) <= 8, "recovery needs 1–8 workers")
        self.repository, self.workers, self.blacklist = repository, list(workers), blacklist
        require(len({w.worker_id for w in workers}) == len(workers), "duplicate worker identity")
        self.policy = policy or RecoveryPolicy()
        self.policy.encode()
        self.explicit_policy = policy is not None

    @classmethod
    def local(
        cls, repository, runner, settings, *, policy=None, worker_count=2, watchdog_directory=None
    ):
        workers = [Worker(runner=runner)]
        workers.extend(
            Worker(
                factory=lambda: DockerRunner(
                    runner.image,
                    watchdog_directory=watchdog_directory or repository.root / "watchdogs",
                    target=runner.target,
                )
            )
            for _ in range(worker_count - 1)
        )
        return cls(repository, workers, RedisBlacklist.from_settings(settings), policy=policy)

    def _put(self, value):
        return self.repository.artifacts.put(canonical(value))

    def _context(self, proposal, config):
        submission = self.repository.get_task(proposal.meta.task_id)["submission"]
        compiler = submission.compiler
        limits = submission.manifest.limits
        return {
            "schema_version": 1,
            "source_sha256": proposal.source.bundle.sha256,
            "transformation": proposal.transformation,
            "parent_source_sha256": submission.source.bundle.sha256,
            "package_sha256": submission.package.sha256,
            "compiler": {
                "image": compiler.image_digest,
                "executable": compiler.executable,
                "version": compiler.version,
                "language": compiler.language,
                "target": compiler.target,
                "flags": list(compiler.flags),
            },
            "verification_plan_sha256": config["verification_plan"]["sha256"],
            "benchmark_plan_sha256": config["benchmark_plan"]["sha256"],
            "limits": {
                "compile_wall_seconds": limits.compile_wall_seconds,
                "compile_memory_mib": limits.compile_memory_mib,
                "run_wall_seconds": limits.run_wall_seconds,
                "run_memory_mib": limits.run_memory_mib,
                "output_kib": limits.output_kib,
            },
        }

    def _policy_for(self, proposal):
        existing = self.repository.get_recovery_job(proposal.meta.task_id, proposal.proposal_id)
        if existing is not None:
            refs = self.repository.list_artifacts(proposal.meta.task_id)
            ref = next(r for r in refs if r["key"] == existing["policy_key"])
            ref = pb.ArtifactRef(**{k: ref[k] for k in ("key", "sha256", "size_bytes")})
            frozen = RecoveryPolicy.parse(self.repository.artifacts.get(ref))
            require(not self.explicit_policy or frozen == self.policy, "recovery policy is frozen")
            return frozen, ref
        return self.policy, self.repository.artifacts.put(self.policy.encode())

    def _retry(self, job):
        events = [
            e
            for e in self.repository.recovery_outbox(job["task_id"])
            if e["job_id"] == job["job_id"] and e["kind"] == "retry"
        ]
        event_id = events[-1]["event_id"] if events else job["job_id"]
        return RetryDelivery(
            max(1, job["available_at_ms"] - self.repository.recovery_now()),
            event_id,
            lambda: self.repository.mark_recovery_event_sent(event_id),
        )

    def _terminal(self, job, reason, *, record=None):
        cached = self.repository.recovery_outcome(job["task_id"], job["proposal_id"])
        if cached is None:
            receipt = {
                "schema_version": 1,
                "task_id": job["task_id"],
                "proposal_id": job["proposal_id"],
                "recovery_job_id": job["job_id"],
                "reason": reason,
                "attempts": job["attempt"],
                "context_sha256": digest(json.loads(job["execution_context_json"]))
                if job["execution_context_json"]
                else None,
                "blacklist_key": None if record is None else record["key"],
            }
            self.repository.terminal_recovery_job(
                job["task_id"],
                job["proposal_id"],
                self._put(receipt),
                quarantined=record is not None,
                blacklist=record if reason == "confirmed_program_crash" else None,
                lease=job if job["state"] == "leased" else None,
            )
            emit(
                "recovery_terminal",
                task_id=job["task_id"],
                proposal_id=job["proposal_id"],
                job_id=job["job_id"],
                attempt=job["attempt"],
                reason=reason,
            )
            cached = receipt
        raise RecoveryTerminal(cached)

    def _pick(self, job, proposal, policy):
        repository = self.repository
        attempts = repository.recovery_attempts(job["task_id"], job["proposal_id"])
        used = [a["worker_id"] for a in attempts]
        candidates = sorted(
            self.workers,
            key=lambda w: (w.worker_id in used, used.count(w.worker_id), self.workers.index(w)),
        )
        for worker in candidates:
            if worker.retired:
                continue
            known = next(
                (w for w in repository.recovery_workers() if w["worker_id"] == worker.worker_id),
                None,
            )
            if known is not None and known["quarantine_until_ms"] > repository.recovery_now():
                continue
            if job["mode"] == "confirm" and job["crash_json"]:
                first = json.loads(job["crash_json"])
                if worker.worker_id == first["worker_id"]:
                    continue
            try:
                runner = worker.materialize()
                compiler = self.repository.get_task(job["task_id"])["submission"].compiler
                if runner.compiler(compiler.language) != compiler:
                    continue  # An incompatible target/image is not an unhealthy worker.
                if hasattr(runner, "check_health"):
                    runner.check_health()
                context = {
                    **json.loads(job["base_context_json"]),
                    "execution": runner.recovery_context()
                    if hasattr(runner, "recovery_context")
                    else {"hardware": runner.hardware},
                }
                if (
                    job["execution_context_json"]
                    and json.loads(job["execution_context_json"]) != context
                ):
                    continue
                repository.register_recovery_worker(
                    job["task_id"], worker.worker_id, worker.incarnation_id
                )
                if not repository.probe_worker_recovered(worker.worker_id, worker.incarnation_id):
                    continue
                worker.heartbeat(repository, job["task_id"])
                return worker, context
            except (InfrastructureError, StorageError, ValueError, OSError):
                try:
                    repository.quarantine_worker(worker.worker_id, policy.worker_cooldown_seconds)
                except StorageError:
                    pass
        return None, None

    def _heartbeat(self, stop, lost, lease, worker, policy):
        while not stop.wait(policy.heartbeat_seconds):
            try:
                worker.heartbeat(self.repository, lease["task_id"], lease["job_id"])
                self.repository.renew_execution_lease(lease)
            except (StorageError, ValueError, OSError):
                lost.set()
                return

    def _cancel(self, worker, token, policy):
        if worker.runner is None:
            return False
        if not hasattr(worker.runner, "cancel_recovery_attempt"):
            return True  # Trusted in-process test/SDK runners must enforce their own cancellation.
        deadline = time.monotonic() + policy.cleanup_grace_seconds
        while True:
            try:
                if worker.runner.cancel_recovery_attempt(token):
                    return True
            except (InfrastructureError, OSError):
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))

    def evaluate(self, proposal, config, action, *, scope="evaluate"):
        token = RECOVERY_SCOPE.set(scope)
        try:
            return self._evaluate_scoped(proposal, config, action)
        finally:
            RECOVERY_SCOPE.reset(token)

    def _evaluate_scoped(self, proposal, config, action):
        try:
            return self._evaluate(proposal, config, action)
        except LeaseLost:
            # Expiry can race any final commit or failure receipt. It is a recovery
            # event, never a reason to accept stale evidence or exclude the proposal.
            self.repository.expire_recovery_lease(proposal.meta.task_id, proposal.proposal_id)
            job = self.repository.get_recovery_job(proposal.meta.task_id, proposal.proposal_id)
            if job["state"] == "leased":
                raise RetryDelivery(
                    max(1, job["lease_expires_ms"] - self.repository.recovery_now()), job["job_id"]
                ) from None
            raise self._retry(job) from None

    def _evaluate(self, proposal, config, action):
        repository = self.repository
        policy, policy_ref = self._policy_for(proposal)
        job = repository.ensure_recovery_job(proposal, self._context(proposal, config), policy_ref)
        repository.expire_recovery_lease(proposal.meta.task_id, proposal.proposal_id)
        job = repository.get_recovery_job(proposal.meta.task_id, proposal.proposal_id)
        if job["state"] == "completed":
            output = repository.recovery_outcome(job["task_id"], job["proposal_id"])
            return self._decode(output)
        if job["state"] in ("quarantined", "exhausted"):
            self._terminal(job, "infrastructure_attempts_exhausted")
        if job["state"] == "leased":
            raise RetryDelivery(
                max(1, job["lease_expires_ms"] - repository.recovery_now()), job["job_id"]
            )
        if job["cleanup_token"]:
            clean = any(self._cancel(w, job["cleanup_token"], policy) for w in self.workers)
            if not clean:
                self._terminal(job, "sandbox_cleanup_unconfirmed")
            repository.confirm_recovery_cleanup(
                job["task_id"], job["proposal_id"], job["cleanup_token"]
            )
        if job["available_at_ms"] > repository.recovery_now():
            raise self._retry(job)
        candidates = [
            w
            for w in self.workers
            if not w.retired
            and not (
                job["crash_json"]
                and job["mode"] == "confirm"
                and w.worker_id == json.loads(job["crash_json"])["worker_id"]
            )
        ]
        rows = {w["worker_id"]: w for w in repository.recovery_workers()}
        if candidates and all(
            w.worker_id in rows
            and rows[w.worker_id]["quarantine_until_ms"] > repository.recovery_now()
            for w in candidates
        ):
            ready = min(rows[w.worker_id]["quarantine_until_ms"] for w in candidates)
            raise RetryDelivery(ready - repository.recovery_now(), job["job_id"])
        worker, context = self._pick(job, proposal, policy)
        if worker is None:
            repository.defer_unavailable_worker(job["task_id"], job["proposal_id"])
            return self._evaluate(proposal, config, action)
        lease = repository.claim_recovery_job(
            job["task_id"], job["proposal_id"], worker.worker_id, worker.incarnation_id, context
        )
        emit(
            "attempt_started",
            task_id=job["task_id"],
            proposal_id=job["proposal_id"],
            job_id=job["job_id"],
            worker_id=worker.worker_id,
            attempt=lease["attempt"],
        )
        key = quarantine_key(proposal.transformation, proposal.source.bundle.sha256, context)
        local = repository.local_blacklist(key)
        try:
            record = local or self.blacklist.get(key, repository.recovery_now())
            if record is not None:
                RedisBlacklist.validate(record, key, repository.recovery_now())
                self._terminal(lease, "blacklisted_execution_context", record=record)
        except (BlacklistUnavailable, ValueError, KeyError, TypeError) as error:
            self._retry_failure(lease, worker, policy, error, {"reason": "blacklist_unavailable"})
        stop, lost, results = threading.Event(), threading.Event(), queue.Queue(maxsize=1)
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(stop, lost, lease, worker, policy), daemon=True
        )
        heartbeat.start()

        def execute():
            try:
                with repository.execution_lease(lease):
                    value = action(
                        GuardedRunner(repository, proposal, worker), lease["mode"] == "confirm"
                    )
                results.put((True, value))
            except BaseException as error:
                results.put((False, error))

        thread_context = copy_context()
        thread = threading.Thread(target=thread_context.run, args=(execute,), daemon=True)
        thread.start()
        started = time.monotonic()
        deadline = started + policy.attempt_timeout_seconds
        try:
            while True:
                if lost.is_set() or time.monotonic() >= deadline:
                    worker.retired = True
                    emit(
                        "worker_retired",
                        task_id=job["task_id"],
                        proposal_id=job["proposal_id"],
                        worker_id=worker.worker_id,
                        attempt=lease["attempt"],
                        reason="heartbeat_lost" if lost.is_set() else "attempt_deadline",
                    )
                    clean = self._cancel(worker, lease["lease_token"], policy)
                    repository.expire_recovery_lease(lease["task_id"], lease["proposal_id"])
                    current = repository.get_recovery_job(lease["task_id"], lease["proposal_id"])
                    if current["state"] == "leased":
                        self._retry_failure(
                            lease,
                            worker,
                            policy,
                            LeaseLost("worker lease lost"),
                            {"reason": "worker_lease_lost"},
                            cleanup_confirmed=clean,
                        )
                    if clean and current["cleanup_token"]:
                        repository.confirm_recovery_cleanup(
                            lease["task_id"], lease["proposal_id"], current["cleanup_token"]
                        )
                    raise self._retry(
                        repository.get_recovery_job(lease["task_id"], lease["proposal_id"])
                    )
                try:
                    success, value = results.get(timeout=0.05)
                    break
                except queue.Empty:
                    pass
            if not success:
                if isinstance(value, LeaseLost):
                    worker.retired = True
                    clean = self._cancel(worker, lease["lease_token"], policy)
                    repository.expire_recovery_lease(lease["task_id"], lease["proposal_id"])
                    current = repository.get_recovery_job(lease["task_id"], lease["proposal_id"])
                    if clean and current["cleanup_token"]:
                        repository.confirm_recovery_cleanup(
                            lease["task_id"], lease["proposal_id"], current["cleanup_token"]
                        )
                    if current["state"] == "exhausted":
                        self._terminal(current, "infrastructure_attempts_exhausted")
                    raise self._retry(current)
                if isinstance(value, ProgramCrash):
                    self._program_crash(lease, worker, policy, proposal, context, value)
                if isinstance(value, RecoveryTerminal):
                    self._terminal(lease, value.receipt["reason"])
                if isinstance(value, (RetryableExecution, InfrastructureError, LeaseLost, OSError)):
                    self._retry_failure(
                        lease,
                        worker,
                        policy,
                        value,
                        getattr(value, "evidence", {"reason": type(value).__name__}),
                    )
                if isinstance(value, (ValueError, StorageError)):
                    self._terminal(lease, "invalid_worker_evidence")
                if isinstance(value, Exception):
                    self._retry_failure(
                        lease,
                        worker,
                        policy,
                        value,
                        {"reason": "unexpected_worker_exception", "class": type(value).__name__},
                    )
                raise value
            outcome = self._encode(value)
            repository.finish_recovery_attempt(lease, self._put(outcome))
            emit(
                "attempt_completed",
                task_id=job["task_id"],
                proposal_id=job["proposal_id"],
                job_id=job["job_id"],
                worker_id=worker.worker_id,
                attempt=lease["attempt"],
                duration_seconds=time.monotonic() - started,
            )
            return value
        finally:
            stop.set()
            heartbeat.join(timeout=2)

    def _retry_failure(self, lease, worker, policy, error, evidence, *, cleanup_confirmed=None):
        if cleanup_confirmed is None:
            cleanup_confirmed = self._cancel(worker, lease["lease_token"], policy)
        self.repository.retry_recovery_attempt(
            lease,
            "infrastructure_failure",
            self._put({"reason": str(error)[:4096], "evidence": evidence}),
            cleanup_confirmed=cleanup_confirmed,
        )
        job = self.repository.get_recovery_job(lease["task_id"], lease["proposal_id"])
        emit(
            "infrastructure_retry",
            task_id=job["task_id"],
            proposal_id=job["proposal_id"],
            job_id=job["job_id"],
            worker_id=worker.worker_id,
            attempt=job["attempt"],
            reason=evidence.get("reason", "infrastructure_failure"),
        )
        if job["state"] == "exhausted":
            self._terminal(job, "infrastructure_attempts_exhausted")
        raise self._retry(job)

    def _program_crash(self, lease, worker, policy, proposal, context, error):
        emit(
            "program_crash",
            task_id=lease["task_id"],
            proposal_id=lease["proposal_id"],
            job_id=lease["job_id"],
            worker_id=worker.worker_id,
            attempt=lease["attempt"],
            reason=error.signature["reason"],
        )
        observation = {
            "worker_id": worker.worker_id,
            "incarnation_id": worker.incarnation_id,
            "signature": error.signature,
            "evidence": error.evidence,
        }
        if lease["crash_json"]:
            first = json.loads(lease["crash_json"])
            if first["signature"] == error.signature and first["worker_id"] != worker.worker_id:
                now = self.repository.recovery_now()
                record = {
                    "schema_version": 1,
                    "key": quarantine_key(
                        proposal.transformation, proposal.source.bundle.sha256, context
                    ),
                    "source_sha256": proposal.source.bundle.sha256,
                    "transformation": proposal.transformation,
                    "context": context,
                    "context_sha256": digest(context),
                    "created_ms": now,
                    "expires_ms": now + policy.blacklist_ttl_seconds * 1000,
                    "confirmations": [first, observation],
                }
                self._terminal(lease, "confirmed_program_crash", record=record)
            self._terminal(lease, "unstable_program_failure")
        self.repository.retry_recovery_attempt(
            lease, "program_crash", self._put(observation), crash=observation
        )
        job = self.repository.get_recovery_job(lease["task_id"], lease["proposal_id"])
        if job["state"] == "exhausted":
            self._terminal(job, "unconfirmed_program_failure")
        raise self._retry(job)

    @staticmethod
    def _encode(value):
        result, benchmark, reviews, work = value
        return {
            "schema_version": 1,
            "result_base64": base64.b64encode(encode_message(result)).decode(),
            "benchmark": benchmark,
            "reviews": reviews,
            "work": work,
        }

    @staticmethod
    def _decode(value):
        return (
            decode_message(
                pb.VerificationResult, base64.b64decode(value["result_base64"], validate=True)
            ),
            value["benchmark"],
            value["reviews"],
            value["work"],
        )

    def deadletter_result(self, receipt, result):
        events = [
            e
            for e in self.repository.recovery_outbox(receipt["task_id"], pending=False)
            if e["job_id"] == receipt["recovery_job_id"] and e["kind"] == "deadletter"
        ]
        event = events[-1]
        if event["sent_ms"] is not None:
            return result
        return DeadLetterResult(
            result,
            receipt["reason"],
            event["event_id"],
            lambda: self.repository.mark_recovery_event_sent(event["event_id"]),
        )

    def publish_quarantines(self, task_id):
        """Publish durable exclusions even for standalone commands without a broker."""
        for event in self.repository.recovery_outbox(task_id):
            if event["kind"] != "blacklist":
                continue
            payload = json.loads(event["payload_json"])
            record = json.loads(self.repository.artifacts.get(pb.ArtifactRef(**payload["record"])))
            if record["expires_ms"] > self.repository.recovery_now():
                try:
                    self.blacklist.put(record, self.repository.recovery_now())
                except BlacklistUnavailable:
                    continue
            self.repository.mark_recovery_event_sent(event["event_id"])

    def flush_outbox(self, task_id, broker):
        for event in self.repository.recovery_outbox(task_id):
            payload = json.loads(event["payload_json"])
            if event["kind"] == "blacklist":
                record = json.loads(
                    self.repository.artifacts.get(pb.ArtifactRef(**payload["record"]))
                )
                if record["expires_ms"] <= self.repository.recovery_now():
                    self.repository.mark_recovery_event_sent(event["event_id"])
                    continue
                try:
                    self.blacklist.put(record, self.repository.recovery_now())
                except BlacklistUnavailable:
                    continue
            elif event["kind"] == "retry" and (
                event["job_state"] in ("completed", "quarantined", "exhausted")
                or self.repository.get_task(task_id)["status"] in ("completed", "failed")
            ):
                pass
            else:
                proposal = next(
                    p
                    for p in self.repository.list_proposals(task_id)
                    if p.proposal_id == event["proposal_id"]
                )
                token = RECOVERY_SCOPE.set(event["scope"])
                try:
                    job = self.repository.get_recovery_job(task_id, proposal.proposal_id)
                finally:
                    RECOVERY_SCOPE.reset(token)
                broker.publish_recovery_event(
                    proposal,
                    event["kind"],
                    event["event_id"],
                    reason=payload.get("reason", "retry"),
                    delay_ms=max(1, job["available_at_ms"] - self.repository.recovery_now()),
                )
            self.repository.mark_recovery_event_sent(event["event_id"])
