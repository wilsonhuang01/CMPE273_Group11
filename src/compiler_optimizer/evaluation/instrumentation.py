"""Shared hard budgets and durable costs across all workers of one evaluation arm."""

import json
import math
import threading
import time
from pathlib import Path

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    temporary.replace(path)


class BudgetExceeded(InfrastructureError):
    pass


class Budget:
    def __init__(self, path, *, maximum_invocations=256, maximum_reserved_seconds=1800):
        require(type(maximum_invocations) is int and maximum_invocations > 0, "invalid call budget")
        require(
            isinstance(maximum_reserved_seconds, (int, float))
            and not isinstance(maximum_reserved_seconds, bool)
            and math.isfinite(maximum_reserved_seconds)
            and maximum_reserved_seconds > 0,
            "invalid time budget",
        )
        self.path, self.maximum_invocations = Path(path), maximum_invocations
        self.maximum_reserved_seconds = maximum_reserved_seconds
        self.calls, self.lock = [], threading.Lock()

    def reserve(self, payload, compile_mode, trusted):
        seconds = payload.get("wall_seconds")
        require(
            type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0,
            "invalid reservation",
        )
        with self.lock:
            reserved = sum(c["reserved_seconds"] for c in self.calls)
            if len(self.calls) >= self.maximum_invocations or (
                reserved + payload["wall_seconds"] > self.maximum_reserved_seconds
            ):
                raise BudgetExceeded("shared arm sandbox budget exhausted")
            row = {
                "index": len(self.calls),
                "kind": "compile" if compile_mode else "metadata" if trusted else "program",
                "reserved_seconds": payload["wall_seconds"],
                "state": "started",
                "reported_wall_seconds": None,
                "reported_cpu_seconds": None,
            }
            self.calls.append(row)
            write_json(self.path, self.calls)
            return row

    def finish(self, row, started, result, error):
        with self.lock:
            row.update(
                state="completed" if error is None else "infrastructure_failure",
                elapsed_seconds=time.monotonic() - started,
                failure=type(error).__name__ if error else result.failure,
                reported_wall_seconds=None if result is None else result.wall_seconds,
                reported_cpu_seconds=None if result is None else result.cpu_seconds,
            )
            write_json(self.path, self.calls)

    def summary(self):
        return {
            "invocations": len(self.calls),
            "compile_invocations": sum(c["kind"] == "compile" for c in self.calls),
            "program_invocations": sum(c["kind"] == "program" for c in self.calls),
            "metadata_invocations": sum(c["kind"] == "metadata" for c in self.calls),
            "reserved_seconds": sum(c["reserved_seconds"] for c in self.calls),
            "sandbox_reported_wall_seconds_lower_bound": sum(
                c["reported_wall_seconds"] or 0 for c in self.calls
            ),
            "container_call_elapsed_seconds": sum(c.get("elapsed_seconds", 0) for c in self.calls),
            "calls_without_reported_wall_time": sum(
                c["reported_wall_seconds"] is None for c in self.calls
            ),
            "reported_child_cpu_seconds_lower_bound": sum(
                c["reported_cpu_seconds"] or 0 for c in self.calls
            ),
            "external_model_calls": 0,
            "model_tokens": None,
            "monetary_cost_usd": None,
        }


class MeteredRunner(DockerRunner):
    def __init__(self, *args, budget, inject_compiler_disconnect=False, **kwargs):
        self.budget = budget
        self.inject_compiler_disconnect = inject_compiler_disconnect
        super().__init__(*args, **kwargs)

    def _invoke(self, payload, *, memory, compile_mode, cpu_id=None, trusted_command=None):
        row = self.budget.reserve(payload, compile_mode, trusted_command is not None)
        started, result, error = time.monotonic(), None, None
        try:
            if compile_mode and self.inject_compiler_disconnect:
                self.inject_compiler_disconnect = False
                raise InfrastructureError("injected compiler transport disconnect")
            result = super()._invoke(
                payload,
                memory=memory,
                compile_mode=compile_mode,
                cpu_id=cpu_id,
                trusted_command=trusted_command,
            )
            return result
        except BaseException as caught:
            error = caught
            raise
        finally:
            self.budget.finish(row, started, result, error)


class ScopedRedis:
    """Isolate experiment blacklist caches without changing record identities or TTLs."""

    def __init__(self, client, prefix):
        require(prefix.startswith("evaluation:"), "invalid evaluation cache prefix")
        self.client, self.prefix = client, prefix

    def get(self, key):
        return self.client.get(self.prefix + key)

    def set(self, key, value, **kwargs):
        return self.client.set(self.prefix + key, value, **kwargs)
