"""Frozen weighted consensus, multi-candidate execution, integrity and baseline fallback."""

import copy
import hashlib
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_benchmark import environment, raw_data
from test_verification import Runner
from test_verification import scenario as scenario

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.coordinator.selection import SelectionCoordinator
from compiler_optimizer.runtime.proposal_queue import DeliveryError
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.selection import SelectionPolicy, rank_key, weighted_statistics
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import ConflictError, StateError
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.package import read_package

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class CohortBroker:
    def __init__(self, repository):
        self.repository, self.events = repository, []
        self.fail = None

    def exchange(self, proposal, process, published):
        if self.fail == "publish":
            raise DeliveryError("broker interrupted")
        published()
        result = process(proposal)
        assert any(
            p["proposal_id"] == proposal.proposal_id
            for p in self.repository.selection_progress(proposal.meta.task_id)
        )
        self.events.append(proposal.proposal_id)
        if self.fail == "ack":
            raise DeliveryError("ack lost")
        return result


class MeasuredRunner(Runner):
    def __init__(self, package):
        super().__init__(package)
        self.kinds = {}
        self.metrics = {
            "baseline": (1.0, 1000000),
            "performance": (0.6, 1000000),
            "architecture": (0.9, 500000),
            "memory": (1.0, 500000),
        }
        self.fail_kind = None
        self.current_candidate = None
        self.benchmark_environment = lambda cpu: environment()

    def compile(self, package, *, build_profile="normal"):
        invocation = super().compile(package, build_profile=build_profile)
        role = self.binaries[invocation.binary]
        code = package.sources["main.c"]
        kind = (
            "baseline"
            if role == "baseline"
            else "architecture"
            if b"alternate" in code
            else "performance"
            if b"setvbuf" in code
            else "memory"
        )
        invocation.binary = (
            hashlib.sha256(package.source_bundle + build_profile.encode()).hexdigest().encode()
        )
        self.binaries[invocation.binary], self.kinds[invocation.binary] = role, kind
        if role != "baseline":
            self.current_candidate = kind
        return invocation

    def execute(self, binary, case, package, *, build_profile="normal", cpu_id=None):
        invocation = super().execute(binary, case, package, build_profile=build_profile)
        kind = self.kinds[binary]
        invocation.wall_seconds, invocation.peak_memory_bytes = self.metrics[kind]
        invocation.sandbox = {"cpu_id": cpu_id, "watchdog": {"state": "removed"}}
        if kind == self.fail_kind:
            invocation.stdout = b"wrong\n"
        return invocation


@pytest.fixture
def cohort(tmp_path):
    package = read_package(EXAMPLES / "sum-array")
    repo = Repository(tmp_path / "state")
    runner = MeasuredRunner(package)
    baseline = run_baseline(package, repo, runner)
    broker = CohortBroker(repo)
    return repo, runner, baseline.meta.task_id, broker, CohortCoordinator(repo, runner, broker)


@pytest.mark.parametrize(
    "policy,winner",
    [
        (SelectionPolicy(), "performance"),
        (SelectionPolicy(performance_weight=0.2, memory_weight=0.8), "architecture"),
    ],
)
def test_weights_choose_between_verified_measured_candidates(cohort, policy, winner):
    repo, runner, task, broker, coordinator = cohort
    audit = coordinator.run(task, agents=["performance", "architecture"], policy=policy)
    assert audit["status"] == "selected"
    selected = next(
        c for c in audit["candidates"] if c["proposal_id"] == audit["selected_proposal_id"]
    )
    assert selected["role"] == winner
    assert all(
        c["votes"] == {"correctness": "approve", "critic": "approve"} for c in audit["candidates"]
    )
    assert len(audit["ranking"]) == 2 and len(broker.events) == 2
    assert repo.get_task(task)["status"] == "completed"
    assert repo.get_decision(task)["scores"] == repo.selection_scores(audit)
    before = list(runner.events)
    assert coordinator.run(task) == audit
    assert before == runner.events


def test_one_failed_candidate_cannot_win_despite_fast_metrics(cohort):
    _, runner, task, _, coordinator = cohort
    runner.fail_kind = "performance"
    audit = coordinator.run(task, agents=["performance", "architecture"])
    assert audit["status"] == "selected"
    failed = next(c for c in audit["candidates"] if c["role"] == "performance")
    assert failed["state"] == "excluded" and failed["reasons"] == ["verification_failed"]
    assert audit["selected_proposal_id"] != failed["proposal_id"]


