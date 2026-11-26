"""Correctness gates, bounded generators, comparison policy, and persisted evidence."""

import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from compiler_optimizer.shared.contracts import BASE_FLAGS
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import InfrastructureError, Invocation
from compiler_optimizer.workers.package import read_package, unbundle
from compiler_optimizer.workers.verification_plan import (
    ArrayGenerator,
    VerificationPlan,
    generate_cases,
)
from compiler_optimizer.workers.verify import register_candidate, run_verification

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def plan():
    return VerificationPlan(generator=ArrayGenerator(273, 2, 3, 0, 8, -100, 100))


class Runner:
    supports_sanitizers = True
    hardware = "unit worker"

    def __init__(self, original):
        self.original = original
        self.mode = "good"
        self.events = []
        self.binaries = {}

    def compiler(self, language):
        return pb.CompilerSettings(
            executable="clang",
            version="18.1.3",
            language=language,
            target="linux-x86_64",
            image_digest="sha256:" + "a" * 64,
            flags=["-std=c17", *BASE_FLAGS],
        )

    def compile(self, package, *, build_profile="normal"):
        role = "baseline" if package.sources == self.original.sources else "candidate"
        self.events.append(("compile", role, build_profile))
        if self.mode == "infrastructure":
            raise InfrastructureError("daemon unavailable")
        binary = f"{role}:{build_profile}".encode()
        self.binaries[binary] = role
        return Invocation(b"", b"", 0.01, 1024, 0, binary=binary)

    def execute(self, binary, case, package, *, build_profile="normal"):
        role = self.binaries[binary]
        self.events.append(("execute", role, build_profile))
        values = list(map(int, package.files[case.stdin].split()))
        output = f"{sum(values[1:])}\n".encode()
        if self.mode == "wrong-generated" and role == "candidate" and values[0] > 6:
            output = b"wrong\n"
        if self.mode == "baseline-drift" and role == "baseline":
            output = b"changed\n"
        if build_profile != "normal" and (
            (self.mode == "candidate-ub" and role == "candidate")
            or (self.mode == "baseline-ub" and role == "baseline")
        ):
            return Invocation(
                b"", b"runtime error: signed integer overflow", 0.01, 1024, 1, "nonzero_exit"
            )
        return Invocation(output, b"", 0.01, 4096, 0)


@pytest.fixture
def scenario(tmp_path):
    original = read_package(EXAMPLES / "sum-array")
    repo = Repository(tmp_path / "state")
    runner = Runner(original)
    baseline = run_baseline(original, repo, runner)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "main.c").write_bytes(original.sources["main.c"] + b"\n/* proposal */\n")
    proposal_id = register_candidate(repo, baseline.meta.task_id, candidate)
    runner.events.clear()
    return repo, runner, baseline.meta.task_id, proposal_id, candidate


def test_verified_candidate_has_complete_evidence_and_benchmarks_last(scenario):
    repo, runner, task_id, proposal_id, _ = scenario
    policy = plan()
    result = run_verification(repo, task_id, proposal_id, runner, policy)
    assert result.WhichOneof("outcome") == "success"
    assert len(result.success.passed_test_ids) == 7
    assert len(result.success.benchmarks) == 1
    assert runner.events[-1] == ("execute", "candidate", "normal")
    assert ("execute", "baseline", "undefined") in runner.events[:-1]
    assert ("execute", "candidate", "undefined") in runner.events[:-1]
    report = json.loads(repo.artifacts.get(result.correctness_report))
    assert report["correctness_passed"] and not report["baseline_invalid"]
    job = repo.list_jobs(task_id)[-1]
    assert len(job.additional_test_ids) == 5
    assert VerificationPlan.parse(repo.artifacts.get(job.verification_plan)) == policy
    assert len(json.loads(repo.artifacts.get(job.verification_inputs))["cases"]) == 7
    assert repo.get_task(task_id)["status"] == "running"
    assert repo.get_decision(task_id) is None


