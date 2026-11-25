"""Independent, deterministic domain contracts; no submitted code runs here."""

from pathlib import Path

import pytest

from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.workers.domains import KINDS, DomainGenerator
from compiler_optimizer.workers.fuzz_policy import FuzzPolicy
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.verification_plan import VerificationPlan, generate_cases

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/correctness"


@pytest.mark.parametrize("kind", KINDS)
def test_domain_roundtrip_determinism_oracle(kind):
    domain = DomainGenerator(kind)
    policy = VerificationPlan(generator=domain)
    assert VerificationPlan.parse(policy.encode()) == policy
    a = generate_cases(read_package(EXAMPLES / kind), policy)
    assert a == generate_cases(read_package(EXAMPLES / kind), policy)
    assert all(c.expected is not None for c in a)
    assert len(a) == 9
    assert a[1].stdin == (
        b"\n" if kind == "reverse" else b"0\n" if kind != "search" else b"0 101\n"
    )


@pytest.mark.parametrize(
    "key,value",
    [("cases", 33), ("max_size", 17), ("bound", 1001), ("seed", True), ("kind", "command")],
)
def test_bad_domains(key, value):
    data = dict(kind="sort", seed=273, cases=8, max_size=8, bound=100)
    data[key] = value
    with pytest.raises(ContractError):
        DomainGenerator.parse(data)


@pytest.mark.parametrize(
    "key,value",
    [
        ("seconds", 31),
        ("runs", 100001),
        ("seed", True),
        ("max_length", 4097),
        ("minimize_seconds", 6),
    ],
)
def test_bad_fuzz_policy(key, value):
    with pytest.raises(ContractError):
        FuzzPolicy(**{key: value}).validate()


def test_hand_computed_oracles():
    # Assert independent results for edge cases, including empty reductions.
    for kind, expected in [
        ("sort", b"\n"),
        ("search", b"-1\n"),
        ("matrix", b"\n"),
        ("reverse", b"\n"),
        ("dot", b"0\n"),
        ("norm", b"0\n"),
    ]:
        assert next(DomainGenerator(kind).generate())[1] == expected


def test_coverage_plan_roundtrip_and_strict_policy():
    policy = VerificationPlan(coverage=FuzzPolicy())
    assert VerificationPlan.parse(policy.encode()) == policy
    data = policy.as_dict()
    data["coverage"]["unknown"] = 1
    with pytest.raises(ContractError):
        VerificationPlan.parse(data)
