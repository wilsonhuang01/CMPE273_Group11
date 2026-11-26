"""Validate counters actually opened and scheduled by the trusted Linux launcher."""

import math

from compiler_optimizer.shared.manifest import require

EVENTS = ("cycles", "instructions", "task-clock", "context-switches", "page-faults", "energy-pkg")


def validate_counters(value):
    require(
        isinstance(value, dict) and set(value) == set(EVENTS), "missing perf capability evidence"
    )
    for name, event in value.items():
        require(isinstance(event, dict), "invalid perf event")
        require(event.get("status") in ("available", "unavailable"), "invalid perf availability")
        if event["status"] == "available" and name == "energy-pkg":
            require(
                event.get("scope") == "package-wide"
                and event.get("unit") == "microjoules"
                and type(event.get("count")) is int
                and event["count"] >= 0
                and type(event.get("joules")) in (float, int)
                and math.isfinite(event["joules"])
                and event["joules"] == event["count"] / 1e6
                and type(event.get("interval_seconds")) in (float, int)
                and math.isfinite(event["interval_seconds"])
                and event["interval_seconds"] > 0
                and isinstance(event.get("domains"), list)
                and 1 <= len(event["domains"]) <= 8,
                "invalid package energy diagnostic",
            )
        elif event["status"] == "available":
            require(
                type(event.get("count")) is int
                and event["count"] >= 0
                and type(event.get("time_enabled_ns")) is int
                and type(event.get("time_running_ns")) is int
                and 0 < event["time_running_ns"] <= event["time_enabled_ns"],
                "perf event was not scheduled",
            )
        else:
            require(
                event.get("count") is None
                and (
                    type(event.get("errno")) is int
                    and event["errno"] > 0
                    or isinstance(event.get("reason"), str)
                    and bool(event["reason"])
                ),
                "unavailable counters must explain why and cannot report zero",
            )
    return value