def test_all_unreliable_results_retain_original(cohort):
    repo, runner, task, _, coordinator = cohort
    runner.metrics["performance"] = runner.metrics["architecture"] = runner.metrics["baseline"]
    audit = coordinator.run(task, agents=["performance", "architecture"])
    assert audit["status"] == "retained_baseline" and audit["ranking"] == []
    assert audit["selected_proposal_id"] is None
    assert (
        audit["selected_source"]["sha256"] == repo.get_task(task)["submission"].source.bundle.sha256
    )


def test_weighted_regression_retains_baseline_even_when_role_objective_qualifies(cohort):
    _, runner, task, _, coordinator = cohort
    runner.metrics["architecture"] = (
        0.9,
        1040000,
    )  # runtime improves, memory regresses within its 5% guard
    audit = coordinator.run(
        task,
        agents=["architecture"],
        policy=SelectionPolicy(performance_weight=0.0, memory_weight=1.0),
    )
    assert audit["status"] == "retained_baseline"
    assert audit["candidates"][0]["reasons"] == ["no_reliable_weighted_gain"]


def test_generation_failure_is_audited_without_dropping_planned_cohort_size(cohort):
    _, _, task, _, coordinator = cohort
    audit = coordinator.run(
        task
    )  # Memory starter intentionally refuses an already-streaming source
    memory = next(c for c in audit["candidates"] if c["role"] == "memory")
    assert memory["reasons"] == ["generation_failed"]
    eligible = next(c for c in audit["candidates"] if c["state"] == "eligible")
    assert eligible["statistics"]["cases"]["mixed"]["per_case_alpha"] == 0.05 / 3


def test_frozen_policy_and_incomplete_cohort_block_early_selection(cohort):
    repo, _, task, broker, coordinator = cohort
    broker.fail = "publish"
    with pytest.raises(DeliveryError):
        coordinator.run(task, agents=["performance", "architecture"])
    assert repo.preview_selection(task)["status"] == "pending"
    with pytest.raises(ContractError, match="not fully evaluated"):
        SelectionCoordinator(repo).choose(task)
    session = repo.get_selection_session(task)
    with pytest.raises(ConflictError, match="frozen"):
        repo.begin_selection(
            task,
            repo.artifacts.put(
                replace(SelectionPolicy(), memory_weight=0.8, performance_weight=0.2).encode()
            ),
            session["members"],
        )
    broker.fail = None
    assert coordinator.run(task)["status"] == "selected"


def test_lost_ack_reuses_candidate_evidence(cohort):
    repo, runner, task, broker, coordinator = cohort
    broker.fail = "ack"
    with pytest.raises(DeliveryError):
        coordinator.run(task, agents=["performance", "architecture"])
    first = repo.selection_progress(task)[0]["proposal_id"]
    calls = sum(e[0] == "compile" for e in runner.events)
    broker.fail = None
    assert coordinator.run(task)["status"] == "selected"
    assert broker.events.count(first) == 1
    assert sum(e[0] == "compile" for e in runner.events) == calls + 4


def test_repository_recomputes_winner_and_scores_instead_of_trusting_coordinator(
    cohort, monkeypatch
):
    repo, _, task, _, coordinator = cohort
    choose = SelectionCoordinator.choose
    monkeypatch.setattr(SelectionCoordinator, "choose", Mock(side_effect=OSError("interrupted")))
    with pytest.raises(OSError):
        coordinator.run(task, agents=["performance", "architecture"])
    audit = repo.preview_selection(task)
    baseline = next(r for r in repo.list_results(task) if r.WhichOneof("subject") == "baseline")
    with pytest.raises(StateError, match="weighted selection"):
        repo.select_result(
            task,
            baseline.job_id,
            baseline.attempt,
            expected_revision=1,
            reason="Override policy",
            scores={},
        )
    with pytest.raises(StateError, match="weighted selection"):
        repo.select_result(
            task,
            audit["selected_job_id"],
            audit["selected_attempt"],
            expected_revision=1,
            reason=audit["reason"],
            scores={"weighted_score": 1.0},
        )
    assert repo.get_decision(task) is None and repo.selection_report(task) is None
    monkeypatch.setattr(SelectionCoordinator, "choose", choose)
    assert coordinator.run(task)["status"] == "selected"


