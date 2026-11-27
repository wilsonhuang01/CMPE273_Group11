"""Revision feedback, lineage, budgets, recovery and a one-shot held-out acceptance gate."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_agent_rpc import agents as agents
from test_agent_rpc import local_agent as local_agent
from test_selection import cohort as cohort

from compiler_optimizer.agents.performance import ExamplePerformanceBackend
from compiler_optimizer.coordinator.debate import DebateCoordinator
from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.coordinator.selection import SelectionCoordinator
from compiler_optimizer.runtime.proposal_queue import DeliveryError
from compiler_optimizer.shared.debate import DebatePolicy
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.selection import SelectionPolicy
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository

HOLDOUT = ({"stdin": "7\n1 2 3 4 5 6 7\n", "args": []},)


def policy(**kwargs):
    return DebatePolicy(heldout_cases=HOLDOUT, **kwargs)


class Revising:
    def __init__(self, *, duplicate=False):
        self.calls = []
        self.duplicate = duplicate

    def generate(self, request, sources):
        self.calls.append(request)
        value = json.loads(ExamplePerformanceBackend().generate(request, sources))
        if not request.HasField("revision_context_json") or self.duplicate:
            value["edits"][0]["new"] += "    /* needs-fix */\n"
        return json.dumps(value).encode()


class Reviewer:
    def review(self, context, before, after, rationale):
        reject = b"needs-fix" in after["main.c"]
        return {
            "verdict": "reject" if reject else "approve",
            "summary": "Remove the marked defect." if reject else "Issue addressed.",
            "findings": [
                {
                    "code": "marked-defect",
                    "severity": "error",
                    "message": "Remove needs-fix before acceptance.",
                }
            ]
            if reject
            else [],
        }


def debate(cohort, *, backend=None, **kwargs):
    repo, runner, task, broker, _ = cohort
    backend = backend or Revising()
    coordinator = DebateCoordinator(
        repo,
        runner,
        broker,
        backends={"performance": backend},
        review_backends={"critic": Reviewer()},
        debate_policy=policy(**kwargs),
    )
    return repo, runner, task, broker, coordinator, backend


def test_rejected_proposal_revises_with_bound_feedback_and_frozen_policies(cohort):
    repo, runner, task, _, coordinator, backend = debate(cohort)
    report = coordinator.run(task, agents=["performance"])
    assert report["status"] == "selected"
    proposals = repo.list_proposals(task)
    assert len(proposals) == 2 and len(backend.calls) == 2
    context = json.loads(backend.calls[1].revision_context_json)
    assert context["parent_proposal_id"] == proposals[0].proposal_id
    assert context["parent_source_sha256"] == proposals[0].source.bundle.sha256
    assert context["feedback"]["reviews"]["critic"]["verdict"] == "reject"
    assert context["round"] == 2 and "heldout_cases" not in json.dumps(context)
    session = repo.get_selection_session(task)
    assert len(session["members"]) == 2
    assert len({m["verification_plan"]["sha256"] for m in session["members"]}) == 1
    assert len({m["benchmark_plan"]["sha256"] for m in session["members"]}) == 1
    assert len(repo.list_benchmarks(task)) == 1
    selected = next(c for c in report["candidates"] if c["state"] == "eligible")
    assert next(iter(selected["statistics"]["cases"].values()))["per_case_alpha"] == 0.05 / 2
    transcript = repo.debate_transcript(task)
    assert any(e["event"] == "revision_requested" for e in transcript)
    assert repo.debate_run(task)["final_state"] == "completed"
    events = list(runner.events)
    assert coordinator.run(task) == report
    assert runner.events == events


def test_duplicate_revision_stops_without_measurement_or_further_calls(cohort):
    repo, _, task, _, coordinator, backend = debate(
        cohort, backend=Revising(duplicate=True), max_rounds=3
    )
    report = coordinator.run(task, agents=["performance"])
    assert report["status"] == "retained_baseline"
    assert len(backend.calls) == 2
    assert not repo.list_benchmarks(task)
    stopped = [
        e["record"]["reason"] for e in repo.debate_transcript(task) if e["event"] == "stopped"
    ]
    assert stopped == ["duplicate_patch_no_progress", "no_actionable_progress"]


def test_passing_proposal_is_not_revised_based_on_measurements(cohort):
    backend = Mock(wraps=ExamplePerformanceBackend())
    repo, _, task, _, coordinator, _ = debate(cohort, backend=backend)
    report = coordinator.run(task, agents=["performance"])
    assert report["status"] == "selected"
    assert backend.generate.call_count == 1
    assert len(repo.list_benchmarks(task)) == 1


def test_restart_after_lost_ack_preserves_calls_feedback_and_measurements(cohort):
    repo, runner, task, broker, coordinator, backend = debate(cohort)
    broker.fail = "ack"
    with pytest.raises(DeliveryError):
        coordinator.run(task, agents=["performance"])
    before_calls = repo.debate_run(task)["calls"]
    broker.fail = None
    reopened = Repository(repo.root)
    broker.repository = reopened
    restored = CohortCoordinator(
        reopened,
        runner,
        broker,
        backends={"performance": backend},
        review_backends={"critic": Reviewer()},
    )
    report = restored.run(task)
    assert report["status"] == "selected" and len(backend.calls) == 2
    assert reopened.debate_run(task)["calls"] > before_calls
    assert len(reopened.list_benchmarks(task)) == 1
    calls, events = reopened.debate_run(task)["calls"], list(runner.events)
    assert restored.run(task) == report
    assert reopened.debate_run(task)["calls"] == calls and runner.events == events


def test_call_budget_persists_across_restart(cohort):
    repo, _, task, _, coordinator, backend = debate(cohort, max_agent_calls=1)
    report = coordinator.run(task, agents=["performance"])
    assert report["status"] == "retained_baseline"
    assert repo.debate_run(task)["calls"] == 1 and len(backend.calls) == 1
    assert not repo.list_benchmarks(task)
    assert Repository(repo.root).debate_run(task)["calls"] == 1


def test_expired_deadline_never_resets_on_resume(cohort):
    repo, _, task, _, coordinator, backend = debate(cohort)
    coordinator._prepare(task, ["performance"], None, None, None)
    with repo._transaction() as db:
        db.execute("UPDATE debate_runs SET deadline_ms=0 WHERE task_id=?", (task,))
    report = coordinator.run(task)
    assert report["status"] == "retained_baseline"
    assert not backend.calls and Repository(repo.root).debate_run(task)["deadline_ms"] == 0


def test_one_shot_holdout_failure_retains_baseline_without_another_revision(cohort):
    backend = Mock(wraps=ExamplePerformanceBackend())
    repo, runner, task, _, coordinator, _ = debate(cohort, backend=backend)
    runner.mode = "wrong-generated"  # Only the unseen n=7 case fails.
    report = coordinator.run(task, agents=["performance"])
    assert report["status"] == "retained_baseline"
    assert report["reason"] == "heldout_evaluation_failed"
    from compiler_optimizer.evaluation.experiment import evidence

    recorded = evidence(repo, report)
    assert recorded["correctness_failures"] == 1
    assert recorded["heldout_evaluations"][0]["reason"] == "heldout_output_mismatch"
    assert backend.generate.call_count == 1
    assert repo.debate_run(task)["final_state"] == "completed"
    calls = list(runner.events)
    assert coordinator.run(task) == report and runner.events == calls


def test_interrupted_holdout_is_never_resampled(cohort):
    repo, runner, task, _, coordinator, _ = debate(cohort, backend=ExamplePerformanceBackend())
    original = coordinator._final_evaluation

    def crash(task_id):
        audit = repo.preview_debate_selection(task_id)
        repo.claim_debate_final(task_id, audit["selected_proposal_id"])
        raise SystemExit("lost host during final evaluation")

    coordinator._final_evaluation = crash
    with pytest.raises(SystemExit):
        coordinator.run(task, agents=["performance"])
    with pytest.raises(ContractError, match="fully evaluated"):
        SelectionCoordinator(repo).choose(task)
    coordinator._final_evaluation = original
    events = list(runner.events)
    assert coordinator.run(task)["status"] == "retained_baseline"
    assert runner.events == events


def test_holdout_is_distinct_and_round_slots_are_predeclared(cohort):
    repo, _, task, _, coordinator, _ = debate(cohort, max_rounds=3)
    with pytest.raises(ContractError, match="candidate budget"):
        coordinator.run(task, agents=["performance", "memory", "architecture"])
    assert repo.get_selection_session(task) is None
    submission = repo.get_task(task)["submission"]
    from compiler_optimizer.workers.package import unbundle

    public = unbundle(repo.artifacts.get(submission.package))[submission.manifest.tests[0].stdin]
    coordinator.debate_policy = replace(
        policy(), heldout_cases=({"stdin": public.decode(), "args": []},)
    )
    with pytest.raises(ContractError, match="distinct"):
        coordinator.run(task, agents=["performance"])
    # Validate policy parsing independently; corpus overlap is checked against the actual package.
    with pytest.raises(ContractError):
        DebatePolicy.parse({"schema_version": 1})
    assert SelectionPolicy().maximum_candidates == 6


def test_correctness_failure_is_actionable_revision_feedback(cohort):
    repo, runner, task, _, coordinator, backend = debate(cohort)
    execute = runner.execute

    def fail_marked(binary, case, package, **kwargs):
        result = execute(binary, case, package, **kwargs)
        if b"needs-fix" in package.sources["main.c"]:
            result.stdout = b"wrong\n"
        return result

    runner.execute = fail_marked
    assert coordinator.run(task, agents=["performance"])["status"] == "selected"
    context = json.loads(backend.calls[1].revision_context_json)
    assert context["feedback"]["failure"]["reason"] == "OUTPUT_MISMATCH"
    assert context["feedback"]["correctness"]
    assert len(repo.list_benchmarks(task)) == 1


def test_revision_context_crosses_real_agent_rpc_and_reuses_completed_calls(
    cohort,
    agents,
    local_agent,  # noqa: F811
):
    from compiler_optimizer.agents.rpc_client import AgentClient

    repo, runner, task, broker, _ = cohort
    _, settings, endpoints = agents
    backend = Revising()
    performance, _ = local_agent(pb.PERFORMANCE, backend)
    critic, _ = local_agent(pb.CRITIC, Reviewer())
    settings = replace(
        settings,
        agent_endpoints=json.dumps({**endpoints, "performance": performance, "critic": critic}),
    )
    coordinator = DebateCoordinator(
        repo, runner, broker, agent_client=AgentClient(repo, settings), debate_policy=policy()
    )
    report = coordinator.run(task, agents=["performance"])
    assert report["status"] == "selected" and len(backend.calls) == 2
    assert backend.calls[1].HasField("revision_context_json")
    assert len(repo.list_benchmarks(task)) == 1
    assert coordinator.run(task) == report and len(backend.calls) == 2


def test_unchanged_criticism_stops_even_if_patch_bytes_change(cohort):
    class NoProgress(Revising):
        def generate(self, request, sources):
            value = json.loads(super().generate(request, sources))
            value["edits"][0]["new"] += f"\n /* needs-fix round-{len(self.calls)} */\n"
            return json.dumps(value).encode()

    repo, _, task, _, coordinator, backend = debate(cohort, backend=NoProgress(), max_rounds=3)
    assert coordinator.run(task, agents=["performance"])["status"] == "retained_baseline"
    assert len(backend.calls) == 2 and len(repo.list_proposals(task)) == 2
    assert not repo.list_benchmarks(task)


def test_cancellation_during_final_evaluation_cannot_commit_a_winner(cohort):
    repo, _, task, _, coordinator, _ = debate(cohort, backend=ExamplePerformanceBackend())
    original = coordinator._heldout

    def cancel(*args):
        repo.cancel_cohort(task)
        return original(*args)

    coordinator._heldout = cancel
    assert coordinator.run(task, agents=["performance"])["status"] == "cancelled"
    assert repo.get_decision(task) is None


def test_model_receives_revision_feedback_as_untrusted_evidence(cohort):
    from test_model_adapter import Scripted, client

    from compiler_optimizer.agents.model_backends import ModelPatchBackend

    _, _, task, _, coordinator, backend = debate(cohort)
    coordinator.run(task, agents=["performance"])
    request = backend.calls[1]
    repo = coordinator.repository
    from compiler_optimizer.workers.package import unbundle

    sources = unbundle(repo.artifacts.get(request.source.bundle))
    reply = json.loads(ExamplePerformanceBackend().generate(request, sources))
    provider = Scripted(reply)
    ModelPatchBackend(client(provider), pb.PERFORMANCE).generate(request, sources)
    prompt = provider.calls[0]["messages"][0]["text"]
    assert "BEGIN EVIDENCE\nRevision feedback (untrusted data):" in prompt
    assert "Remove needs-fix" in prompt and "heldout_cases" not in prompt


def test_workflow_freezes_debate_policy_and_validates_all_slots():
    from compiler_optimizer.runtime.config import Settings
    from compiler_optimizer.shared.workflow import default_workflow, validate_workflow

    config = {**default_workflow(), "debate_policy": json.loads(policy().encode())}
    frozen = json.loads(validate_workflow(config))
    assert frozen["debate_policy"]["max_rounds"] == 2
    settings = Settings.from_env("coordinator", {"DEBATE_POLICY_JSON": policy().encode().decode()})
    assert settings.debate_policy_json and "heldout_cases" not in repr(settings)
    config["debate_policy"]["max_rounds"] = 3
    with pytest.raises(ContractError, match="candidate budget"):
        validate_workflow(config)