def test_random_fuzz_mismatch_rejects_candidate_before_sanitizers_or_measurement(scenario):
    repo, runner, task_id, proposal_id, _ = scenario
    runner.mode = "wrong-generated"
    result = run_verification(repo, task_id, proposal_id, runner, plan())
    assert result.failure.reason == pb.OUTPUT_MISMATCH
    assert result.failure.test_id.startswith(("random-", "fuzz-"))
    assert not any(event[2] != "normal" for event in runner.events)
    assert repo.get_task(task_id)["status"] == "running"
    assert not json.loads(repo.artifacts.get(result.correctness_report))["correctness_passed"]


def test_candidate_sanitizer_failure_disqualifies_without_killing_task(scenario):
    repo, runner, task_id, proposal_id, _ = scenario
    runner.mode = "candidate-ub"
    result = run_verification(repo, task_id, proposal_id, runner)
    assert result.failure.reason == pb.SANITIZER_FAILURE
    assert runner.events[-1] == ("execute", "candidate", "undefined")
    assert repo.get_task(task_id)["status"] == "running"


@pytest.mark.parametrize("mode", ["baseline-ub", "baseline-drift"])
def test_unsafe_reference_invalidates_task(scenario, mode):
    repo, runner, task_id, proposal_id, _ = scenario
    runner.mode = mode
    result = run_verification(repo, task_id, proposal_id, runner)
    assert result.WhichOneof("outcome") == "failure"
    assert json.loads(repo.artifacts.get(result.correctness_report))["baseline_invalid"]
    assert repo.get_task(task_id)["status"] == "failed"


def test_infrastructure_failure_is_not_a_sanitizer_finding(scenario):
    repo, runner, task_id, proposal_id, _ = scenario
    runner.mode = "infrastructure"
    result = run_verification(repo, task_id, proposal_id, runner)
    assert result.failure.reason == pb.INFRASTRUCTURE_ERROR
    assert repo.get_task(task_id)["status"] == "running"


def test_candidate_cannot_supply_new_fixtures_or_build_settings(scenario):
    repo, _, task_id, _, candidate = scenario
    (candidate / "submission.json").write_text("{}")
    with pytest.raises(ContractError):
        register_candidate(repo, task_id, candidate)


def test_generator_is_reproducible_and_domain_preserving():
    package = read_package(EXAMPLES / "sum-array")
    first = generate_cases(package, plan())
    assert first == generate_cases(package, plan())
    for case in first[2:]:
        values = list(map(int, case.stdin.split()))
        assert values[0] == len(values) - 1
        assert 0 <= values[0] <= 8
        assert all(-100 <= value <= 100 for value in values[1:])
    assert any(case.kind == "random" for case in first)
    assert any(case.kind == "fuzz" for case in first)


@pytest.mark.parametrize(
    ("actual", "expected", "matches"),
    [
        (b"1.00001\n2", b"1 2.00000", True),
        (b"1.1", b"1", False),
        (b"nan", b"nan", False),
        (b"inf", b"1", False),
        (b"1 2", b"1", False),
        (b"label 1", b"label 1", False),
        (b"-1e3", b"-1000", True),
        (b"1_000", b"1000", False),
    ],
)
def test_numeric_tolerances_are_explicit_and_reject_nonfinite_tokens(actual, expected, matches):
    policy = VerificationPlan(comparison="numeric", absolute_tolerance=0.0001, relative_tolerance=0)
    assert policy.matches(actual, expected) is matches
    assert not VerificationPlan().matches(b"1\n", b"1")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("unknown", 1),
        ("sanitizer", "disabled"),
        ("comparison", {"mode": "numeric"}),
        ("generator", {"kind": "arbitrary_script", "command": "bad"}),
    ],
)
def test_plan_rejects_invalid_and_unknown_fields(key, value):
    data = VerificationPlan().as_dict()
    data[key] = value
    with pytest.raises(ContractError):
        VerificationPlan.parse(data)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("random_cases", 33),
        ("fuzz_cases", -1),
        ("max_length", 1025),
        ("seed", True),
        ("min_value", -1000001),
        ("args", ["bad\0"]),
    ],
)
def test_generator_rejects_unbounded_or_invalid_domains(key, value):
    data = plan().as_dict()
    data["generator"][key] = value
    with pytest.raises(ContractError):
        VerificationPlan.parse(data)


