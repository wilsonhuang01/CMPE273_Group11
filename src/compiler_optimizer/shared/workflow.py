"""Operator-owned defaults frozen when automatic submission execution is accepted."""

import json

from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.recovery import RecoveryPolicy
from compiler_optimizer.shared.selection import SelectionPolicy
from compiler_optimizer.workers.verification_plan import VerificationPlan


def default_workflow():
    return {
        "schema_version": 1,
        "execution_backend": "local",
        "agents": ["performance", "memory", "architecture"],
        "verification_plan": json.loads(VerificationPlan().encode()),
        "benchmark_plan": json.loads(BenchmarkPlan().encode()),
        "selection_policy": json.loads(SelectionPolicy().encode()),
        "recovery_policy": json.loads(RecoveryPolicy().encode()),
        "maximum_attempts": 3,
        "retry_seconds": 5,
    }


def validate_workflow(value):
    expected = set(default_workflow())
    require(
        isinstance(value, dict) and set(value) in (expected, expected - {"execution_backend"}),
        "invalid workflow",
    )
    require(
        value.get("execution_backend", "local") in {"local", "remote"}, "invalid execution backend"
    )
    require(
        type(value["schema_version"]) is int and value["schema_version"] == 1, "workflow version"
    )
    names = value["agents"]
    require(
        isinstance(names, list)
        and all(isinstance(name, str) for name in names)
        and 1 <= len(names) <= 3
        and len(set(names)) == len(names)
        and set(names) <= {"performance", "memory", "architecture"},
        "invalid workflow agents",
    )
    for key, maximum in (("maximum_attempts", 10), ("retry_seconds", 300)):
        require(type(value[key]) is int and 1 <= value[key] <= maximum, "invalid workflow limit")
    VerificationPlan.parse(value["verification_plan"])
    BenchmarkPlan.parse(value["benchmark_plan"])
    policy = SelectionPolicy.parse(value["selection_policy"])
    require(len(names) <= policy.maximum_candidates, "workflow exceeds cohort size")
    RecoveryPolicy.parse(value["recovery_policy"])
    return json.dumps(value, sort_keys=True, allow_nan=False)
