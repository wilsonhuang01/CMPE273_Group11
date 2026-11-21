"""Durable automatic-execution queue; the local pipeline lock owns dispatch authority."""

import json
import time

from compiler_optimizer.storage.errors import StateError


class ExecutionStorage:
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
        if stage not in {"baseline", "optimizing"}:
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