def test_plan_duplicate_keys_and_nonfinite_tolerance():
    with pytest.raises(ContractError):
        VerificationPlan.parse(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises((ContractError, ValueError)):
        VerificationPlan(comparison="numeric", absolute_tolerance=float("nan")).encode()


def test_unbundle_rejects_compression_symlinks_and_traversal():
    for name, mode, compression in [
        ("../escape", 0o100444, zipfile.ZIP_STORED),
        ("link", 0o120777, zipfile.ZIP_STORED),
        ("file", 0o100444, zipfile.ZIP_DEFLATED),
    ]:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            info.compress_type = compression
            archive.writestr(info, b"x")
        with pytest.raises(ContractError):
            unbundle(output.getvalue())


@pytest.mark.parametrize("tamper", ["job", "source", "coverage", "decision"])
def test_stored_evidence_cannot_be_reused_or_claim_incomplete_success(scenario, tamper):
    repo, runner, task_id, proposal_id, _ = scenario
    valid = run_verification(repo, task_id, proposal_id, runner)
    report = json.loads(repo.artifacts.get(valid.correctness_report))
    if tamper == "job":
        report["job_id"] = str(uuid4())
    elif tamper == "source":
        report["candidate_source_sha256"] = "b" * 64
    elif tamper == "coverage":
        report["checks"] = [check for check in report["checks"] if check["phase"] != "sanitizer"]
    else:
        report["correctness_passed"] = False
    bad = pb.VerificationResult()
    bad.CopyFrom(valid)
    bad.meta.message_id = str(uuid4())
    bad.correctness_report.CopyFrom(repo.artifacts.put(json.dumps(report).encode()))
    with pytest.raises(ContractError):
        repo.save_result(bad)
    assert repo.list_results(task_id)[-1] == valid


def test_verified_evidence_requires_reliable_benchmark_before_selection(scenario):
    repo, runner, task_id, proposal_id, _ = scenario
    valid = run_verification(repo, task_id, proposal_id, runner)
    from compiler_optimizer.storage.errors import StateError

    with pytest.raises(StateError, match="reliable benchmark"):
        repo.select_result(
            task_id,
            valid.job_id,
            valid.attempt,
            expected_revision=1,
            reason="Verified candidate chosen manually",
        )
    assert repo.get_task(task_id)["status"] == "running"


def test_optional_coverage_rejects_unsupported_c_contract(scenario):
    from compiler_optimizer.workers.fuzz_policy import FuzzPolicy

    repo, runner, task, proposal, _ = scenario
    # The existing scenario is C17, which must fail closed before creating a job.
    runner.native_execution = True
    runner.target = "linux-x86_64"
    with pytest.raises(ContractError, match="native C\\+\\+17"):
        run_verification(repo, task, proposal, runner, VerificationPlan(coverage=FuzzPolicy()))


def test_independent_oracle_rejects_agreeing_but_wrong_reference_and_candidate(scenario):
    from compiler_optimizer.workers.domains import DomainGenerator

    repo, runner, task, proposal, _ = scenario
    # Both mocked sum executables agree on 0 for empty input; the sorting contract
    # independently requires an empty output line.
    result = run_verification(
        repo, task, proposal, runner, VerificationPlan(generator=DomainGenerator("sort", cases=1))
    )
    assert result.failure.reason == pb.OUTPUT_MISMATCH
    report = json.loads(repo.artifacts.get(result.correctness_report))
    assert report["baseline_invalid"]
    assert repo.get_task(task)["status"] == "failed"
