"""Durable cohort barriers and engine-wide remote measurement admission."""

import json
from uuid import uuid4

from compiler_optimizer.scheduler.resources import Capacity
from compiler_optimizer.shared.fleet import FRESH_MS
from compiler_optimizer.shared.lease_context import EXECUTION_LEASE
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import StateError


class ConcurrencyStorage:
    def begin_cohort_work(self, task_id, workers, memory_mib):
        with self._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO cohort_runs(task_id,workers,reservation_mib) VALUES(?,?,?)",
                (task_id, workers, memory_mib),
            )
            session = self.get_selection_session(task_id)
            for member in session["members"]:
                db.execute(
                    "INSERT OR IGNORE INTO cohort_work VALUES(?,?,'pending')",
                    (task_id, member["proposal_id"]),
                )
        return self.cohort_work(task_id)

    def cohort_work(self, task_id):
        with self._transaction(write=False) as db:
            run = db.execute("SELECT * FROM cohort_runs WHERE task_id=?", (task_id,)).fetchone()
            if run is None:
                return None
            return {
                **dict(run),
                "members": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM cohort_work WHERE task_id=? ORDER BY rowid", (task_id,)
                    )
                ],
            }

    def checkpoint_cohort_member(self, task_id, proposal_id, state):
        require(
            state in ("preparing", "ready", "completed", "cancelled"), "invalid cohort work state"
        )
        with self._transaction() as db:
            db.execute(
                "UPDATE cohort_work SET state=? WHERE task_id=? AND proposal_id=? "
                "AND state NOT IN ('completed','cancelled')",
                (state, task_id, proposal_id),
            )

    def checkpoint_cohort(self, task_id, state):
        require(state in ("measuring", "completed", "cancelled"), "invalid cohort phase")
        with self._transaction() as db:
            db.execute(
                "UPDATE cohort_runs SET state=? WHERE task_id=? AND state!='cancelled' "
                "AND (state!='cancelling' OR ?='cancelled')",
                (state, task_id, state),
            )

    def cancel_cohort(self, task_id):
        """Stop queued work; active bounded attempts drain before cancellation commits."""
        with self._transaction() as db:
            require(
                db.execute("SELECT 1 FROM cohort_runs WHERE task_id=?", (task_id,)).fetchone()
                is not None,
                "cohort work has not started",
            )
            db.execute(
                "UPDATE cohort_runs SET state='cancelling' WHERE task_id=? "
                "AND state NOT IN ('completed','cancelled') "
                "AND EXISTS(SELECT 1 FROM selection_sessions s WHERE s.task_id=cohort_runs.task_id "
                "AND s.outcome_key IS NULL)",
                (task_id,),
            )

    def request_measurement_window(self, task_id, engine_id, parent_token=None):
        token = uuid4().hex
        with self._transaction() as db:
            self._task(db, task_id, running=True)
            db.execute(
                "INSERT INTO measurement_windows VALUES(?,?,?,?,'waiting')",
                (token, engine_id, task_id, parent_token),
            )
        return token

    def enter_measurement_window(self, token):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM measurement_windows WHERE token=?", (token,)).fetchone()
            require(row is not None and row["state"] in ("waiting", "active"), "window is fenced")
            # A window never expires into availability while its sandboxes need cleanup.
            first = db.execute(
                "SELECT token FROM measurement_windows WHERE engine_id=? "
                "AND state!='released' ORDER BY rowid LIMIT 1",
                (row["engine_id"],),
            ).fetchone()
            busy = db.execute(
                "SELECT 1 FROM fleet_jobs WHERE engine_id=? "
                "AND state IN ('assigned','running','cleanup') LIMIT 1",
                (row["engine_id"],),
            ).fetchone()
            if first[0] != token or busy:
                return False
            db.execute("UPDATE measurement_windows SET state='active' WHERE token=?", (token,))
            return True

    def release_measurement_window(self, token):
        with self._transaction() as db:
            db.execute("UPDATE measurement_windows SET state='draining' WHERE token=?", (token,))
            self._drain_measurement_windows(db)

    def _drain_measurement_windows(self, db):
        # Parent lease loss/restart fences queued window work before admitting another owner.
        for row in db.execute(
            "SELECT * FROM measurement_windows WHERE state!='released'"
        ).fetchall():
            task = self._task(db, row["task_id"])
            parent_live = (
                row["parent_token"] is None
                or db.execute(
                    "SELECT 1 FROM recovery_jobs WHERE lease_token=? AND state='leased' "
                    "AND lease_expires_ms>? AND deadline_ms>?",
                    (row["parent_token"], self.recovery_now(), self.recovery_now()),
                ).fetchone()
            )
            if not parent_live or task["status"] != "running":
                db.execute(
                    "UPDATE measurement_windows SET state='draining' WHERE token=?", (row["token"],)
                )
                db.execute(
                    "UPDATE fleet_jobs SET state=CASE WHEN state='pending' THEN 'failed' "
                    "ELSE 'cleanup' END,error='window_cancelled' WHERE window_token=? "
                    "AND state IN ('pending','assigned','running')",
                    (row["token"],),
                )
            state = db.execute(
                "SELECT state FROM measurement_windows WHERE token=?", (row["token"],)
            ).fetchone()[0]
            busy = db.execute(
                "SELECT 1 FROM fleet_jobs WHERE window_token=? "
                "AND state IN ('pending','assigned','running','cleanup')",
                (row["token"],),
            ).fetchone()
            if state == "draining" and not busy:
                db.execute(
                    "UPDATE measurement_windows SET state='released' WHERE token=?", (row["token"],)
                )

    def _fleet_window_allows(self, db, row):
        windows = db.execute(
            "SELECT * FROM measurement_windows WHERE engine_id=? "
            "AND state!='released' ORDER BY rowid",
            (row["engine_id"],),
        ).fetchall()
        if row["window_token"]:
            return bool(
                windows
                and windows[0]["token"] == row["window_token"]
                and windows[0]["state"] == "active"
            )
        return not windows

    def _engine_resources_allow(self, db, row):
        capacities = []
        for profile in db.execute(
            "SELECT profile_json FROM fleet_workers WHERE engine_id=? AND last_seen_ms>=?",
            (row["engine_id"], self.fleet_now() - FRESH_MS),
        ):
            try:
                hardware = json.loads(json.loads(profile[0])["hardware"])
            except (ValueError, TypeError):
                capacities = []
                break  # Old profiles are restricted to one operation per engine.
            if not isinstance(hardware, dict) or "docker_engine" not in hardware:
                capacities = []
                break
            try:
                capacities.append(Capacity.parse(hardware))
            except ContractError:
                return False  # Known inadequate headroom is never treated as missing telemetry.
        capacity = (
            Capacity(min(c.cpus for c in capacities), min(c.memory_mib for c in capacities))
            if capacities
            else None
        )
        active = db.execute(
            "SELECT reservation_mib FROM fleet_jobs WHERE engine_id=? "
            "AND state IN ('assigned','running','cleanup')",
            (row["engine_id"],),
        ).fetchall()
        if capacity is None:
            return not active
        return (
            len(active) < capacity.cpus
            and sum(r[0] for r in active) + row["reservation_mib"] <= capacity.memory_mib
        )

    def begin_measurement(self, task_id, job_id, attempt, plan_ref):
        """One durable claim per candidate; an interrupted measurement cannot be resampled."""
        with self._transaction() as db:
            job, _, _ = self._verified_candidate(db, task_id, job_id, attempt)
            self._assert_managed_execution(db, task_id, job.proposal_id)
            claim = db.execute(
                "SELECT * FROM measurement_claims WHERE task_id=? AND proposal_id=?",
                (task_id, job.proposal_id),
            ).fetchone()
            policy = (
                self._recovery_policy(db, EXECUTION_LEASE.get()) if EXECUTION_LEASE.get() else None
            )
            maximum = policy.maximum_attempts if policy else 3
            historical_attempts = 0
            if claim is not None:
                if claim["state"] != "retryable":
                    raise StateError(
                        "measurement already completed or interrupted; cannot resample"
                    )
                require(claim["plan_key"] == plan_ref.key, "measurement plan is frozen")
                require(claim["attempts"] < maximum, "measurement retry budget exhausted")
            else:
                # Existing pre-migration reports retain the same one-shot policy.
                for record in db.execute(
                    "SELECT b.report_key FROM benchmark_runs b JOIN jobs j "
                    "ON j.job_id=b.job_id AND j.attempt=b.attempt WHERE b.task_id=?",
                    (task_id,),
                ).fetchall():
                    report = json.loads(self.artifacts.get(self._ref(db, record[0])))
                    stored = self._job(db, report["job_id"], report["attempt"])
                    if stored.proposal_id != job.proposal_id:
                        continue
                    historical_attempts += 1
                    raw = json.loads(self.artifacts.get(pb.ArtifactRef(**report["raw"])))
                    if raw["status"] != "failed" or raw.get("error", {}).get("kind") not in (
                        "infrastructure",
                        "measurement",
                    ):
                        raise StateError("measurement already completed; cannot resample")
                    require(report["plan"]["key"] == plan_ref.key, "measurement plan is frozen")
                require(historical_attempts < maximum, "measurement retry budget exhausted")
            prior_attempts = claim["attempts"] if claim is not None else historical_attempts
            self._artifact(db, task_id, plan_ref, "measurement-plan", owner=job.proposal_id)
            token = (EXECUTION_LEASE.get() or {}).get("lease_token")
            db.execute(
                "INSERT INTO measurement_claims VALUES(?,?,?,?,?,?,?,'running') "
                "ON CONFLICT(task_id,proposal_id) DO UPDATE SET job_id=excluded.job_id, "
                "attempt=excluded.attempt,lease_token=excluded.lease_token, "
                "attempts=excluded.attempts,state='running'",
                (
                    task_id,
                    job.proposal_id,
                    job_id,
                    attempt,
                    plan_ref.key,
                    token,
                    prior_attempts + 1,
                ),
            )

    def _commit_measurement_claim(self, db, job, report, raw):
        claim = db.execute(
            "SELECT * FROM measurement_claims WHERE task_id=? AND proposal_id=?",
            (report["task_id"], job.proposal_id),
        ).fetchone()
        if claim is None:
            return  # Historical/imported evidence continues through the existing strict validators.
        require(
            claim["state"] == "running"
            and claim["job_id"] == job.job_id
            and claim["attempt"] == job.attempt
            and claim["plan_key"] == report["plan"]["key"]
            and claim["lease_token"] == (EXECUTION_LEASE.get() or {}).get("lease_token"),
            "measurement claim is completed or fenced",
        )
        retryable = raw["status"] == "failed" and raw.get("error", {}).get("kind") in (
            "infrastructure",
            "measurement",
        )
        db.execute(
            "UPDATE measurement_claims SET state=? WHERE task_id=? AND proposal_id=?",
            ("retryable" if retryable else "completed", report["task_id"], job.proposal_id),
        )
