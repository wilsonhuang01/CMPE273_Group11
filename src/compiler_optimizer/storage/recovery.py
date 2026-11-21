"""Coordinator-owned worker health, fenced task leases, retry state, and quarantine outbox."""

import json
import time
from contextlib import contextmanager
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from compiler_optimizer.shared.contracts import validate_message
from compiler_optimizer.shared.lease_context import EXECUTION_LEASE, RECOVERY_SCOPE
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.messaging import encode_message
from compiler_optimizer.shared.recovery import RecoveryPolicy, canonical, digest
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import ConflictError, NotFoundError, StateError


class LeaseLost(StateError):
    """Execution authority expired or was replaced; no further writes may commit."""


class RecoveryStorage:
    def recovery_now(self):
        return getattr(self, "recovery_clock_ms", lambda: time.time_ns() // 1000000)()

    def register_recovery_worker(self, task_id, worker_id, incarnation_id):
        require(
            str(UUID(worker_id)) == worker_id and str(UUID(incarnation_id)) == incarnation_id,
            "worker identities must be UUIDs",
        )
        with self._transaction() as db:
            self._task(db, task_id)
            prior = db.execute(
                "SELECT * FROM recovery_workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if prior is not None and prior["incarnation_id"] == incarnation_id:
                require(
                    prior["current_job_id"] is None or prior["task_id"] == task_id,
                    "busy worker cannot change tasks",
                )
                db.execute(
                    "UPDATE recovery_workers SET task_id=? WHERE worker_id=?", (task_id, worker_id)
                )
                return False
            if prior is not None and prior["current_job_id"] is not None:
                raise StateError("cannot replace a worker with an active assignment")
            if prior is None:
                db.execute(
                    "INSERT INTO recovery_workers VALUES (?, ?, ?, 0, 'idle', ?, 0, NULL, NULL)",
                    (worker_id, incarnation_id, task_id, self.recovery_now()),
                )
            else:
                db.execute(
                    "UPDATE recovery_workers SET incarnation_id=?, task_id=?, sequence=0, "
                    "state='idle', last_seen_ms=?, quarantine_until_ms=0, "
                    "current_job_id=NULL, heartbeat=NULL "
                    "WHERE worker_id=?",
                    (incarnation_id, task_id, self.recovery_now(), worker_id),
                )
        return True

    def report_worker_heartbeat(self, heartbeat):
        validate_message(heartbeat)
        payload = encode_message(heartbeat)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM recovery_workers WHERE worker_id=?", (heartbeat.worker_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("worker has not been registered")
            require(
                row["incarnation_id"] == heartbeat.incarnation_id
                and (heartbeat.state != pb.BUSY or row["task_id"] == heartbeat.meta.task_id),
                "stale worker incarnation/task",
            )
            if heartbeat.sequence <= row["sequence"]:
                if heartbeat.sequence == row["sequence"] and row["heartbeat"] == payload:
                    return False
                raise ConflictError("stale or conflicting heartbeat sequence")
            expected = row["current_job_id"]
            if row["state"] == "quarantined":
                raise StateError("quarantined workers need a control-plane health probe")
            require(
                (expected is None and heartbeat.state == pb.IDLE)
                or (
                    expected is not None
                    and heartbeat.state == pb.BUSY
                    and heartbeat.current_job_id == expected
                ),
                "heartbeat assignment mismatch",
            )
            db.execute(
                "UPDATE recovery_workers SET sequence=?, last_seen_ms=?, heartbeat=? "
                "WHERE worker_id=?",
                (heartbeat.sequence, self.recovery_now(), payload, heartbeat.worker_id),
            )
        return True

    def recovery_workers(self):
        with self._transaction(write=False) as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM recovery_workers ORDER BY worker_id")
            ]

    def probe_worker_recovered(self, worker_id, incarnation_id):
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM recovery_workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            require(
                row is not None and row["incarnation_id"] == incarnation_id,
                "worker incarnation changed",
            )
            require(row["current_job_id"] is None, "worker is still assigned")
            if row["quarantine_until_ms"] > self.recovery_now():
                return False
            db.execute(
                "UPDATE recovery_workers SET state='idle', last_seen_ms=? WHERE worker_id=?",
                (self.recovery_now(), worker_id),
            )
        return True

    def quarantine_worker(self, worker_id, seconds):
        with self._transaction() as db:
            db.execute(
                "UPDATE recovery_workers SET state='quarantined', quarantine_until_ms=? "
                "WHERE worker_id=?",
                (self.recovery_now() + int(seconds * 1000), worker_id),
            )

    def ensure_recovery_job(self, proposal, base_context, policy_ref):
        RecoveryPolicy.parse(self.artifacts.get(policy_ref))
        context = canonical(base_context).decode()
        with self._transaction() as db:
            task_id, proposal_id = proposal.meta.task_id, proposal.proposal_id
            self._task(db, task_id)
            existing = db.execute(
                "SELECT * FROM recovery_jobs WHERE task_id=? AND proposal_id=? AND scope=?",
                (task_id, proposal_id, RECOVERY_SCOPE.get()),
            ).fetchone()
            if existing is not None:
                if (existing["policy_key"], existing["base_context_json"]) != (
                    policy_ref.key,
                    context,
                ):
                    raise ConflictError("recovery policy/context is frozen")
                return dict(existing)
            self._artifact(db, task_id, policy_ref, "recovery-policy", owner=proposal_id)
            job_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"compiler-optimizer:recovery:{task_id}:{proposal_id}"
                    + (":" + RECOVERY_SCOPE.get() if RECOVERY_SCOPE.get() != "evaluate" else ""),
                )
            )
            db.execute(
                "INSERT INTO recovery_jobs(task_id,proposal_id,job_id,policy_key,"
                "base_context_json,scope,state) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (task_id, proposal_id, job_id, policy_ref.key, context, RECOVERY_SCOPE.get()),
            )
        return self.get_recovery_job(task_id, proposal_id)

    def get_recovery_job(self, task_id, proposal_id):
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT * FROM recovery_jobs WHERE task_id=? AND proposal_id=? AND scope=?",
                (task_id, proposal_id, RECOVERY_SCOPE.get()),
            ).fetchone()
            return None if row is None else dict(row)

    def recovery_jobs(self, task_id):
        with self._transaction(write=False) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM recovery_jobs WHERE task_id=? ORDER BY rowid", (task_id,)
                )
            ]

    def recovery_attempts(self, task_id, proposal_id):
        with self._transaction(write=False) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT a.* FROM recovery_attempts a JOIN recovery_jobs j "
                    "ON j.job_id=a.job_id WHERE j.task_id=? AND j.proposal_id=? "
                    "AND j.scope=? ORDER BY a.attempt",
                    (task_id, proposal_id, RECOVERY_SCOPE.get()),
                )
            ]

    def _recovery_policy(self, db, row):
        return RecoveryPolicy.parse(self.artifacts.get(self._ref(db, row["policy_key"])))

    def _outbox(self, db, row, kind, payload):
        event_id = str(
            uuid5(NAMESPACE_URL, f"{row['job_id']}:{row['attempt']}:{kind}:{digest(payload)}")
        )
        db.execute(
            "INSERT OR IGNORE INTO recovery_outbox VALUES (?, ?, ?, ?, NULL)",
            (event_id, row["job_id"], kind, canonical(payload).decode()),
        )
        return event_id

    def _assert_recovery_fence(self, db, lease):
        row = db.execute(
            "SELECT * FROM recovery_jobs WHERE job_id=?",
            (lease["job_id"],),
        ).fetchone()
        now = self.recovery_now()
        if (
            row is None
            or row["state"] != "leased"
            or row["lease_token"] != lease["lease_token"]
            or row["fence"] != lease["fence"]
            or row["worker_id"] != lease["worker_id"]
            or row["incarnation_id"] != lease["incarnation_id"]
            or min(row["lease_expires_ms"], row["deadline_ms"]) <= now
        ):
            raise LeaseLost("execution lease expired or was fenced")
        policy = self._recovery_policy(db, row)
        worker = db.execute(
            "SELECT * FROM recovery_workers WHERE worker_id=?", (lease["worker_id"],)
        ).fetchone()
        if (
            worker is None
            or worker["incarnation_id"] != lease["incarnation_id"]
            or worker["state"] != "busy"
            or worker["current_job_id"] != row["job_id"]
            or now - worker["last_seen_ms"] > policy.worker_health_seconds * 1000
        ):
            raise LeaseLost("worker heartbeat expired or worker was quarantined")

    def assert_execution_lease(self, task_id, proposal_id):
        with self._transaction(write=False) as db:
            self._assert_managed_execution(db, task_id, proposal_id)

    def _assert_managed_execution(self, db, task_id, proposal_id):
        if (
            db.execute(
                "SELECT 1 FROM recovery_jobs WHERE task_id=? AND proposal_id=?",
                (task_id, proposal_id),
            ).fetchone()
            is None
        ):
            return
        lease = EXECUTION_LEASE.get()
        if (
            lease is None
            or lease["database"] != str(self.database)
            or (lease["task_id"], lease["proposal_id"]) != (task_id, proposal_id)
        ):
            raise LeaseLost("managed execution needs its fencing token")
        self._assert_recovery_fence(db, lease)

    @contextmanager
    def execution_lease(self, lease):
        token = EXECUTION_LEASE.set(
            {
                **lease,
                "database": str(self.database),
                "assert_live": lambda: self.assert_execution_lease(
                    lease["task_id"], lease["proposal_id"]
                ),
            }
        )
        try:
            self.assert_execution_lease(lease["task_id"], lease["proposal_id"])
            yield
        finally:
            EXECUTION_LEASE.reset(token)

    def claim_recovery_job(self, task_id, proposal_id, worker_id, incarnation_id, context):
        with self._transaction() as db:
            self._task(db, task_id, running=True)
            row = db.execute(
                "SELECT * FROM recovery_jobs WHERE task_id=? AND proposal_id=? AND scope=?",
                (task_id, proposal_id, RECOVERY_SCOPE.get()),
            ).fetchone()
            require(row is not None, "recovery job is missing")
            policy, now = self._recovery_policy(db, row), self.recovery_now()
            require(
                row["state"] in ("pending", "backoff") and row["available_at_ms"] <= now,
                "recovery job is not available",
            )
            require(row["cleanup_token"] is None, "previous sandbox cleanup is unconfirmed")
            require(row["attempt"] < policy.maximum_attempts, "recovery attempts exhausted")
            encoded = canonical(context).decode()
            require(
                row["execution_context_json"] is None or row["execution_context_json"] == encoded,
                "retry worker execution context differs",
            )
            worker = db.execute(
                "SELECT * FROM recovery_workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            require(
                worker is not None
                and worker["incarnation_id"] == incarnation_id
                and worker["task_id"] == task_id
                and worker["state"] == "idle"
                and worker["current_job_id"] is None
                and now - worker["last_seen_ms"] <= policy.worker_health_seconds * 1000,
                "worker is not healthy and idle",
            )
            token, attempt, fence = str(uuid4()), row["attempt"] + 1, row["fence"] + 1
            deadline = now + int(policy.attempt_timeout_seconds * 1000)
            db.execute(
                "UPDATE recovery_jobs SET state='leased', attempt=?, fence=?, worker_id=?, "
                "incarnation_id=?, lease_token=?, lease_expires_ms=?, deadline_ms=?, "
                "execution_context_json=? "
                "WHERE task_id=? AND proposal_id=? AND scope=?",
                (
                    attempt,
                    fence,
                    worker_id,
                    incarnation_id,
                    token,
                    min(deadline, now + int(policy.lease_seconds * 1000)),
                    deadline,
                    encoded,
                    task_id,
                    proposal_id,
                    RECOVERY_SCOPE.get(),
                ),
            )
            db.execute(
                "UPDATE recovery_workers SET state='busy', current_job_id=? WHERE worker_id=?",
                (row["job_id"], worker_id),
            )
            db.execute(
                "INSERT INTO recovery_attempts VALUES (?, ?, ?, ?, ?, ?, NULL, 'running', NULL)",
                (row["job_id"], attempt, worker_id, token, fence, now),
            )
        return self.get_recovery_job(task_id, proposal_id)

    def renew_execution_lease(self, lease):
        with self._transaction() as db:
            self._assert_recovery_fence(db, lease)
            policy = self._recovery_policy(db, lease)
            worker = db.execute(
                "SELECT * FROM recovery_workers WHERE worker_id=?", (lease["worker_id"],)
            ).fetchone()
            require(
                worker["state"] == "busy" and worker["incarnation_id"] == lease["incarnation_id"],
                "worker is no longer healthy",
            )
            db.execute(
                "UPDATE recovery_jobs SET lease_expires_ms=? WHERE job_id=?",
                (
                    min(
                        lease["deadline_ms"], self.recovery_now() + int(policy.lease_seconds * 1000)
                    ),
                    lease["job_id"],
                ),
            )

    def _release_worker(self, db, row, policy, unhealthy):
        if row["worker_id"] is not None:
            db.execute(
                "UPDATE recovery_workers SET state=?, current_job_id=NULL, quarantine_until_ms=? "
                "WHERE worker_id=? AND incarnation_id=? AND current_job_id=?",
                (
                    "quarantined" if unhealthy else "idle",
                    self.recovery_now()
                    + (int(policy.worker_cooldown_seconds * 1000) if unhealthy else 0),
                    row["worker_id"],
                    row["incarnation_id"],
                    row["job_id"],
                ),
            )

    def finish_recovery_attempt(self, lease, outcome_ref):
        with self._transaction() as db:
            self._assert_recovery_fence(db, lease)
            self._artifact(
                db, lease["task_id"], outcome_ref, "recovery-outcome", owner=lease["proposal_id"]
            )
            db.execute(
                "UPDATE recovery_jobs SET state='completed', outcome_key=?, lease_expires_ms=NULL "
                "WHERE job_id=?",
                (outcome_ref.key, lease["job_id"]),
            )
            db.execute(
                "UPDATE recovery_attempts SET status='completed', finished_ms=?, evidence_key=? "
                "WHERE job_id=? AND attempt=?",
                (self.recovery_now(), outcome_ref.key, lease["job_id"], lease["attempt"]),
            )
            self._release_worker(db, lease, self._recovery_policy(db, lease), False)

    def retry_recovery_attempt(
        self, lease, reason, evidence_ref, *, crash=None, cleanup_confirmed=True
    ):
        with self._transaction() as db:
            self._assert_recovery_fence(db, lease)
            policy, now = self._recovery_policy(db, lease), self.recovery_now()
            self._artifact(
                db, lease["task_id"], evidence_ref, "recovery-failure", owner=lease["proposal_id"]
            )
            db.execute(
                "UPDATE recovery_attempts SET status=?, finished_ms=?, evidence_key=? "
                "WHERE job_id=? AND attempt=?",
                (reason, now, evidence_ref.key, lease["job_id"], lease["attempt"]),
            )
            exhausted = lease["attempt"] >= policy.maximum_attempts
            db.execute(
                "UPDATE recovery_jobs SET state=?, fence=fence+1, lease_expires_ms=NULL, "
                "available_at_ms=?, mode=?, crash_json=?, cleanup_token=? WHERE job_id=?",
                (
                    "exhausted" if exhausted else "backoff",
                    now + policy.delay_ms(lease["job_id"], lease["attempt"]),
                    "confirm" if crash else lease["mode"],
                    canonical(crash).decode() if crash else lease["crash_json"],
                    None if cleanup_confirmed else lease["lease_token"],
                    lease["job_id"],
                ),
            )
            self._release_worker(db, lease, policy, reason != "program_crash")
            if not exhausted:
                return self._outbox(
                    db, lease, "retry", {"attempt": lease["attempt"], "reason": reason}
                )
        return None

    def expire_recovery_lease(self, task_id, proposal_id):
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM recovery_jobs WHERE task_id=? AND proposal_id=? AND scope=?",
                (task_id, proposal_id, RECOVERY_SCOPE.get()),
            ).fetchone()
            if row is None or row["state"] != "leased":
                return False
            policy, now = self._recovery_policy(db, row), self.recovery_now()
            worker = db.execute(
                "SELECT * FROM recovery_workers WHERE worker_id=?", (row["worker_id"],)
            ).fetchone()
            fresh = (
                worker is not None
                and worker["state"] == "busy"
                and worker["incarnation_id"] == row["incarnation_id"]
                and now - worker["last_seen_ms"] <= policy.worker_health_seconds * 1000
            )
            if fresh and min(row["lease_expires_ms"], row["deadline_ms"]) > now:
                return False
            state = "exhausted" if row["attempt"] >= policy.maximum_attempts else "backoff"
            db.execute(
                "UPDATE recovery_jobs SET state=?, fence=fence+1, cleanup_token=lease_token, "
                "available_at_ms=?, lease_expires_ms=NULL WHERE job_id=?",
                (state, now + policy.delay_ms(row["job_id"], row["attempt"]), row["job_id"]),
            )
            db.execute(
                "UPDATE recovery_attempts SET status='lease_expired', finished_ms=? "
                "WHERE job_id=? AND attempt=?",
                (now, row["job_id"], row["attempt"]),
            )
            self._release_worker(db, row, policy, True)
            if state == "backoff":
                self._outbox(
                    db, row, "retry", {"attempt": row["attempt"], "reason": "lease_expired"}
                )
        return True

    def confirm_recovery_cleanup(self, task_id, proposal_id, token):
        with self._transaction() as db:
            db.execute(
                "UPDATE recovery_jobs SET cleanup_token=NULL "
                "WHERE task_id=? AND proposal_id=? AND scope=? "
                "AND cleanup_token=?",
                (task_id, proposal_id, RECOVERY_SCOPE.get(), token),
            )

    def defer_unavailable_worker(self, task_id, proposal_id):
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM recovery_jobs WHERE task_id=? AND proposal_id=? AND scope=?",
                (task_id, proposal_id, RECOVERY_SCOPE.get()),
            ).fetchone()
            require(row["state"] in ("pending", "backoff"), "cannot defer active job")
            policy, now = self._recovery_policy(db, row), self.recovery_now()
            attempt = row["attempt"] + 1
            state = "exhausted" if attempt >= policy.maximum_attempts else "backoff"
            db.execute(
                "UPDATE recovery_jobs SET attempt=?, fence=fence+1, state=?, available_at_ms=? "
                "WHERE job_id=?",
                (attempt, state, now + policy.delay_ms(row["job_id"], attempt), row["job_id"]),
            )
            db.execute(
                "INSERT INTO recovery_attempts "
                "VALUES (?, ?, NULL, NULL, ?, ?, ?, 'no_healthy_worker', NULL)",
                (row["job_id"], attempt, row["fence"] + 1, now, now),
            )
            if state == "backoff":
                self._outbox(
                    db,
                    {**dict(row), "attempt": attempt},
                    "retry",
                    {"attempt": attempt, "reason": "no_healthy_worker"},
                )

    def terminal_recovery_job(
        self, task_id, proposal_id, receipt_ref, *, quarantined=False, blacklist=None, lease=None
    ):
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM recovery_jobs WHERE task_id=? AND proposal_id=? AND scope=?",
                (task_id, proposal_id, RECOVERY_SCOPE.get()),
            ).fetchone()
            if lease is not None:
                self._assert_recovery_fence(db, lease)
            if row["outcome_key"] is not None:
                return False
            self._artifact(db, task_id, receipt_ref, "recovery-outcome", owner=proposal_id)
            db.execute(
                "UPDATE recovery_jobs SET state=?, outcome_key=?, fence=fence+1, "
                "lease_expires_ms=NULL WHERE job_id=?",
                ("quarantined" if quarantined else "exhausted", receipt_ref.key, row["job_id"]),
            )
            if row["state"] == "leased":
                self._release_worker(db, row, self._recovery_policy(db, row), False)
                db.execute(
                    "UPDATE recovery_attempts SET status=?, finished_ms=?, evidence_key=? "
                    "WHERE job_id=? AND attempt=?",
                    (
                        "quarantined" if quarantined else "terminal",
                        self.recovery_now(),
                        receipt_ref.key,
                        row["job_id"],
                        row["attempt"],
                    ),
                )
            if blacklist is not None:
                record_ref = self.artifacts.put(canonical(blacklist))
                self._artifact(db, task_id, record_ref, "blacklist", owner=proposal_id)
                db.execute(
                    "INSERT INTO recovery_blacklist VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "record_key=excluded.record_key, expires_ms=excluded.expires_ms "
                    "WHERE recovery_blacklist.expires_ms<=?",
                    (
                        blacklist["key"],
                        record_ref.key,
                        blacklist["expires_ms"],
                        self.recovery_now(),
                    ),
                )
                self._outbox(
                    db,
                    row,
                    "blacklist",
                    {
                        "record": {
                            "key": record_ref.key,
                            "sha256": record_ref.sha256,
                            "size_bytes": record_ref.size_bytes,
                        }
                    },
                )
            self._outbox(
                db,
                row,
                "deadletter",
                {
                    "reason": json.loads(self.artifacts.get(receipt_ref))["reason"],
                    "receipt_key": receipt_ref.key,
                },
            )
        return True

    def local_blacklist(self, key):
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT record_key FROM recovery_blacklist WHERE key=? AND expires_ms>?",
                (key, self.recovery_now()),
            ).fetchone()
            return None if row is None else json.loads(self.artifacts.get(self._ref(db, row[0])))

    def recovery_outbox(self, task_id, *, pending=True):
        with self._transaction(write=False) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT o.*, j.task_id, j.proposal_id, j.scope, j.state AS job_state "
                    "FROM recovery_outbox o JOIN recovery_jobs j ON j.job_id=o.job_id "
                    "WHERE j.task_id=? "
                    "AND (?=0 OR o.sent_ms IS NULL) ORDER BY o.rowid",
                    (task_id, int(pending)),
                )
            ]

    def mark_recovery_event_sent(self, event_id):
        with self._transaction() as db:
            db.execute(
                "UPDATE recovery_outbox SET sent_ms=COALESCE(sent_ms, ?) WHERE event_id=?",
                (self.recovery_now(), event_id),
            )

    def recovery_outcome(self, task_id, proposal_id):
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT outcome_key FROM recovery_jobs "
                "WHERE task_id=? AND proposal_id=? AND scope=?",
                (task_id, proposal_id, RECOVERY_SCOPE.get()),
            ).fetchone()
            return (
                None
                if row is None or row[0] is None
                else json.loads(self.artifacts.get(self._ref(db, row[0])))
            )

    def managed_benchmark_retry_allowed(self, db, task_id, proposal_id, job_id):
        row = db.execute(
            "SELECT * FROM recovery_jobs WHERE job_id=?",
            ((EXECUTION_LEASE.get() or {}).get("job_id"),),
        ).fetchone()
        if row is None:
            return False
        self._assert_managed_execution(db, task_id, proposal_id)
        records = db.execute(
            "SELECT a.* FROM artifacts a JOIN benchmark_runs b ON b.report_key=a.key "
            "WHERE b.task_id=? AND b.job_id=?",
            (task_id, job_id),
        ).fetchall()
        for record in records:
            report = json.loads(self.artifacts.get(pb.ArtifactRef(**dict(record))))
            raw = json.loads(self.artifacts.get(pb.ArtifactRef(**report["raw"])))
            if raw["status"] != "failed" or raw.get("error", {}).get("kind") not in (
                "infrastructure",
                "measurement",
            ):
                return False
        return bool(records)
