"""Allowlisted JSON lifecycle events; never include submitted text or exception messages."""

import json
import logging
from datetime import UTC, datetime

LOGGER = logging.getLogger("compiler_optimizer.events")
FIELDS = {
    "task_id",
    "proposal_id",
    "job_id",
    "worker_id",
    "incarnation_id",
    "attempt",
    "event_id",
    "status",
    "reason",
    "role",
    "duration_seconds",
    "performance_gain",
    "memory_gain",
    "weighted_score",
    "weighted_lower_bound",
    "redelivered",
    "duplicate",
    "delay_ms",
    "measurements",
    "owner_id",
    "previous_owner",
    "fence",
    "error_class",
}


def emit(event, **fields):
    if set(fields) - FIELDS:
        raise ValueError("unknown structured event field")
    LOGGER.info(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                **fields,
            },
            sort_keys=True,
            allow_nan=False,
        )
    )


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("pika").setLevel(logging.CRITICAL)


def verification_committed(result):
    from compiler_optimizer.shared.v1 import contracts_pb2 as pb

    failed = result.WhichOneof("outcome") == "failure"
    emit(
        "verification_result",
        task_id=result.meta.task_id,
        job_id=result.job_id,
        attempt=result.attempt,
        status="failed" if failed else "passed",
        reason=pb.FailureReason.Name(result.failure.reason) if failed else "NONE",
    )
