"""Durable automatic-execution queue with database-backed dispatch claims.

A coordinator may execute a task only while it holds that task's claim: a random token
with an expiry, renewed while work runs. Claims are taken inside the repository's
serialized write transaction, so two coordinators can never hold the same task.
An expired claim can be taken over; each takeover increments the claim's fence. While
the dispatcher works, every repository write is checked against its claim (see
``Repository._assert_fences``), so a stalled or partitioned previous owner can no
longer commit anything once a replacement has taken over.
"""

import json
import time
from uuid import uuid4

from compiler_optimizer.shared.lease_context import DISPATCH_CLAIM
from compiler_optimizer.storage.errors import ClaimLost, StateError


class ExecutionStorage:
    def dispatch_now(self, db):
        """Milliseconds on the database clock, so coordinators on different hosts agree."""
        if getattr(self, "_postgres", None) is not None:
            return db.execute(
                "SELECT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint"
            ).fetchone()[0]
        return time.time_ns() // 1_000_000

    def claim_execution(self, owner_id, *, backend, lease_ms, max_active=1):
        """Claim the oldest due, unclaimed execution for ``backend``; None if none or full."""
        if not (isinstance(owner_id, str) and 0 < len(owner_id) <= 128):
            raise ValueError("owner_id must be a short nonempty string")
        if not (type(lease_ms) is int and 1 <= lease_ms <= 600_000):
            raise ValueError("lease_ms must be 1–600000")
        if not (type(max_active) is int and 1 <= max_active <= 8):
            raise ValueError("max_active must be 1–8")
        with self._transaction() as db:
            now = self.dispatch_now(db)
            live = db.execute(
                "SELECT COUNT(*) FROM dispatch_claims WHERE expires_ms>?", (now,)
            ).fetchone()[0]
            if live >= max_active:
                return None
            candidates = db.execute(
                "SELECT e.task_id, e.configuration_json, c.owner_id, c.fence, c.expires_ms "
                "FROM submission_executions e LEFT JOIN dispatch_claims c ON c.task_id=e.task_id "
                "WHERE e.finished=0 AND e.available_at_ms<=? "
                "AND (c.task_id IS NULL OR c.expires_ms<=?) ORDER BY e.rowid",
                (now, now),
            ).fetchall()
            row = next(
                (
                    row
                    for row in candidates
                    if json.loads(row["configuration_json"]).get("execution_backend", "local")
                    == backend
                ),
                None,
            )
            if row is None:
                return None
            claim = {
                "database": str(self.database),
                "task_id": row["task_id"],
                "owner_id": owner_id,
                "token": uuid4().hex,
                "fence": (row["fence"] or 0) + 1,
                "expires_ms": now + lease_ms,
                # An unreleased expired claim means its owner stopped without finishing.
                "previous_owner": row["owner_id"] if row["expires_ms"] else None,
            }
            db.execute(
                "INSERT INTO dispatch_claims VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET owner_id=excluded.owner_id, "
                "token=excluded.token, fence=excluded.fence, claimed_ms=excluded.claimed_ms, "
                "expires_ms=excluded.expires_ms",
                (
                    claim["task_id"],
                    owner_id,
                    claim["token"],
                    claim["fence"],
                    now,
                    claim["expires_ms"],
                ),
            )
        return claim

    def renew_execution_claim(self, claim, lease_ms):
        """Extend a live claim; False once it expired or another coordinator holds it."""
        authority = DISPATCH_CLAIM.set(None)  # Renewal is checked by its own predicate.
        try:
            with self._transaction() as db:
                now = self.dispatch_now(db)
                cursor = db.execute(
                    "UPDATE dispatch_claims SET expires_ms=? "
                    "WHERE task_id=? AND token=? AND expires_ms>?",
                    (now + lease_ms, claim["task_id"], claim["token"], now),
                )
                renewed = cursor.rowcount == 1
        finally:
            DISPATCH_CLAIM.reset(authority)
        if renewed:
            claim["expires_ms"] = now + lease_ms
        return renewed

    def release_execution_claim(self, claim):
        """Give up a claim; its fence is kept so later owners keep increasing it."""
        authority = DISPATCH_CLAIM.set(None)
        try:
            with self._transaction() as db:
                db.execute(
                    "UPDATE dispatch_claims SET expires_ms=0 WHERE task_id=? AND token=?",
                    (claim["task_id"], claim["token"]),
                )
        finally:
            DISPATCH_CLAIM.reset(authority)

    def dispatch_claims(self):
        with self._transaction(write=False) as db:
            return [dict(row) for row in db.execute("SELECT * FROM dispatch_claims ORDER BY rowid")]

    def _assert_dispatch_claim(self, db, claim):
        row = db.execute(
            "SELECT token, expires_ms FROM dispatch_claims WHERE task_id=?", (claim["task_id"],)
        ).fetchone()
        if (
            row is None
            or row["token"] != claim["token"]
            or row["expires_ms"] <= self.dispatch_now(db)
        ):
            raise ClaimLost("dispatch claim expired or was taken over")

    def get_execution(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            row = db.execute(
                "SELECT * FROM submission_executions WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["configuration"] = json.loads(result.pop("configuration_json"))
        return result

    def pending_executions(self):
        with self._transaction(write=False) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT task_id FROM submission_executions WHERE finished=0 "
                    "AND available_at_ms<=? ORDER BY rowid",
                    (time.time_ns() // 1_000_000,),
                )
            ]

    def start_execution(self, task_id):
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM submission_executions WHERE task_id=? AND finished=0", (task_id,)
            ).fetchone()
            if row is None:
                raise StateError("execution is already finished")
            maximum = json.loads(row["configuration_json"])["maximum_attempts"]
            if row["attempts"] >= maximum:
                return False
            db.execute(
                "UPDATE submission_executions SET attempts=attempts+1, error_code=NULL "
                "WHERE task_id=?",
                (task_id,),
            )
        return True

    def checkpoint_execution(self, task_id, stage):
        if stage not in {"baseline", "optimizing", "compiling"}:
            raise ValueError("invalid execution checkpoint")
        with self._transaction() as db:
            db.execute(
                "UPDATE submission_executions SET stage=? WHERE task_id=? AND finished=0",
                (stage, task_id),
            )

    def finish_execution(self, task_id, *, error_code=None, retry=False):
        with self._transaction() as db:
            task = self._task(db, task_id)
            row = db.execute(
                "SELECT * FROM submission_executions WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None or row["finished"]:
                return
            config = json.loads(row["configuration_json"])
            retry = retry and row["attempts"] < config["maximum_attempts"]
            terminal = task["status"] in {"completed", "failed"}
            failed = task["status"] == "failed"
            if terminal:
                retry = False
                if not failed:
                    error_code = None
            if error_code is not None and not retry and not terminal:
                db.execute(
                    "UPDATE tasks SET status='failed', revision=revision+1 WHERE task_id=?",
                    (task_id,),
                )
                self._history(db, task_id, "failed", task["revision"] + 1, error_code)
                terminal = True
                failed = True
            if not terminal and not retry:
                raise StateError("execution cannot finish without a final task outcome")
            db.execute(
                "UPDATE submission_executions SET stage=?, error_code=?, available_at_ms=?, "
                "finished=? WHERE task_id=?",
                (
                    ("failed" if failed else "completed") if terminal else "retry_wait",
                    error_code,
                    time.time_ns() // 1_000_000 + config["retry_seconds"] * 1000 if retry else 0,
                    int(terminal),
                    task_id,
                ),
            )