def test_duplicate_source_snapshot_has_one_rank(cohort):
    from compiler_optimizer.agents.rules import PerformanceRuleBackend

    class DuplicatePerformance:
        def generate(self, request, sources):
            from compiler_optimizer.shared.v1 import contracts_pb2 as pb

            copy = pb.OptimizationRequest()
            copy.CopyFrom(request)
            copy.role = pb.PERFORMANCE
            return PerformanceRuleBackend().generate(copy, sources)

    repo, runner, task, broker, _ = cohort
    coordinator = CohortCoordinator(
        repo, runner, broker, backends={"architecture": DuplicatePerformance()}
    )
    audit = coordinator.run(task, agents=["performance", "architecture"])
    assert len(audit["ranking"]) == 1
    assert sum("duplicate_source" in c["reasons"] for c in audit["candidates"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("performance_weight", float("nan")),
        ("memory_weight", -1),
        ("performance_weight", 0.8),
        ("alpha", True),
        ("maximum_candidates", 100),
        ("tie_digits", -1),
    ],
)
def test_invalid_policy_rejected(field, value):
    policy = asdict(SelectionPolicy())
    policy[field] = value
    with pytest.raises(ContractError):
        SelectionPolicy.parse(policy)


def test_cohort_multiple_comparison_correction_and_ties():
    plan = BenchmarkPlan(warmups=0, repetitions=7)
    raw = raw_data(plan)
    policy = SelectionPolicy()
    assert weighted_statistics(raw, plan, ["work"], policy, 1)["reliable"]
    # Two workloads and six planned candidates cannot qualify using seven pairs.
    raw["cases"]["other"] = copy.deepcopy(raw["cases"]["work"])
    from compiler_optimizer.shared.benchmarking import pair_order

    for i, pair in enumerate(raw["cases"]["other"]["pairs"]):
        pair["order"] = pair_order(plan, "other", i)
    assert not weighted_statistics(raw, plan, ["work", "other"], policy, 6)["reliable"]


def test_tie_breakers_are_deterministic_and_use_no_author_vote():
    stats = {
        "weighted_lower_bound": 0.1,
        "weighted_point_score": 0.2,
        "worst_case_lower_bound": 0.05,
        "peak_memory_ratio": 0.8,
        "runtime_ratio": 0.9,
    }
    a = {"statistics": stats, "source_sha256": "a" * 64, "proposal_id": "z"}
    b = {"statistics": dict(stats), "source_sha256": "b" * 64, "proposal_id": "a"}
    assert sorted([b, a], key=lambda c: rank_key(c, SelectionPolicy())) == [a, b]
    b["statistics"]["peak_memory_ratio"] = 0.7
    assert rank_key(b, SelectionPolicy()) < rank_key(a, SelectionPolicy())


def test_vetoed_high_score_is_excluded_from_consensus(cohort):
    repo, runner, task, broker, _ = cohort

    class Reviewer:
        def review(self, context, before, after, rationale):
            veto = b"alternate" in after["main.c"]
            return {
                "verdict": "reject" if veto else "approve",
                "summary": "Independent review",
                "findings": [
                    {"code": "manual-veto", "severity": "error", "message": "Needs revision"}
                ]
                if veto
                else [],
            }

    coordinator = CohortCoordinator(repo, runner, broker, review_backends={"critic": Reviewer()})
    audit = coordinator.run(
        task,
        agents=["performance", "architecture"],
        policy=SelectionPolicy(performance_weight=0.2, memory_weight=0.8),
    )
    assert audit["status"] == "selected"
    rejected = next(c for c in audit["candidates"] if c["role"] == "architecture")
    assert rejected["reasons"] == ["review_veto_or_abstention"]
    assert rejected["votes"]["critic"] == "reject"


def test_different_worker_environment_cannot_win_cross_candidate_ranking(cohort):
    _, runner, task, _, coordinator = cohort
    runner.benchmark_environment = lambda cpu: {
        **environment(),
        "boot_id": "other-worker" if runner.current_candidate == "architecture" else "boot",
    }
    audit = coordinator.run(
        task,
        agents=["performance", "architecture"],
        policy=SelectionPolicy(performance_weight=0.2, memory_weight=0.8),
    )
    excluded = next(c for c in audit["candidates"] if c["role"] == "architecture")
    assert excluded["reasons"] == ["incomparable_environment_or_baseline"]
    assert audit["status"] == "selected"


