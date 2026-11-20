"""Collect frozen candidates and commit the independently recomputed weighted winner."""

from compiler_optimizer.monitoring.events import emit
from compiler_optimizer.shared.manifest import require


class SelectionCoordinator:
    def __init__(self, repository):
        self.repository = repository

    def choose(self, task_id):
        repository = self.repository
        cached = repository.selection_report(task_id)
        if cached is not None:
            return cached[1]
        audit = repository.preview_selection(task_id)
        require(audit["status"] != "pending", "selection cohort is not fully evaluated")
        if audit["status"] == "blocked":
            return repository.record_blocked_selection(task_id)
        repository.select_result(
            task_id,
            audit["selected_job_id"],
            audit["selected_attempt"],
            expected_revision=repository.get_task(task_id)["revision"],
            reason=audit["reason"],
            scores=repository.selection_scores(audit),
        )
        history = repository.history(task_id)
        emit(
            "task_completed",
            task_id=task_id,
            status=audit["status"],
            duration_seconds=(
                history[-1]["recorded_at_unix_ms"] - history[0]["recorded_at_unix_ms"]
            )
            / 1000,
            **repository.selection_scores(audit),
        )
        return repository.selection_report(task_id)[1]
