"""Immutable, task-scoped compile-only evidence, separate from performance decisions."""

import json

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import ConflictError


class CUDAStorage:
    def cuda_record(self, task_id, owner):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            row = db.execute(
                "SELECT a.* FROM artifacts a JOIN task_artifacts t USING(key) "
                "WHERE t.task_id=? AND t.kind='cuda-report' AND t.owner_id=?",
                (task_id, owner),
            ).fetchone()
        if row is None:
            return None
        ref = pb.ArtifactRef(**{k: row[k] for k in ("key", "sha256", "size_bytes")})
        return ref, json.loads(self.artifacts.get(ref))

    def cuda_task_report(self, task_id):
        return self.cuda_record(task_id, "task")

    def save_cuda_record(self, task_id, owner, report, refs=(), *, final=False):
        """Commit evidence and, for the final audit, task completion in one transaction."""
        require(report["performance_qualified"] is False, "compile-only cannot qualify performance")
        ref = self.artifacts.put(json.dumps(report, sort_keys=True, allow_nan=False).encode())
        with self._transaction() as db:
            task = self._task(db, task_id)
            submission = self._submission(task)
            require(submission.manifest.language == "cuda17", "expected CUDA task")
            prior = db.execute(
                "SELECT key FROM task_artifacts "
                "WHERE task_id=? AND kind='cuda-report' AND owner_id=?",
                (task_id, owner),
            ).fetchone()
            if prior:
                if prior["key"] != ref.key:
                    raise ConflictError("compile-only evidence is immutable")
                return ref
            require(task["status"] == "running", "compile task must be running")
            for kind, artifact in refs:
                self._artifact(db, task_id, artifact, kind, owner=owner)
            self._artifact(db, task_id, ref, "cuda-report", owner=owner)
            if final:
                require(owner == "task", "invalid final compile report")
                status = "completed" if report["status"] == "compiled" else "failed"
                db.execute(
                    "UPDATE tasks SET status=?, revision=revision+1 WHERE task_id=?",
                    (status, task_id),
                )
                self._history(db, task_id, status, task["revision"] + 1, "CUDA compile-only audit")
        return ref
