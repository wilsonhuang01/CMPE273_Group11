"""Durable debate lineage, budgets, immutable feedback and one-shot held-out evaluation."""

import json
import time

from compiler_optimizer.shared.debate import DebatePolicy, revision_context
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.storage.errors import ConflictError, StateError


class DebateStorage:
    def _begin_debate(self, db, task_id, policy_ref, members):
        policy = DebatePolicy.parse(self.artifacts.get(policy_ref))
        require(len(members) % policy.max_rounds == 0, "invalid debate slot count")
        width = len(members) // policy.max_rounds
        now = int(time.time() * 1000)
        self._artifact(db, task_id, policy_ref, "debate-policy")
        db.execute(
            "INSERT INTO debate_runs(task_id,policy_key,started_ms,deadline_ms) VALUES(?,?,?,?)",
            (task_id, policy_ref.key, now, now + policy.max_seconds * 1000),
        )
        for i, member in enumerate(members):
            parent = members[i - width]["proposal_id"] if i >= width else None
            require(
                i < width or member["role"] == members[i - width]["role"],
                "revision must retain its generator role",
            )
            db.execute(
                "INSERT INTO debate_slots VALUES(?,?,?,?,NULL,NULL)",
                (task_id, member["proposal_id"], i // width + 1, parent),
            )

    def debate_run(self, task_id):
        with self._transaction(write=False) as db:
            row = db.execute("SELECT * FROM debate_runs WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return None
            return {
                **dict(row),
                "policy": json.loads(self.artifacts.get(self._ref(db, row["policy_key"]))),
                "slots": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM debate_slots WHERE task_id=? ORDER BY rowid", (task_id,)
                    )
                ],
            }

    def debate_limit(self, task_id):
        run = self.debate_run(task_id)
        if run is None:
            return None
        if int(time.time() * 1000) >= run["deadline_ms"]:
            return "time_budget_exhausted"
        if run["calls"] >= run["policy"]["max_agent_calls"]:
            return "call_budget_exhausted"
        return None

    def _debate_call(self, db, task_id):
        run = db.execute("SELECT * FROM debate_runs WHERE task_id=?", (task_id,)).fetchone()
        if run is None:
            return
        policy = DebatePolicy.parse(self.artifacts.get(self._ref(db, run["policy_key"])))
        if int(time.time() * 1000) >= run["deadline_ms"] or run["calls"] >= policy.max_agent_calls:
            raise StateError("debate call/time budget exhausted")
        db.execute("UPDATE debate_runs SET calls=calls+1 WHERE task_id=?", (task_id,))

    def debate_event(self, task_id, proposal_id, event, record):
        ref = self.artifacts.put(json.dumps(record, sort_keys=True, allow_nan=False).encode())
        with self._transaction() as db:
            old = db.execute(
                "SELECT record_key FROM debate_events "
                "WHERE task_id=? AND proposal_id=? AND event=?",
                (task_id, proposal_id, event),
            ).fetchone()
            if old:
                if old[0] != ref.key:
                    raise ConflictError("debate transcript is immutable")
                return
            self._artifact(db, task_id, ref, "debate-transcript", owner=proposal_id)
            db.execute(
                "INSERT INTO debate_events(task_id,proposal_id,event,record_key) VALUES(?,?,?,?)",
                (task_id, proposal_id, event, ref.key),
            )
            if event == "stopped":
                db.execute(
                    "UPDATE debate_slots SET reason=? WHERE task_id=? AND proposal_id=?",
                    (record["reason"], task_id, proposal_id),
                )

    def bind_debate_context(self, task_id, proposal_id, data):
        context = revision_context(data)
        ref = self.artifacts.put(data.encode())
        with self._transaction() as db:
            slot = db.execute(
                "SELECT * FROM debate_slots WHERE task_id=? AND proposal_id=?",
                (task_id, proposal_id),
            ).fetchone()
            require(
                slot is not None
                and slot["parent_id"] == context["parent_proposal_id"]
                and slot["round_number"] == context["round"],
                "revision lineage mismatch",
            )
            from compiler_optimizer.shared.v1 import contracts_pb2 as pb

            parent = self._load(db, "proposals", pb.Proposal, "proposal_id", slot["parent_id"])
            require(
                parent.meta.task_id == task_id
                and context["parent_source_sha256"] == parent.source.bundle.sha256,
                "revision parent source mismatch",
            )
            if slot["context_key"] is not None and slot["context_key"] != ref.key:
                raise ConflictError("revision feedback is frozen")
            self._artifact(db, task_id, ref, "revision-feedback", owner=proposal_id)
            db.execute(
                "UPDATE debate_slots SET context_key=? WHERE proposal_id=?", (ref.key, proposal_id)
            )
        return ref

    def debate_context(self, task_id, proposal_id):
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT context_key FROM debate_slots WHERE task_id=? AND proposal_id=?",
                (task_id, proposal_id),
            ).fetchone()
            return (
                None
                if row is None or row[0] is None
                else self.artifacts.get(self._ref(db, row[0])).decode()
            )

    def claim_debate_final(self, task_id, proposal_id):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM debate_runs WHERE task_id=?", (task_id,)).fetchone()
            if row["final_state"] is not None:
                require(row["final_proposal_id"] == proposal_id, "held-out candidate is frozen")
                return row["final_state"]
            db.execute(
                "UPDATE debate_runs SET final_state='running',final_proposal_id=? WHERE task_id=?",
                (proposal_id, task_id),
            )
            return "claimed"

    def finish_debate_final(self, task_id, report):
        require(
            isinstance(report, dict) and type(report.get("passed")) is bool,
            "invalid held-out report",
        )
        ref = self.artifacts.put(json.dumps(report, sort_keys=True, allow_nan=False).encode())
        with self._transaction() as db:
            row = db.execute(
                "SELECT final_report_key FROM debate_runs WHERE task_id=?", (task_id,)
            ).fetchone()
            require(row is not None, "unknown debate")
            if row[0] is not None:
                require(row[0] == ref.key, "held-out result is immutable")
                return
            run = db.execute("SELECT * FROM debate_runs WHERE task_id=?", (task_id,)).fetchone()
            require(run["final_state"] == "running", "held-out evaluation must be claimed")
            if report["passed"] and run["final_proposal_id"] is not None:
                policy = DebatePolicy.parse(self.artifacts.get(self._ref(db, run["policy_key"])))
                require(
                    report.get("proposal_id") == run["final_proposal_id"]
                    and len(report.get("cases", [])) == len(policy.heldout_cases)
                    and all(c.get("passed") is True for c in report["cases"]),
                    "incomplete held-out pass",
                )
            self._artifact(db, task_id, ref, "heldout-evaluation")
            db.execute(
                "UPDATE debate_runs SET final_state='completed',final_report_key=? WHERE task_id=?",
                (ref.key, task_id),
            )

    def debate_transcript(self, task_id):
        with self._transaction(write=False) as db:
            return [
                {
                    "proposal_id": r["proposal_id"],
                    "event": r["event"],
                    "record": json.loads(self.artifacts.get(self._ref(db, r["record_key"]))),
                }
                for r in db.execute(
                    "SELECT * FROM debate_events WHERE task_id=? ORDER BY sequence", (task_id,)
                )
            ]
