"""All compiled fixture execution is through DockerRunner's protected sandbox."""

import json
import os
from pathlib import Path

import pytest

from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.domains import KINDS, DomainGenerator
from compiler_optimizer.workers.fuzz_policy import FuzzPolicy
from compiler_optimizer.workers.fuzzing import run_fuzz
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.policy import FUZZ_POLICY_DIGEST
from compiler_optimizer.workers.verification_plan import VerificationPlan
from compiler_optimizer.workers.verify import register_candidate, run_verification

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("BASELINE_DOCKER_TESTS") != "1", reason="opt-in Docker"),
]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples/correctness"


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    return DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-fuzz-test:18"),
        target=os.environ.get("COMPILER_TARGET", "linux-aarch64"),
        watchdog_directory=tmp_path_factory.mktemp("watchdogs"),
    )


def prepare(tmp_path, runner, kind, source=None):
    original = read_package(EXAMPLES / kind)
    repo = Repository(tmp_path / "state")
    baseline = run_baseline(original, repo, runner)
    assert baseline.HasField("success"), baseline.failure
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    code = original.sources["main.cpp"] if source is None else source
    (candidate / "main.cpp").write_bytes(code + b"\n// proposal\n")
    proposal = register_candidate(repo, baseline.meta.task_id, candidate)
    return repo, baseline.meta.task_id, proposal


@pytest.mark.parametrize("kind", KINDS)
def test_native_domains(tmp_path, runner, kind):
    repo, task, proposal = prepare(tmp_path, runner, kind)
    plan = VerificationPlan(
        comparison="numeric" if kind in ("norm", "dot") else "exact",
        absolute_tolerance=1e-10 if kind in ("norm", "dot") else 0,
        generator=DomainGenerator(kind, cases=3),
    )
    result = run_verification(repo, task, proposal, runner, plan)
    assert result.HasField("success"), result.failure
    report = json.loads(repo.artifacts.get(result.correctness_report))
    assert sum(c["phase"] == "sanitizer" for c in report["checks"]) == 4


@pytest.mark.parametrize(
    "kind,bug,reason",
    [
        ("sort", b"volatile int x=2147483647; volatile int y=x+1; (void)y;", pb.SANITIZER_FAILURE),
        ("matrix", b"{volatile int a[1]={0}; volatile int i=2; a[i]=3;}", pb.SANITIZER_FAILURE),
        ("reverse", b'std::cout<<"wrong";', pb.OUTPUT_MISMATCH),
        ("search", b"for(;;) {volatile int spin=0;(void)spin;}", pb.TIMEOUT),
    ],
)
def test_injected_faults(tmp_path, runner, kind, bug, reason):
    code = (
        (EXAMPLES / kind / "main.cpp").read_bytes().replace(b"int main() {", b"int main() {" + bug)
    )
    repo, task, proposal = prepare(tmp_path, runner, kind, code)
    result = run_verification(repo, task, proposal, runner)
    assert result.failure.reason == reason, result.failure


def test_coverage_failure_is_persisted_minimized_and_replayed(tmp_path, runner):
    code = (
        (EXAMPLES / "sort/main.cpp")
        .read_bytes()
        .replace(
            b"if(n) std::sort(a,a+n);",
            b"if(n) std::sort(a,a+n); for(size_t i=0;i<n;i++) if(a[i]==42) {a[i]=43;break;}",
        )
    )
    repo, task, proposal = prepare(tmp_path, runner, "sort", code)
    assert run_verification(repo, task, proposal, runner).HasField("success")
    policy = FuzzPolicy(seconds=3, runs=10000, minimize_seconds=1)
    report, ref = run_fuzz(repo, task, proposal, runner, policy=policy)
    assert report["status"] == "rejected"
    assert report["invocation"]["sandbox"]["policy_digest"] == FUZZ_POLICY_DIGEST
    assert report["failure_inputs"], json.dumps(report, indent=2)
    assert any(k.startswith("corpus/") for k in report["artifacts"])
    assert report["minimization"] is not None
    name = "minimized" if "minimized" in report["artifacts"] else report["failure_inputs"][0]
    from compiler_optimizer.shared.v1.contracts_pb2 import ArtifactRef

    failure = repo.artifacts.get(ArtifactRef(**report["artifacts"][name]))
    (tmp_path / "failure.bin").write_bytes(failure)
    (tmp_path / "campaign.json").write_text(json.dumps(report, sort_keys=True))
    replayed, _ = run_fuzz(repo, task, proposal, runner, policy=policy, replay=failure)
    assert replayed["status"] == "rejected"
    assert repo.artifacts.get(ref)


def test_opt_in_coverage_gate_passes_and_blocks_faulty_candidate(tmp_path, runner):
    good = tmp_path / "good"
    good.mkdir()
    repo, task, proposal = prepare(good, runner, "sort")
    plan = VerificationPlan(coverage=FuzzPolicy(seconds=2, runs=1000, minimize_seconds=1))
    result = run_verification(repo, task, proposal, runner, plan)
    assert result.HasField("success"), result.failure
    report = json.loads(repo.artifacts.get(result.correctness_report))
    assert report["coverage"]["status"] == "bounded-no-finding"

    bad = tmp_path / "bad"
    bad.mkdir()
    code = (
        (EXAMPLES / "sort/main.cpp")
        .read_bytes()
        .replace(
            b"if(n) std::sort(a,a+n);",
            b"if(n) std::sort(a,a+n); for(size_t i=0;i<n;i++) if(a[i]==42) {a[i]=43;break;}",
        )
    )
    repo, task, proposal = prepare(bad, runner, "sort", code)
    result = run_verification(repo, task, proposal, runner, plan)
    assert result.HasField("failure"), result.success
    report = json.loads(repo.artifacts.get(result.correctness_report))
    assert report["coverage"]["status"] == "rejected"
    assert not report["correctness_passed"]
