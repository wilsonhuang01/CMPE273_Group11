"""Bounded recovery policy, scoped quarantine identities, and broker dispositions."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb


def infrastructure_failure(failure):
    return failure.reason in (pb.INFRASTRUCTURE_ERROR, pb.UNKNOWN_FAILURE) or (
        failure.stage == pb.COMPILE
        and failure.reason in (pb.SIGNAL_TERMINATION, pb.TIMEOUT, pb.MEMORY_LIMIT, pb.OUTPUT_LIMIT)
    )


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate recovery policy field")
        result[key] = value
    return result


@dataclass(frozen=True)
class RecoveryPolicy:
    schema_version: int = 1
    maximum_attempts: int = 3
    lease_seconds: float = 15.0
    heartbeat_seconds: float = 2.0
    worker_health_seconds: float = 10.0
    attempt_timeout_seconds: float = 3600.0
    backoff_seconds: float = 1.0
    maximum_backoff_seconds: float = 30.0
    jitter_fraction: float = 0.2
    worker_cooldown_seconds: float = 5.0
    cleanup_grace_seconds: float = 5.0
    blacklist_ttl_seconds: int = 604800

    @classmethod
    def parse(cls, data):
        if isinstance(data, bytes | str):
            require(len(data) <= 16384, "recovery policy exceeds 16 KiB")
            data = json.loads(data, object_pairs_hook=_unique)
        require(
            isinstance(data, dict) and set(data) == set(asdict(cls())),
            "recovery policy has missing/unknown fields",
        )
        for key, low, high in (
            ("schema_version", 1, 1),
            ("maximum_attempts", 2, 5),
            ("blacklist_ttl_seconds", 60, 2592000),
        ):
            require(
                type(data[key]) is int and low <= data[key] <= high,
                "invalid recovery integer: " + key,
            )
        for key, low, high in (
            ("lease_seconds", 0.2, 300),
            ("heartbeat_seconds", 0.05, 30),
            ("worker_health_seconds", 0.2, 300),
            ("attempt_timeout_seconds", 0.2, 86400),
            ("backoff_seconds", 0.01, 60),
            ("maximum_backoff_seconds", 0.01, 300),
            ("jitter_fraction", 0, 0.5),
            ("worker_cooldown_seconds", 0.01, 300),
            ("cleanup_grace_seconds", 0.01, 60),
        ):
            value = data[key]
            require(
                type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
                "invalid recovery number: " + key,
            )
        require(
            data["heartbeat_seconds"] * 3
            <= min(data["lease_seconds"], data["worker_health_seconds"]),
            "heartbeats must be at least three times faster than expiry",
        )
        require(data["backoff_seconds"] <= data["maximum_backoff_seconds"], "invalid backoff range")
        return cls(**data)

    def encode(self):
        encoded = canonical(asdict(self))
        self.parse(encoded)
        return encoded

    def delay_ms(self, job_id, attempt):
        unit = int(hashlib.sha256(f"{job_id}:{attempt}".encode()).hexdigest()[:8], 16) / (2**32 - 1)
        nominal = self.backoff_seconds * 2 ** max(0, attempt - 1)
        seconds = min(
            self.maximum_backoff_seconds,
            nominal * (1 - self.jitter_fraction + 2 * self.jitter_fraction * unit),
        )
        return max(1, math.ceil(seconds * 1000))


def quarantine_key(transformation, source_sha256, context):
    identity = {
        "transformation": transformation,
        "source_sha256": source_sha256,
        "context_sha256": digest(context),
    }
    return "compiler-optimizer:blacklist:v1:" + digest(identity)


class RetryDelivery(Exception):
    def __init__(self, delay_ms, event_id, confirmed=None):
        super().__init__("recovery retry scheduled")
        self.delay_ms, self.event_id = max(1, int(delay_ms)), event_id
        self.confirmed = confirmed or (lambda: None)


@dataclass
class DeadLetterResult:
    result: dict
    reason: str
    event_id: str
    confirmed: Callable = field(default=lambda: None, repr=False)


class RecoveryTerminal(Exception):
    def __init__(self, receipt):
        super().__init__(receipt["reason"])
        self.receipt = receipt


class RetryableExecution(Exception):
    def __init__(self, reason, evidence=None):
        super().__init__(reason)
        self.evidence = evidence or {}


class ProgramCrash(Exception):
    def __init__(self, signature, evidence):
        super().__init__("program crash observed")
        self.signature, self.evidence = signature, evidence
