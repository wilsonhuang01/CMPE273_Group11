"""Coordinator-owned primitive work leases. Workers never open this database."""

import hashlib
import hmac
import json
import time
from uuid import uuid4

from compiler_optimizer.shared.fleet import (
    FRESH_MS,
    LEASE_MS,
    canonical,
    decode_result,
    lease_from_row,
    validate_identity,
    validate_profile,
)
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import ConflictError, NotFoundError, StateError


class FleetStorage:
    def fleet_now(self):
        return getattr(self, "fleet_clock_ms", lambda: time.time_ns() // 1_000_000)()

    def register_fleet_worker(self, identity, profile_json):
        validate_identity(identity)
        profile = validate_profile(profile_json)
        encoded = canonical(profile).decode()
        secret_hash = hashlib.sha256(identity.secret.encode()).hexdigest()
        with self._transaction() as db:
            prior = db.execute(
                "SELECT * FROM fleet_workers WHERE worker_id=?", (identity.worker_id,)
            ).fetchone()
            if prior and prior["incarnation_id"] == identity.incarnation_id:
                if prior["secret_hash"] != secret_hash or prior["profile_json"] != encoded:
                    raise ConflictError("worker registration changed within an incarnation")
                return
            db.execute(
                "INSERT INTO fleet_workers VALUES (?, ?, ?, ?, ?, 0, ?, NULL) "
                "ON CONFLICT(worker_id) DO UPDATE SET incarnation_id=excluded.incarnation_id, "
                "secret_hash=excluded.secret_hash, profile_json=excluded.profile_json, "
                "engine_id=excluded.engine_id, sequence=0, "
                "last_seen_ms=excluded.last_seen_ms, active_job=NULL",
                (
                    identity.worker_id,
                    identity.incarnation_id,
                    secret_hash,
                    encoded,
                    profile["context"]["hardware"]["engine_id"],
                    self.fleet_now(),
                ),
            )

    def _fleet_worker(self, db, identity):
        validate_identity(identity)
        row = db.execute(
            "SELECT * FROM fleet_workers WHERE worker_id=?", (identity.worker_id,)
        ).fetchone()
        if (
            row is None
            or row["incarnation_id"] != identity.incarnation_id
            or not hmac.compare_digest(
                row["secret_hash"], hashlib.sha256(identity.secret.encode()).hexdigest()
            )
        ):
            raise PermissionError("worker identity rejected")
        return row

    def _fleet_authority(self, db, identity, lease):
        worker = self._fleet_worker(db, identity)
        row = db.execute("SELECT * FROM fleet_jobs WHERE job_id=?", (lease.job_id,)).fetchone()
        if (
            row is None
            or row["state"] != "running"
            or row["worker_id"] != identity.worker_id
            or (
                row["incarnation_id"] != identity.incarnation_id
                or row["token"] != lease.token
                or row["fence"] != lease.fence
                or row["expires_ms"] <= self.fleet_now()
                or row["deadline_ms"] <= self.fleet_now()
                or worker["last_seen_ms"] < self.fleet_now() - FRESH_MS
            )
        ):
            raise StateError("worker execution lease expired or fenced")
        self._task(db, row["task_id"], running=True)
        if row["parent_token"]:
            parent = db.execute(
                "SELECT 1 FROM recovery_jobs WHERE task_id=? AND lease_token=? AND state='leased' "
                "AND lease_expires_ms>? AND deadline_ms>?",
                (row["task_id"], row["parent_token"], self.recovery_now(), self.recovery_now()),
            ).fetchone()
            if parent is None:
                raise StateError("parent execution lease expired or fenced")
        if row["window_token"] and not self._fleet_window_allows(db, row):
            raise StateError("measurement window expired or fenced")
        return row

    def fleet_profiles(self, *, fresh=True):
        with self._transaction(write=False) as db:
            rows = db.execute("SELECT * FROM fleet_workers ORDER BY worker_id").fetchall()
        return [
            dict(row)
            for row in rows
            if not fresh or row["last_seen_ms"] >= self.fleet_now() - FRESH_MS
        ]

    def enqueue_fleet_job(self, task_id, worker, payload, refs, parent_token=None):
        operation = payload["operation"]
        require(operation in {"compile", "execute", "environment"}, "unsupported worker operation")
        memory_mib = payload.get("reservation_mib", 128)
        require(type(memory_mib) is int and 1 <= memory_mib <= 65536, "invalid memory reservation")
        window_token = payload.get("window_token")
        job_id = str(uuid4())
        input_ref = self.artifacts.put(canonical(payload))
        with self._transaction() as db:
            self._task(db, task_id, running=True)
            for ref in [input_ref, *refs]:
                self._artifact(db, task_id, ref, "worker-input", owner="fleet")
            if window_token:
                window = db.execute(
                    "SELECT * FROM measurement_windows WHERE token=?", (window_token,)
                ).fetchone()
                require(
                    window is not None
                    and window["state"] == "active"
                    and window["engine_id"] == worker["engine_id"]
                    and window["task_id"] == task_id
                    and window["parent_token"] == parent_token,
                    "invalid measurement authority",
                )
            db.execute(
                "INSERT INTO "
                "fleet_jobs(job_id,task_id,worker_id,incarnation_id,engine_id,parent_token,"
                "input_key,operation,token,deadline_ms,window_token,reservation_mib) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    task_id,
                    worker["worker_id"],
                    worker["incarnation_id"],
                    worker["engine_id"],
                    parent_token,
                    input_ref.key,
                    operation,
                    uuid4().hex,
                    self.fleet_now() + payload["timeout_seconds"] * 1000,
                    window_token,
                    memory_mib,
                ),
            )
        return job_id

    def fleet_job(self, job_id):
        with self._transaction(write=False) as db:
            row = db.execute("SELECT * FROM fleet_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError("worker job not found")
        return dict(row)

    def schedule_fleet(self):
        now = self.fleet_now()
        with self._transaction() as db:
            # Expired authority cannot be resurrected. Cleanup must precede retry/fallback.
            db.execute(
                "UPDATE fleet_jobs SET state='cleanup', error='lease_lost' "
                "WHERE state IN ('assigned','running') AND (expires_ms<=? OR deadline_ms<=? "
                "OR NOT EXISTS(SELECT 1 FROM fleet_workers w WHERE "
                "w.worker_id=fleet_jobs.worker_id "
                "AND w.incarnation_id=fleet_jobs.incarnation_id AND w.last_seen_ms>=?))",
                (now, now, now - FRESH_MS),
            )
            db.execute(
                "UPDATE fleet_jobs SET state='failed',error='worker_unavailable' "
                "WHERE state='pending' AND deadline_ms<=?",
                (now,),
            )
            self._drain_measurement_windows(db)
            for row in db.execute(
                "SELECT * FROM fleet_jobs WHERE state='pending' ORDER BY rowid LIMIT 32"
            ).fetchall():
                worker = db.execute(
                    "SELECT * FROM fleet_workers WHERE worker_id=? AND incarnation_id=? AND "
                    "last_seen_ms>=?",
                    (row["worker_id"], row["incarnation_id"], now - FRESH_MS),
                ).fetchone()
                if (
                    worker is None
                    or worker["active_job"] is not None
                    or not self._fleet_window_allows(db, row)
                    or not self._engine_resources_allow(db, row)
                ):
                    continue
                db.execute(
                    "UPDATE fleet_jobs SET state='assigned',expires_ms=? WHERE job_id=?",
                    (min(now + LEASE_MS, row["deadline_ms"]), row["job_id"]),
                )
                db.execute(
                    "UPDATE fleet_workers SET active_job=? WHERE worker_id=?",
                    (row["job_id"], row["worker_id"]),
                )
            rows = db.execute(
                "SELECT * FROM fleet_jobs WHERE state='assigned' AND published_ms<=? LIMIT 32",
                (now - 1000,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE fleet_jobs SET published_ms=? WHERE job_id=?", (now, row["job_id"])
                )
            return [dict(row) for row in rows]

    def fleet_heartbeat(self, request):
        with self._transaction() as db:
            worker = self._fleet_worker(db, request.identity)
            if request.sequence < worker["sequence"] or request.sequence == 0:
                raise ConflictError("stale worker heartbeat sequence")
            fresh = request.sequence > worker["sequence"]
            if fresh:
                db.execute(
                    "UPDATE fleet_workers SET sequence=?,last_seen_ms=? WHERE worker_id=?",
                    (request.sequence, self.fleet_now(), request.identity.worker_id),
                )
            valid = False
            if request.HasField("active"):
                try:
                    row = self._fleet_authority(db, request.identity, request.active)
                    valid = True
                    if fresh:
                        db.execute(
                            "UPDATE fleet_jobs SET expires_ms=? WHERE job_id=?",
                            (min(self.fleet_now() + LEASE_MS, row["deadline_ms"]), row["job_id"]),
                        )
                except StateError:
                    pass
            cleanup = db.execute(
                "SELECT * FROM fleet_jobs WHERE state='cleanup' AND engine_id=? LIMIT 8",
                (worker["engine_id"],),
            ).fetchall()
            return pb.FleetPulse(
                lease_valid=valid, cleanup=[lease_from_row(row) for row in cleanup]
            )

    def claim_fleet(self, request):
        with self._transaction() as db:
            worker = self._fleet_worker(db, request.identity)
            row = db.execute(
                "SELECT * FROM fleet_jobs WHERE job_id=?", (request.dispatch.job_id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "assigned"
                or (
                    row["worker_id"] != request.identity.worker_id
                    or row["incarnation_id"] != request.identity.incarnation_id
                    or request.dispatch.worker_id != request.identity.worker_id
                    or request.dispatch.fence != row["fence"]
                    or row["expires_ms"] <= self.fleet_now()
                    or worker["last_seen_ms"] < self.fleet_now() - FRESH_MS
                )
            ):
                return pb.FleetAssignment(accepted=False)
            db.execute("UPDATE fleet_jobs SET state='running' WHERE job_id=?", (row["job_id"],))
            ref = self._ref(db, row["input_key"])
            return pb.FleetAssignment(
                accepted=True, task_id=row["task_id"], lease=lease_from_row(row), input=ref
            )

    def check_fleet_authority(self, identity, lease):
        with self._transaction(write=False) as db:
            self._fleet_authority(db, identity, lease)

    def commit_fleet_result(self, identity, lease, data):
        with self._transaction() as db:
            row = self._fleet_authority(db, identity, lease)
            decoded = decode_result(data, row["operation"])
            if not isinstance(decoded, dict):
                value = json.loads(self.artifacts.get(self._ref(db, row["input_key"])))
                require(
                    decoded.build_profile == value["build_profile"], "worker changed build profile"
                )
                require(
                    len(decoded.stdout) + len(decoded.stderr) <= value["output_bytes"],
                    "uncapped worker output",
                )
                if row["operation"] == "execute":
                    require(
                        decoded.sandbox["cpu_id"] == value["cpu_id"], "worker changed CPU affinity"
                    )
            elif "environment" in decoded:
                require(
                    decoded["environment"].get("engine_id") == row["engine_id"],
                    "worker engine changed",
                )
            ref = self.artifacts.put(data)
            self._artifact(db, row["task_id"], ref, "worker-result", owner=row["job_id"])
            db.execute(
                "UPDATE fleet_jobs SET state='completed',result_key=? WHERE job_id=?",
                (ref.key, row["job_id"]),
            )
            db.execute(
                "UPDATE fleet_workers SET active_job=NULL WHERE worker_id=? AND active_job=?",
                (identity.worker_id, row["job_id"]),
            )

    def confirm_fleet_cleanup(self, identity, lease):
        with self._transaction() as db:
            worker = self._fleet_worker(db, identity)
            row = db.execute("SELECT * FROM fleet_jobs WHERE job_id=?", (lease.job_id,)).fetchone()
            if (
                row is None
                or row["state"] != "cleanup"
                or row["token"] != lease.token
                or (
                    row["fence"] != lease.fence
                    or worker["engine_id"] != row["engine_id"]
                    or worker["last_seen_ms"] < self.fleet_now() - FRESH_MS
                )
            ):
                raise StateError("cleanup receipt does not match a fenced job/engine")
            db.execute(
                "UPDATE fleet_jobs SET state='failed',fence=fence+1 WHERE job_id=?",
                (row["job_id"],),
            )
            db.execute(
                "UPDATE fleet_workers SET active_job=NULL WHERE active_job=?", (row["job_id"],)
            )

    def cancel_fleet_parent(self, token):
        with self._transaction() as db:
            db.execute(
                "UPDATE fleet_jobs SET state='failed',error='cancelled' WHERE "
                "parent_token=? AND state='pending'",
                (token,),
            )
            db.execute(
                "UPDATE fleet_jobs SET state='cleanup',error='cancelled' WHERE "
                "parent_token=? AND state IN ('assigned','running')",
                (token,),
            )
            count = db.execute(
                "SELECT COUNT(*) FROM fleet_jobs WHERE parent_token=? AND state='cleanup'", (token,)
            ).fetchone()[0]
        return count == 0

    def cancel_fleet_task(self, task_id):
        with self._transaction() as db:
            db.execute(
                "UPDATE measurement_windows SET state='draining' WHERE task_id=? "
                "AND state!='released'",
                (task_id,),
            )
            db.execute(
                "UPDATE fleet_jobs SET state='failed',error='coordinator_restart' "
                "WHERE task_id=? AND state='pending'",
                (task_id,),
            )
            db.execute(
                "UPDATE fleet_jobs SET state='cleanup',error='coordinator_restart' "
                "WHERE task_id=? AND state IN ('assigned','running')",
                (task_id,),
            )
            return (
                db.execute(
                    "SELECT COUNT(*) FROM fleet_jobs WHERE task_id=? AND state='cleanup'",
                    (task_id,),
                ).fetchone()[0]
                == 0
            )

    def cancel_fleet_job(self, job_id):
        with self._transaction() as db:
            db.execute(
                "UPDATE fleet_jobs SET state=CASE WHEN state='pending' THEN 'failed' ELSE "
                "'cleanup' END, "
                "error='cancelled' WHERE job_id=? AND state IN ('pending','assigned','running')",
                (job_id,),
            )
