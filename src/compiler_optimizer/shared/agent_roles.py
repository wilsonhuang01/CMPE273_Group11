"""Operator-owned role contracts and per-task invocation budgets."""

from dataclasses import dataclass

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb


@dataclass(frozen=True)
class RoleSpec:
    name: str
    purpose: str
    inputs: tuple[str, ...]
    output: str
    max_iterations: int
    objective: str | None = None


ROLES = {
    pb.PERFORMANCE: RoleSpec(
        "performance",
        "Reduce runtime",
        ("assignment", "source"),
        "structured source patch and rationale",
        2,
        "runtime",
    ),
    pb.MEMORY: RoleSpec(
        "memory",
        "Reduce peak resident memory",
        ("assignment", "source"),
        "structured source patch and rationale",
        2,
        "memory",
    ),
    pb.ARCHITECTURE: RoleSpec(
        "architecture",
        "Improve portable x86-64 execution",
        ("assignment", "source", "fixed target/compiler"),
        "structured source patch and rationale",
        2,
        "runtime",
    ),
    pb.CORRECTNESS: RoleSpec(
        "correctness",
        "Review automated semantic-equivalence evidence",
        ("proposal", "verification job/result", "correctness artifacts"),
        "bound approve/reject review with findings",
        2,
    ),
    pb.CRITIC: RoleSpec(
        "critic",
        "Review changes and unsupported optimization assumptions",
        ("proposal", "parent/candidate source", "patch", "verification result"),
        "bound approve/reject review with findings",
        2,
    ),
    pb.BENCHMARK: RoleSpec(
        "benchmark",
        "Freeze and organize controlled measurement work",
        ("verified proposal", "benchmark plan", "verified binaries"),
        "immutable measurement work specification",
        1,
    ),
}

REQUIRED_REVIEWS = (pb.CORRECTNESS, pb.CRITIC)


def role_spec(role):
    require(role in ROLES, "unsupported agent role")
    return ROLES[role]


def generation_role(name):
    matches = [role for role, spec in ROLES.items() if spec.name == name and spec.objective]
    require(len(matches) == 1, "role does not generate candidates")
    return matches[0]