def test_invalid_review_output_excludes_candidate_instead_of_accepting_it(cohort):
    repo, runner, task, broker, _ = cohort
    backend = Mock()
    backend.review.return_value = {
        "verdict": "approve",
        "summary": "Skip evidence",
        "findings": [],
        "rewrite_policy": True,
    }
    audit = CohortCoordinator(repo, runner, broker, review_backends={"critic": backend}).run(
        task, agents=["performance", "architecture"]
    )
    assert audit["status"] == "retained_baseline"
    assert all(c["reasons"] == ["assessment_failed"] for c in audit["candidates"])


def test_atomic_decision_and_audit_rollback_together(cohort, monkeypatch):
    repo, _, task, _, coordinator = cohort
    choose = SelectionCoordinator.choose
    monkeypatch.setattr(
        SelectionCoordinator, "choose", Mock(side_effect=OSError("pause before selection"))
    )
    with pytest.raises(OSError):
        coordinator.run(task, agents=["performance", "architecture"])
    monkeypatch.setattr(SelectionCoordinator, "choose", choose)
    history = repo._history
    monkeypatch.setattr(repo, "_history", Mock(side_effect=OSError("commit interrupted")))
    with pytest.raises(OSError):
        coordinator.run(task)
    assert repo.get_decision(task) is None and repo.selection_report(task) is None
    assert repo.get_task(task)["status"] == "running"
    monkeypatch.setattr(repo, "_history", history)
    assert coordinator.run(task)["status"] == "selected"


def test_policy_cannot_be_chosen_after_verification(scenario):
    from compiler_optimizer.storage.pipeline import reference
    from compiler_optimizer.workers.verify import run_verification

    repo, runner, task, proposal_id, _ = scenario
    result = run_verification(repo, task, proposal_id, runner)
    job = next(j for j in repo.list_jobs(task) if j.job_id == result.job_id)
    with pytest.raises(ContractError, match="before candidate"):
        repo.begin_selection(
            task,
            repo.artifacts.put(SelectionPolicy().encode()),
            [
                {
                    "proposal_id": proposal_id,
                    "role": pb.PERFORMANCE,
                    "verification_plan": reference(job.verification_plan),
                    "benchmark_plan": reference(repo.artifacts.put(BenchmarkPlan().encode())),
                }
            ],
        )
    assert repo.get_selection_session(task) is None


def test_single_pipeline_crash_before_cohort_freeze_recovers_stored_policy(tmp_path, monkeypatch):
    from test_agents import make_workflow

    from compiler_optimizer.coordinator.pipeline import PipelineCoordinator

    repo, runner, task, coordinator = make_workflow(tmp_path)
    begin = repo.begin_selection
    monkeypatch.setattr(repo, "begin_selection", Mock(side_effect=OSError("owner stopped")))
    with pytest.raises(OSError):
        coordinator.run(task, agent="memory")
    assert repo.get_pipeline(task)["configuration"]["schema_version"] == 3
    monkeypatch.setattr(repo, "begin_selection", begin)
    other = PipelineCoordinator(
        repo,
        runner,
        coordinator.broker,
        selection_policy=SelectionPolicy(performance_weight=0.2, memory_weight=0.8),
    )
    with pytest.raises(ContractError, match="frozen"):
        other.run(task)
    assert coordinator.run(task)["selection"]["settings"] == asdict(SelectionPolicy())


def test_reference_failure_never_returns_invalid_original_as_a_verified_result(cohort):
    repo, runner, task, _, coordinator = cohort
    runner.mode = "baseline-ub"
    audit = coordinator.run(task, agents=["performance", "architecture"])
    assert audit["status"] == "blocked" and audit["selected_source"] is None
    assert repo.get_decision(task) is None and repo.get_task(task)["status"] == "failed"


def test_frozen_candidate_cannot_be_resampled_for_a_better_score(cohort, monkeypatch):
    from compiler_optimizer.workers.benchmark import run_benchmark

    repo, runner, task, _, coordinator = cohort
    monkeypatch.setattr(
        SelectionCoordinator, "choose", Mock(side_effect=OSError("stop before choose"))
    )
    with pytest.raises(OSError):
        coordinator.run(task, agents=["performance"])
    result = next(r for r in repo.list_results(task) if r.WhichOneof("subject") == "proposal_id")
    before = len(repo.list_benchmarks(task))
    calls = list(runner.events)
    with pytest.raises(StateError, match="one committed benchmark"):
        run_benchmark(repo, task, result.job_id, runner)
    assert len(repo.list_benchmarks(task)) == before
    assert runner.events == calls
