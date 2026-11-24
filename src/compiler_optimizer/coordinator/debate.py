"""Bounded, restartable criticism/revision rounds over a predeclared selection cohort."""

import json
import time
from contextlib import nullcontext
from dataclasses import replace

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.coordinator.selection import SelectionCoordinator
from compiler_optimizer.scheduler.resources import reservation_mib
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import StateError
from compiler_optimizer.workers.package import package_from_files, unbundle
from compiler_optimizer.workers.verification_plan import VerificationPlan


class DebateCoordinator(CohortCoordinator):
    def __init__(self, *args, debate_policy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.debate_policy = debate_policy
        self._debate_policy_ref = (
            self.repository.artifacts.put(debate_policy.encode()) if debate_policy else None
        )

    def _prepare(self, task_id, agents, policy, verification_plan, benchmark_plan):
        from compiler_optimizer.workers.verification_plan import generate_cases
        from compiler_optimizer.workers.verify import _snapshot

        require(self.debate_policy is not None, "new debate requires an operator debate policy")
        _, original, _ = _snapshot(self.repository, task_id)
        training = {
            (case.stdin, case.args)
            for case in generate_cases(original, verification_plan or VerificationPlan())
        }
        heldout = [
            (case["stdin"].encode(), tuple(case["args"]))
            for case in self.debate_policy.heldout_cases
        ]
        require(
            len(heldout) == len(set(heldout)) and not training.intersection(heldout),
            "held-out corpus must be distinct from search verification inputs",
        )
        return super()._prepare(task_id, agents, policy, verification_plan, benchmark_plan)

    def _candidate_names(self, names, policy):
        require(self.debate_policy is not None, "new debate requires an operator debate policy")
        require(
            len(names) * self.debate_policy.max_rounds <= policy.maximum_candidates,
            "all debate rounds must fit the frozen selection candidate budget",
        )
        return names * self.debate_policy.max_rounds

    def _generation_request(self, task_id, member, request):
        data = self.repository.debate_context(task_id, member["proposal_id"])
        if data is not None:
            revised = pb.OptimizationRequest()
            revised.CopyFrom(request)
            revised.revision_context_json = data
            return revised
        return request

    def _skip(self, task_id, member, reason):
        repo = self.repository
        identifier = member["proposal_id"]
        generated = any(p.proposal_id == identifier for p in repo.list_proposals(task_id))
        record = {"task_id": task_id, "proposal_id": identifier, "reason": reason}
        ref = repo.artifacts.put(json.dumps(record, sort_keys=True).encode())
        repo.record_selection_progress(
            task_id, identifier, "assessment_failed" if generated else "generation_failed", ref
        )
        repo.debate_event(task_id, identifier, "stopped", record)

    def _feedback(self, task_id, slot):
        repo = self.repository
        cached = repo.debate_context(task_id, slot["proposal_id"])
        if cached is not None:
            repo.debate_event(
                task_id, slot["proposal_id"], "revision_requested", json.loads(cached)
            )
            return True
        progress = next(
            (p for p in repo.selection_progress(task_id) if p["proposal_id"] == slot["parent_id"]),
            None,
        )
        if progress is None or progress["status"] != "evaluated":
            return False
        with repo._transaction(write=False) as db:
            previous = json.loads(repo.artifacts.get(repo._ref(db, progress["evidence_key"])))
        reviews = previous["reviews"]
        result = next(
            r
            for r in repo.list_results(task_id)
            if r.job_id == previous["verification_job_id"]
            and r.attempt == previous["verification_attempt"]
        )
        if result.WhichOneof("outcome") == "success" and all(
            review["verdict"] == "approve" for review in reviews.values()
        ):
            return False  # Never feed benchmark scores/noise back into adaptive search.
        parent = next(p for p in repo.list_proposals(task_id) if p.proposal_id == slot["parent_id"])
        patch = next(
            pb.ArtifactRef(**{k: row[k] for k in ("key", "sha256", "size_bytes")})
            for row in repo.list_artifacts(task_id)
            if row["kind"] == "selection-patch" and row["owner_id"] == parent.proposal_id
        )
        feedback = {
            "reviews": reviews,
            "automated_passed": result.WhichOneof("outcome") == "success",
        }
        if result.WhichOneof("outcome") == "failure":
            feedback["failure"] = {
                "reason": pb.FailureReason.Name(result.failure.reason),
                "message": result.failure.message,
                "test_id": result.failure.test_id,
            }
            feedback["correctness"] = json.loads(repo.artifacts.get(result.correctness_report))
        prior_context = repo.debate_context(task_id, parent.proposal_id)
        if prior_context is not None:

            def actionable(value):
                return {
                    "reviews": {
                        name: {key: review[key] for key in ("verdict", "summary", "findings")}
                        for name, review in value["reviews"].items()
                    },
                    "failure": value.get("failure"),
                }

            if actionable(json.loads(prior_context)["feedback"]) == actionable(feedback):
                return False
        context = {
            "schema_version": 1,
            "round": slot["round_number"],
            "parent_proposal_id": parent.proposal_id,
            "parent_source_sha256": parent.source.bundle.sha256,
            "previous_patch": json.loads(repo.artifacts.get(patch)),
            "feedback": feedback,
        }
        data = json.dumps(context, sort_keys=True, allow_nan=False)
        if len(data.encode()) > 262144:
            return False  # Do not silently truncate criticism or verification evidence.
        repo.bind_debate_context(task_id, slot["proposal_id"], data)
        repo.debate_event(task_id, slot["proposal_id"], "revision_requested", context)
        return True

    def _run(self, task_id):
        repo = self.repository
        run = repo.debate_run(task_id)
        require(run is not None, "resume requires a frozen debate session")
        if self.debate_policy is not None:
            require(
                json.loads(self.debate_policy.encode()) == run["policy"], "debate policy is frozen"
            )
        cached = repo.selection_report(task_id)
        if cached is not None:
            work = repo.cohort_work(task_id)
            if work is not None:
                for member in work["members"]:
                    repo.checkpoint_cohort_member(task_id, member["proposal_id"], "completed")
                repo.checkpoint_cohort(task_id, "completed")
            return cached[1]
        self._reservation_mib = reservation_mib(repo.get_task(task_id)["submission"].manifest)
        repo.begin_cohort_work(task_id, 1, self._reservation_mib)
        if self.recovery is not None:
            self.recovery.flush_outbox(task_id, self.broker)
        else:
            with repo._transaction(write=False) as db:
                require(
                    db.execute("SELECT 1 FROM recovery_jobs WHERE task_id=?", (task_id,)).fetchone()
                    is None,
                    "resume this task with its recovery controller",
                )
        session = repo.get_selection_session(task_id)
        for slot, member in zip(run["slots"], session["members"], strict=True):
            if self._cancellation_requested(task_id):
                return self._finish_cancelled(task_id)
            progress = next(
                (
                    p
                    for p in repo.selection_progress(task_id)
                    if p["proposal_id"] == member["proposal_id"]
                ),
                None,
            )
            if progress is None:
                limit = repo.debate_limit(task_id)
                if limit or repo.get_task(task_id)["status"] == "failed":
                    self._skip(task_id, member, limit or "baseline_invalidated")
                elif slot["parent_id"] is not None and not self._feedback(task_id, slot):
                    self._skip(task_id, member, "no_actionable_progress")
                else:
                    proposal = self._generate_member(task_id, member)
                    if proposal is not None:
                        repo.debate_event(
                            task_id,
                            proposal.proposal_id,
                            "generated",
                            {
                                "round": slot["round_number"],
                                "parent_proposal_id": slot["parent_id"],
                                "source_sha256": proposal.source.bundle.sha256,
                                "baseline_source_sha256": proposal.source.parent_source_sha256,
                            },
                        )
                        duplicate = any(
                            p.proposal_id != proposal.proposal_id
                            and p.source.bundle.sha256 == proposal.source.bundle.sha256
                            for p in repo.list_proposals(task_id)
                        )
                        if duplicate:
                            self._skip(task_id, member, "duplicate_patch_no_progress")
                        elif repo.debate_limit(task_id):
                            self._skip(task_id, member, repo.debate_limit(task_id))
                        else:
                            self.broker.exchange(proposal, self.consume, lambda: None)
            elif self.recovery is not None and repo.get_recovery_job(
                task_id, member["proposal_id"]
            ):
                proposal = next(
                    p
                    for p in repo.list_proposals(task_id)
                    if p.proposal_id == member["proposal_id"]
                )
                self.broker.exchange(proposal, self.consume, lambda: None)
            progress = next(
                p
                for p in repo.selection_progress(task_id)
                if p["proposal_id"] == member["proposal_id"]
            )
            with repo._transaction(write=False) as db:
                record = json.loads(repo.artifacts.get(repo._ref(db, progress["evidence_key"])))
            repo.debate_event(task_id, member["proposal_id"], "round_completed", record)
            repo.checkpoint_cohort_member(task_id, member["proposal_id"], "completed")
        if self.recovery is not None:
            self.recovery.flush_outbox(task_id, self.broker)
        if self._cancellation_requested(task_id):
            return self._finish_cancelled(task_id)
        self._final_evaluation(task_id)
        if self._cancellation_requested(task_id):
            return self._finish_cancelled(task_id)
        try:
            result = SelectionCoordinator(repo).choose(task_id)
        except StateError:
            if self._cancellation_requested(task_id):
                return self._finish_cancelled(task_id)
            raise
        repo.checkpoint_cohort(task_id, "completed")
        return result

    def _final_evaluation(self, task_id):
        repo = self.repository
        audit = repo.preview_debate_selection(task_id)
        identifier = audit["selected_proposal_id"]
        state = repo.claim_debate_final(task_id, identifier)
        if state == "completed":
            return
        if state == "running":
            repo.finish_debate_final(
                task_id, {"passed": False, "reason": "interrupted_final_evaluation"}
            )
            return
        if identifier is None:
            repo.finish_debate_final(task_id, {"passed": True, "reason": "baseline_retained"})
            return
        run = repo.debate_run(task_id)
        if int(time.time() * 1000) >= run["deadline_ms"]:
            repo.finish_debate_final(task_id, {"passed": False, "reason": "time_budget_exhausted"})
            return
        report = {"passed": False, "proposal_id": identifier, "cases": []}
        try:
            report = self._heldout(task_id, identifier)
        except Exception as error:
            report["reason"] = type(error).__name__
        repo.finish_debate_final(task_id, report)

    def _heldout(self, task_id, identifier):
        repo, runner = self.repository, self.runner
        run = repo.debate_run(task_id)
        submission = repo.get_task(task_id)["submission"]
        original = package_from_files(unbundle(repo.artifacts.get(submission.package)))
        proposal = next(p for p in repo.list_proposals(task_id) if p.proposal_id == identifier)
        sources = unbundle(repo.artifacts.get(proposal.source.bundle))
        candidate = package_from_files({**original.files, **sources})
        member = next(
            m
            for m in repo.get_selection_session(task_id)["members"]
            if m["proposal_id"] == identifier
        )
        plan = VerificationPlan.parse(
            repo.artifacts.get(pb.ArtifactRef(**member["verification_plan"]))
        )
        report = {"passed": False, "proposal_id": identifier, "cases": []}
        window = (
            runner.work_window(memory_mib=self._reservation_mib)
            if hasattr(runner, "work_window")
            else nullcontext()
        )
        with window:

            def active():
                if self._cancellation_requested(task_id):
                    raise StateError("debate cancelled during held-out evaluation")
                if int(time.time() * 1000) >= run["deadline_ms"]:
                    raise TimeoutError("debate deadline expired during held-out evaluation")

            active()
            before = runner.compile(original)
            active()
            after = runner.compile(candidate)
            if before.failure or after.failure:
                return {**report, "reason": "heldout_compilation_failed"}
            for index, item in enumerate(run["policy"]["heldout_cases"]):
                if int(time.time() * 1000) >= run["deadline_ms"]:
                    return {**report, "reason": "time_budget_exhausted"}
                active()
                path = "__debate_heldout_input__"
                case = pb.TestCase(id=f"heldout-{index}", stdin=path, args=item["args"])
                left = runner.execute(
                    before.binary,
                    case,
                    replace(original, files={**original.files, path: item["stdin"].encode()}),
                )
                active()
                right = runner.execute(
                    after.binary,
                    case,
                    replace(candidate, files={**candidate.files, path: item["stdin"].encode()}),
                )
                passed = (
                    not left.failure
                    and not right.failure
                    and plan.matches(right.stdout, left.stdout)
                )
                report["cases"].append({"index": index, "passed": passed})
                if not passed:
                    return {**report, "reason": "heldout_output_mismatch"}
        return {**report, "passed": True, "reason": "heldout_passed"}
