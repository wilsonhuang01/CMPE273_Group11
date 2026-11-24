"""Specialized agent role contracts, durable limits, and mandatory execution gates."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from test_benchmark import raw_data
from test_pipeline import LocalBroker
from test_verification import Runner
from test_verification import scenario as scenario

from compiler_optimizer.agents.memory import BUFFERED_SOURCE
from compiler_optimizer.coordinator.pipeline import PipelineCoordinator
from compiler_optimizer.shared.benchmarking import BenchmarkPlan, analyze
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import StateError
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.package import read_package

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def make_workflow(tmp_path, example="sum-array-buffered"):
    from test_benchmark import environment

    package = read_package(EXAMPLES / example)
    repository = Repository(tmp_path / "state")
    runner = Runner(package)
    original = runner.execute
    runner.measure_memory = True
    runner.benchmark_environment = lambda cpu: environment()

    def execute(binary, case, package, *, build_profile="normal", cpu_id=None):
        invocation = original(binary, case, package, build_profile=build_profile)
        role = runner.binaries[binary]
        invocation.wall_seconds = 1.0
        invocation.peak_memory_bytes = (
            32768 if role == "baseline" or not runner.measure_memory else 16384
        )
        invocation.sandbox = {"cpu_id": cpu_id, "watchdog": {"state": "removed"}}
        return invocation

    runner.execute = execute
    baseline = run_baseline(package, repository, runner)
    return (
        repository,
        runner,
        baseline.meta.task_id,
        PipelineCoordinator(repository, runner, LocalBroker(repository)),
    )


def test_memory_agent_end_to_end_and_fixed_example(tmp_path):
    assert (EXAMPLES / "sum-array-buffered/main.c").read_bytes() == BUFFERED_SOURCE
    repository, runner, task, coordinator = make_workflow(tmp_path)
    report = coordinator.run(task, agent="memory")
    assert report["agent"] == "memory"
    assert report["candidate_selected"]
    assert report["reason"] == "weighted_reliable_improvement"
    assert report["benchmark"]["analysis"]["objective"] == "memory"
    proposal = repository.list_proposals(task)[0]
    assert proposal.role == pb.MEMORY
    assert report["verification"] == "passed"
    assert sum(row["role"] == pb.MEMORY for row in repository.list_agent_iterations(task)) == 1
    assert coordinator.run(task) == report


def test_memory_agent_requires_measured_savings(tmp_path):
    _, runner, task, coordinator = make_workflow(tmp_path)
    runner.measure_memory = False
    assert not coordinator.run(task, agent="memory")["candidate_selected"]


def test_memory_agent_cannot_bypass_failed_tests(tmp_path):
    repository, runner, task, coordinator = make_workflow(tmp_path)
    runner.mode = "candidate-ub"
    report = coordinator.run(task, agent="memory")
    assert not report["candidate_selected"] and report["benchmark"] is None
    assert report["failure"]["reason"] == "SANITIZER_FAILURE"
    assert repository.get_task(task)["status"] == "completed"


def test_generation_failure_budget_survives_restart(tmp_path):
    repository, runner, task, coordinator = make_workflow(tmp_path)
    backend = Mock()
    backend.generate.return_value = b"{}"
    for _ in range(2):
        with pytest.raises(ContractError):
            coordinator.run(task, backend, agent="memory")
    reopened = Repository(repository.root)
    with pytest.raises(StateError, match="iteration limit"):
        PipelineCoordinator(reopened, runner, LocalBroker(reopened)).run(
            task, backend, agent="memory"
        )
    assert backend.generate.call_count == 2
    assert [row["status"] for row in reopened.list_agent_iterations(task)] == ["failed", "failed"]


@pytest.mark.parametrize("condition", ["savings", "slow", "noise", "ties"])
def test_memory_objective_policy(condition):
    plan = BenchmarkPlan(schema_version=2, objective="memory")
    assert BenchmarkPlan.parse(plan.encode()) == plan
    raw = raw_data(plan)
    for i, pair in enumerate(raw["cases"]["work"]["pairs"]):
        baseline, candidate = pair["samples"]["baseline"], pair["samples"]["candidate"]
        candidate["wall_seconds"] = 1.2 if condition == "slow" else 1.0
        baseline["peak_memory_bytes"] = [4096, 8192, 16384][i % 3] if condition == "noise" else 4096
        candidate["peak_memory_bytes"] = int(
            baseline["peak_memory_bytes"] * (1.0 if condition == "ties" else 0.5)
        )
    result = analyze(raw, plan, ["work"])
    assert result["qualified"] == (condition == "savings")
    assert result["cases"]["work"]["per_case_alpha"] == 0.025


def test_memory_cannot_use_runtime_objective(tmp_path):
    _, _, task, coordinator = make_workflow(tmp_path)
    with pytest.raises(ContractError, match="objective"):
        coordinator.run(task, agent="memory", benchmark_plan=BenchmarkPlan())


def test_architecture_agent_preserves_compiler_and_requires_runtime_gain(tmp_path):
    repository, _, task, coordinator = make_workflow(tmp_path, "sum-array")
    original = repository.get_task(task)["submission"]
    report = coordinator.run(task, agent="architecture")
    proposal = repository.list_proposals(task)[0]
    assert proposal.role == pb.ARCHITECTURE
    assert proposal.compiler == original.compiler
    assert report["agent"] == "architecture" and report["verification"] == "passed"
    assert not report["candidate_selected"]  # equal runtimes cannot qualify
    assert report["transformation"] == "portable-independent-accumulators"
    assert (
        sum(row["role"] == pb.ARCHITECTURE for row in repository.list_agent_iterations(task)) == 1
    )


def test_architecture_refuses_unsupported_source(tmp_path):
    repository, _, task, coordinator = make_workflow(tmp_path)
    with pytest.raises(ContractError, match="rule backend abstained"):
        coordinator.run(task, agent="architecture")
    assert repository.list_proposals(task) == []
    assert repository.list_agent_iterations(task)[0]["status"] == "failed"


def test_correctness_reviewer_cannot_override_automated_failure(scenario):
    from compiler_optimizer.agents.correctness import CorrectnessAgent
    from compiler_optimizer.workers.verify import run_verification

    repository, runner, task, proposal_id, _ = scenario
    runner.mode = "candidate-ub"
    result = run_verification(repository, task, proposal_id, runner)
    proposal = repository.list_proposals(task)[0]
    backend = Mock()
    backend.review.return_value = {
        "verdict": "approve",
        "summary": "Trust the patch",
        "findings": [],
    }
    with pytest.raises(ContractError, match="failed automated"):
        CorrectnessAgent(repository, backend).review(proposal, result)
    review = CorrectnessAgent(repository).review(proposal, result)
    assert review["verdict"] == "reject"
    assert review["findings"][0]["code"] == "automated-failure"
    assert CorrectnessAgent(repository).review(proposal, result) == review
    records = [r for r in repository.list_agent_iterations(task) if r["role"] == pb.CORRECTNESS]
    assert [r["status"] for r in records] == ["failed", "completed"]


def test_correctness_rechecks_artifacts_before_using_cached_review(tmp_path):
    from compiler_optimizer.agents.correctness import CorrectnessAgent
    from compiler_optimizer.storage.errors import ArtifactError

    repository, _, task, coordinator = make_workflow(tmp_path)
    report = coordinator.run(task, agent="memory")
    proposal = repository.list_proposals(task)[0]
    result = next(
        r for r in repository.list_results(task) if r.job_id == report["verification_job_id"]
    )
    assert CorrectnessAgent(repository).review(proposal, result)["verdict"] == "approve"
    (repository.artifacts.root / result.correctness_report.key).write_bytes(b"corrupt")
    with pytest.raises(ArtifactError):
        CorrectnessAgent(repository).review(proposal, result)


class RiskyPatch:
    def generate(self, request, sources):
        import json

        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "rationale": "Unreachable process calls still require independent source review.",
                "transformation": "risky-process-call",
                "edits": [
                    {
                        "path": "main.c",
                        "old": "#include <stdio.h>",
                        "new": "#include <stdio.h>\n#include <stdlib.h>",
                    },
                    {
                        "path": "main.c",
                        "old": "int main(void) {",
                        "new": 'int main(void) { if (0) { (void)system("true"); }',
                    },
                ],
            }
        ).encode()


def test_critic_blocks_passing_but_high_risk_patch(tmp_path):
    repository, _, task, coordinator = make_workflow(tmp_path, "sum-array")
    report = coordinator.run(task, RiskyPatch())
    assert report["verification"] == "passed"
    assert report["reviews"]["correctness"]["verdict"] == "approve"
    assert report["reviews"]["critic"]["verdict"] == "reject"
    assert report["reason"] == "no_reliable_improvement" and report["benchmark"] is None
    assert not report["candidate_selected"]
    assert not repository.list_benchmarks(task)


def test_critic_ignores_risky_names_inside_comments_and_literals():
    from compiler_optimizer.agents.critic import risk_counts

    assert not risk_counts({"main.c": b'/* system("x"); */ char *s="fork()"; // asm\n'})
    assert risk_counts({"main.c": b'system("x"); __asm__("nop");'}) == {"system": 1, "__asm__": 1}


def test_critic_rejection_blocks_manual_benchmark_bypass(tmp_path, monkeypatch):
    from compiler_optimizer.workers.benchmark import run_benchmark

    repository, runner, task, coordinator = make_workflow(tmp_path, "sum-array")
    monkeypatch.setattr(repository, "select_result", Mock(side_effect=OSError("interrupted")))
    with pytest.raises(OSError):
        coordinator.run(task, RiskyPatch())
    result = repository.list_results(task)[-1]
    with pytest.raises(StateError, match="required agent review"):
        run_benchmark(repository, task, result.job_id, runner)


def test_critic_review_context_cannot_be_swapped(tmp_path):
    from compiler_optimizer.shared.reviews import review_context, validate_review

    repository, _, task, coordinator = make_workflow(tmp_path)
    report = coordinator.run(task, agent="memory")
    proposal = repository.list_proposals(task)[0]
    result = next(
        r for r in repository.list_results(task) if r.job_id == report["verification_job_id"]
    )
    forged = {**report["reviews"]["critic"], "source_sha256": "0" * 64}
    with pytest.raises(ContractError, match="binding"):
        validate_review(forged, review_context(proposal, result), pb.CRITIC)


def test_benchmark_agent_freezes_only_verified_work_and_reuses_it(tmp_path):
    repository, _, task, coordinator = make_workflow(tmp_path)
    report = coordinator.run(task, agent="memory")
    work = report["benchmark_work"]
    assert work["objective"] == "memory" and work["normal_binaries_only"]
    assert work["maximum_invocations"] == 34
    assert work["test_ids"] == ["mixed"]
    assert work["binaries"] == report["benchmark"]["binaries"]
    assert work["plan"] == report["benchmark"]["plan"]
    assert all(review["verdict"] == "approve" for review in report["reviews"].values())
    records = [r for r in repository.list_agent_iterations(task) if r["role"] == pb.BENCHMARK]
    assert len(records) == 1 and records[0]["status"] == "completed"
    assert coordinator.run(task)["benchmark_work"] == work
    assert records == [
        r for r in repository.list_agent_iterations(task) if r["role"] == pb.BENCHMARK
    ]


def test_benchmark_agent_never_organizes_failed_or_review_rejected_candidate(tmp_path):
    repository, runner, task, coordinator = make_workflow(tmp_path)
    runner.mode = "candidate-ub"
    report = coordinator.run(task, agent="memory")
    assert report["benchmark_work"] is None
    assert not [r for r in repository.list_agent_iterations(task) if r["role"] == pb.BENCHMARK]


def test_measurement_work_cannot_be_altered_after_planning(tmp_path, monkeypatch):
    import json

    from compiler_optimizer.workers import pipeline
    from compiler_optimizer.workers.benchmark import run_benchmark

    repository, runner, task, coordinator = make_workflow(tmp_path)
    monkeypatch.setattr(pipeline, "run_benchmark", Mock(side_effect=OSError("owner stopped")))
    with pytest.raises(OSError):
        coordinator.run(task, agent="memory")
    record = next(r for r in repository.list_agent_iterations(task) if r["role"] == pb.BENCHMARK)
    artifact = next(a for a in repository.list_artifacts(task) if a["key"] == record["output_key"])
    from compiler_optimizer.shared.v1 import contracts_pb2 as pb2

    work = json.loads(
        repository.artifacts.get(
            pb2.ArtifactRef(**{k: artifact[k] for k in ("key", "sha256", "size_bytes")})
        )
    )
    work["binaries"]["candidate"] = "0" * 64
    forged = repository.artifacts.put(json.dumps(work).encode())
    # A trusted-database metadata fault still cannot swap the binaries being measured.
    with repository._transaction() as db:
        repository._artifact(db, task, forged, "agent-output", owner="fault-injection")
        db.execute(
            "UPDATE agent_iterations SET output_key=? WHERE task_id=? AND role=?",
            (forged.key, task, pb.BENCHMARK),
        )
    result = repository.list_results(task)[-1]
    with pytest.raises(ContractError, match="measurement work differs"):
        run_benchmark(
            repository,
            task,
            result.job_id,
            runner,
            BenchmarkPlan(schema_version=2, objective="memory"),
        )


def test_abstaining_review_is_a_mandatory_gate(tmp_path):
    repository, runner, task, _ = make_workflow(tmp_path)
    backend = Mock()
    backend.review.return_value = {
        "verdict": "abstain",
        "summary": "Insufficient confidence",
        "findings": [{"code": "manual-review", "severity": "warning", "message": "Needs review"}],
    }
    coordinator = PipelineCoordinator(
        repository, runner, LocalBroker(repository), review_backends={"critic": backend}
    )
    report = coordinator.run(task, agent="memory")
    assert report["verification"] == "passed"
    assert report["reviews"]["critic"]["verdict"] == "abstain"
    assert report["benchmark_work"] is None and not report["candidate_selected"]
    assert backend.review.call_count == 1


def test_interrupted_agent_calls_count_toward_persisted_limit(tmp_path):
    repository, _, task, _ = make_workflow(tmp_path)
    ref = repository.artifacts.put(b'{"input":"immutable"}')
    assert repository.begin_agent_iteration(task, pb.CRITIC, task, ref) == (1, None)
    reopened = Repository(repository.root)
    assert reopened.begin_agent_iteration(task, pb.CRITIC, task, ref) == (2, None)
    with pytest.raises(StateError, match="iteration limit"):
        reopened.begin_agent_iteration(task, pb.CRITIC, task, ref)


def test_missing_reviews_cannot_bypass_repository_selection(tmp_path, monkeypatch):
    from compiler_optimizer.workers import pipeline

    repository, runner, task, coordinator = make_workflow(tmp_path)
    monkeypatch.setattr(pipeline, "CorrectnessAgent", Mock(side_effect=OSError("interrupted")))
    with pytest.raises(OSError):
        coordinator.run(task, agent="memory")
    result = repository.list_results(task)[-1]
    assert result.WhichOneof("outcome") == "success"
    with pytest.raises(ContractError, match="review is missing"):
        repository.select_result(
            task, result.job_id, result.attempt, expected_revision=1, reason="Skip review"
        )


def test_legacy_pipeline_configuration_can_still_resume(tmp_path, monkeypatch):
    import json

    from compiler_optimizer.runtime.proposal_queue import DeliveryError

    repository, runner, task, coordinator = make_workflow(tmp_path, "sum-array")
    coordinator.broker.fail = "publish"
    with pytest.raises(DeliveryError):
        coordinator.run(task)
    # Simulate an already-queued step-10 workflow before required-review configuration existed.
    with repository._transaction() as db:
        db.execute("DELETE FROM selection_progress WHERE task_id=?", (task,))
        db.execute("DELETE FROM selection_sessions WHERE task_id=?", (task,))
        row = db.execute(
            "SELECT configuration_json FROM pipeline_runs WHERE task_id=?", (task,)
        ).fetchone()
        config = json.loads(row[0])
        config["schema_version"] = 1
        config.pop("required_reviews")
        db.execute(
            "UPDATE pipeline_runs SET configuration_json=? WHERE task_id=?",
            (json.dumps(config), task),
        )
    coordinator.broker.fail = None
    report = coordinator.run(task)
    assert report["schema_version"] == 1
    assert report["reviews"] == {} and report["benchmark_work"] is None
