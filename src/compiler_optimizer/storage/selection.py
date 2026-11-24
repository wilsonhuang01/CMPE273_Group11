"""Frozen cohorts, independent evidence collection, and atomic weighted decision audits."""

import json
from dataclasses import asdict

from compiler_optimizer.shared.agent_roles import role_spec
from compiler_optimizer.shared.benchmarking import ENVIRONMENT_KEYS, BenchmarkPlan
from compiler_optimizer.shared.contracts import validate_proposal_for_request
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.measurement import measurement_work
from compiler_optimizer.shared.messaging import decode_message
from compiler_optimizer.shared.patches import apply_patch, parse_patch
from compiler_optimizer.shared.recovery import infrastructure_failure
from compiler_optimizer.shared.reviews import review_context, review_operation, validate_review
from compiler_optimizer.shared.selection import (
    TIE_BREAKERS,
    SelectionPolicy,
    rank_key,
    weighted_statistics,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import ConflictError, StateError, StorageError
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.package import bundle, unbundle
from compiler_optimizer.workers.verification_plan import VerificationPlan


class SelectionStorage:
    def _ref(self, db, key):
        row = db.execute("SELECT * FROM artifacts WHERE key=?", (key,)).fetchone()
        require(row is not None, "unregistered evidence artifact")
        return pb.ArtifactRef(**dict(row))

    def _selection_member(self, db, task_id, proposal_id):
        session = db.execute(
            "SELECT * FROM selection_sessions WHERE task_id=?", (task_id,)
        ).fetchone()
        if session is None:
            return None
        return next(
            (m for m in json.loads(session["cohort_json"]) if m["proposal_id"] == proposal_id), None
        )

    def get_selection_session(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            row = db.execute(
                "SELECT * FROM selection_sessions WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return {
                "task_id": task_id,
                "policy": reference(self._ref(db, row["policy_key"])),
                "members": json.loads(row["cohort_json"]),
                "outcome_key": row["outcome_key"],
            }

    def begin_selection(self, task_id, policy_ref, members, *, debate_policy_ref=None):
        policy = SelectionPolicy.parse(self.artifacts.get(policy_ref))
        require(
            isinstance(members, list) and 1 <= len(members) <= policy.maximum_candidates,
            "selection cohort is empty or exceeds its limit",
        )
        require(
            all(
                isinstance(m, dict)
                and {"proposal_id", "role", "verification_plan", "benchmark_plan"} <= set(m)
                and set(m)
                <= {
                    "proposal_id",
                    "role",
                    "verification_plan",
                    "benchmark_plan",
                    "backend_kind",
                    "backend_patch",
                }
                for m in members
            ),
            "invalid selection cohort member",
        )
        require(len({m["proposal_id"] for m in members}) == len(members), "duplicate cohort member")
        cohort = json.dumps(members, sort_keys=True, allow_nan=False)
        with self._transaction() as db:
            task = self._task(db, task_id)
            previous = db.execute(
                "SELECT * FROM selection_sessions WHERE task_id=?", (task_id,)
            ).fetchone()
            if previous is not None:
                if (previous["policy_key"], previous["cohort_json"]) != (policy_ref.key, cohort):
                    raise ConflictError("selection policy/cohort is already frozen")
                if debate_policy_ref is not None:
                    debate = db.execute(
                        "SELECT policy_key FROM debate_runs WHERE task_id=?", (task_id,)
                    ).fetchone()
                    require(
                        debate is not None and debate[0] == debate_policy_ref.key,
                        "debate policy is frozen",
                    )
                return False
            require(task["status"] == "running", "selection needs a running task")
            self._selection_baseline(db, task_id)
            jobs = [
                decode_message(pb.VerificationJob, r[0])
                for r in db.execute("SELECT payload FROM jobs WHERE task_id=?", (task_id,))
            ]
            require(
                not any(j.WhichOneof("subject") == "proposal_id" for j in jobs),
                "freeze selection policy before candidate verification/measurement",
            )
            results = [
                decode_message(pb.VerificationResult, r[0])
                for r in db.execute("SELECT payload FROM results WHERE task_id=?", (task_id,))
            ]
            require(
                not any(r.WhichOneof("subject") == "proposal_id" for r in results),
                "freeze selection policy before candidate verification/measurement",
            )
            common = None
            for member in members:
                request = self._load(
                    db, "requests", pb.OptimizationRequest, "proposal_id", member["proposal_id"]
                )
                require(
                    request.meta.task_id == task_id
                    and request.role == member["role"]
                    and request.source == self._submission(task).source,
                    "cohort assignment does not match baseline/role",
                )
                require(
                    role_spec(request.role).objective is not None, "reviewer cannot be a candidate"
                )
                require(
                    member.get("backend_kind", "starter") in ("starter", "patch", "custom"),
                    "unknown generation backend kind",
                )
                if member.get("backend_patch") is not None:
                    pref = pb.ArtifactRef(**member["backend_patch"])
                    patch = parse_patch(self.artifacts.get(pref))
                    require(
                        patch["parent_source_sha256"] == request.source.bundle.sha256,
                        "frozen patch parent mismatch",
                    )
                    self._artifact(
                        db, task_id, pref, "selection-patch-input", owner=request.proposal_id
                    )
                vref, bref = (
                    pb.ArtifactRef(**member[k]) for k in ("verification_plan", "benchmark_plan")
                )
                VerificationPlan.parse(self.artifacts.get(vref))
                plan = BenchmarkPlan.parse(self.artifacts.get(bref))
                require(
                    plan.objective == role_spec(request.role).objective, "cohort objective mismatch"
                )
                normalized = asdict(plan)
                normalized.pop("objective")
                normalized.pop("schema_version")
                settings = (vref.sha256, normalized)
                require(
                    common is None or common == settings,
                    "cohort candidates need common verification and measurement settings",
                )
                common = settings
                for ref in (vref, bref):
                    self._artifact(db, task_id, ref, "selection-plan", owner=member["proposal_id"])
            self._artifact(db, task_id, policy_ref, "selection-policy")
            db.execute(
                "INSERT INTO selection_sessions VALUES (?, ?, ?, NULL)",
                (task_id, policy_ref.key, cohort),
            )
            if debate_policy_ref is not None:
                self._begin_debate(db, task_id, debate_policy_ref, members)
        return True

    def register_selection_patch(self, task_id, proposal_id, patch_ref):
        with self._transaction() as db:
            self._task(db, task_id, running=True)
            require(
                self._selection_member(db, task_id, proposal_id) is not None, "unassigned candidate"
            )
            proposal = self._load(db, "proposals", pb.Proposal, "proposal_id", proposal_id)
            request = self._load(db, "requests", pb.OptimizationRequest, "proposal_id", proposal_id)
            validate_proposal_for_request(proposal, request)
            patch, changed = apply_patch(
                self.artifacts.get(patch_ref),
                unbundle(self.artifacts.get(request.source.bundle)),
                request.source.bundle.sha256,
                compiler=request.compiler,
            )
            require(
                bundle(changed) == self.artifacts.get(proposal.source.bundle)
                and patch["rationale"] == proposal.rationale
                and patch["transformation"] == proposal.transformation,
                "selection patch/proposal mismatch",
            )
            prior = db.execute(
                "SELECT key FROM task_artifacts WHERE task_id=? AND owner_id=? "
                "AND kind='selection-patch'",
                (task_id, proposal_id),
            ).fetchone()
            if prior is not None and prior[0] != patch_ref.key:
                raise ConflictError("selection patch is immutable")
            self._artifact(db, task_id, patch_ref, "selection-patch", owner=proposal_id)

    def selection_progress(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                dict(r)
                for r in db.execute("SELECT * FROM selection_progress WHERE task_id=?", (task_id,))
            ]

    def record_selection_progress(self, task_id, proposal_id, status, evidence_ref):
        require(
            status in ("evaluated", "generation_failed", "assessment_failed"),
            "invalid cohort progress",
        )
        evidence = json.loads(self.artifacts.get(evidence_ref))
        require(
            evidence.get("task_id") == task_id and evidence.get("proposal_id") == proposal_id,
            "cohort progress identity mismatch",
        )
        with self._transaction() as db:
            task = self._task(db, task_id)
            require(task["status"] in ("running", "failed"), "selection is terminal")
            require(
                self._selection_member(db, task_id, proposal_id) is not None, "unassigned candidate"
            )
            prior = db.execute(
                "SELECT * FROM selection_progress WHERE task_id=? AND proposal_id=?",
                (task_id, proposal_id),
            ).fetchone()
            if prior:
                if (prior["status"], prior["evidence_key"]) != (status, evidence_ref.key):
                    raise ConflictError("candidate evaluation is immutable")
                return False
            if status == "generation_failed":
                require(
                    db.execute(
                        "SELECT 1 FROM proposals WHERE proposal_id=?", (proposal_id,)
                    ).fetchone()
                    is None,
                    "generated proposal cannot be marked as generation failure",
                )
            elif status == "evaluated":
                member = self._selection_member(db, task_id, proposal_id)
                candidate, _ = self._selection_candidate(db, task_id, member, 1)
                require(candidate["state"] != "pending", "candidate evaluation is incomplete")
            self._artifact(db, task_id, evidence_ref, "candidate-evaluation", owner=proposal_id)
            db.execute(
                "INSERT INTO selection_progress VALUES (?, ?, ?, ?)",
                (task_id, proposal_id, status, evidence_ref.key),
            )
        return True

    def _selection_baseline(self, db, task_id):
        task = self._task(db, task_id)
        submission = self._submission(task)
        self.artifacts.get(submission.package)
        self.artifacts.get(submission.source.bundle)
        baselines = [
            decode_message(pb.VerificationResult, r[0])
            for r in db.execute(
                "SELECT payload FROM results WHERE task_id=? ORDER BY rowid", (task_id,)
            )
        ]
        baselines = [
            r
            for r in baselines
            if r.WhichOneof("subject") == "baseline"
            and r.WhichOneof("outcome") == "success"
            and r.source == submission.source
            and r.compiler == submission.compiler
        ]
        require(bool(baselines), "a passing original baseline is required")
        return baselines[-1]

    def _selection_reviews(self, db, task_id, proposal, result):
        if self._selection_member(db, task_id, proposal.proposal_id) is None:
            return {}
        reviews = {}
        for role in (pb.CORRECTNESS, pb.CRITIC):
            row = db.execute(
                "SELECT output_key FROM agent_iterations WHERE task_id=? AND role=? "
                "AND operation_id=? AND status='completed' ORDER BY iteration DESC LIMIT 1",
                (task_id, role, review_operation(proposal, result)),
            ).fetchone()
            require(row is not None, "required agent review is missing")
            review = json.loads(self.artifacts.get(self._ref(db, row[0])))
            reviews[role_spec(role).name] = validate_review(
                review, review_context(proposal, result), role
            )
        return reviews

    def _selection_work(self, db, task_id, proposal, job, result, plan_ref):
        member = self._selection_member(db, task_id, proposal.proposal_id)
        if member is None:
            return None
        require(member["benchmark_plan"] == reference(plan_ref), "measurement plan changed")
        row = db.execute(
            "SELECT output_key FROM agent_iterations WHERE task_id=? AND role=? "
            "AND operation_id=? AND status='completed' ORDER BY iteration DESC LIMIT 1",
            (task_id, pb.BENCHMARK, review_operation(proposal, result)),
        ).fetchone()
        require(row is not None, "Benchmark Agent measurement work is missing")
        work = json.loads(self.artifacts.get(self._ref(db, row[0])))
        expected = measurement_work(
            self._submission(self._task(db, task_id)),
            proposal,
            job,
            result,
            json.loads(self.artifacts.get(result.correctness_report)),
            BenchmarkPlan.parse(self.artifacts.get(plan_ref)),
            plan_ref,
        )
        require(work == expected, "measurement work differs from frozen inputs")
        return work

    def _selection_candidate(self, db, task_id, member, cohort_size):
        identifier = member["proposal_id"]
        candidate = {
            "proposal_id": identifier,
            "role": role_spec(member["role"]).name,
            "state": "pending",
            "reasons": [],
            "votes": {},
            "statistics": None,
        }
        identity = None
        try:
            row = db.execute(
                "SELECT payload FROM proposals WHERE proposal_id=?", (identifier,)
            ).fetchone()
            if row is None:
                return candidate, None
            proposal = decode_message(pb.Proposal, row[0])
            request = self._load(db, "requests", pb.OptimizationRequest, "proposal_id", identifier)
            validate_proposal_for_request(proposal, request)
            require(proposal.role == member["role"], "candidate role changed")
            pipeline = db.execute(
                "SELECT 1 FROM pipeline_runs WHERE task_id=? AND proposal_id=?",
                (task_id, identifier),
            ).fetchone()
            if pipeline is None:
                pref = db.execute(
                    "SELECT key FROM task_artifacts WHERE task_id=? AND owner_id=? "
                    "AND kind='selection-patch'",
                    (task_id, identifier),
                ).fetchone()
                require(pref is not None, "generated patch evidence is missing")
                patch, changed = apply_patch(
                    self.artifacts.get(self._ref(db, pref[0])),
                    unbundle(self.artifacts.get(request.source.bundle)),
                    request.source.bundle.sha256,
                    compiler=request.compiler,
                )
                require(
                    bundle(changed) == self.artifacts.get(proposal.source.bundle)
                    and patch["rationale"] == proposal.rationale
                    and patch["transformation"] == proposal.transformation,
                    "candidate patch mismatch",
                )
            self.artifacts.get(proposal.source.bundle)
            candidate["source_sha256"] = proposal.source.bundle.sha256
            all_results = [
                decode_message(pb.VerificationResult, r[0])
                for r in db.execute(
                    "SELECT payload FROM results WHERE task_id=? ORDER BY rowid", (task_id,)
                )
            ]
            results = [
                r
                for r in all_results
                if r.WhichOneof("subject") == "proposal_id" and r.proposal_id == identifier
            ]
            if not results:
                return candidate, None
            result = results[-1]
            candidate.update(verification_job_id=result.job_id, verification_attempt=result.attempt)
            if result.HasField("correctness_report"):
                candidate["correctness_report"] = reference(result.correctness_report)
            bad = [
                r
                for r in all_results
                if r.WhichOneof("subject") == "proposal_id"
                and r.source.bundle.sha256 == proposal.source.bundle.sha256
                and r.WhichOneof("outcome") == "failure"
                and not infrastructure_failure(r.failure)
            ]
            if result.WhichOneof("outcome") != "success" or bad:
                candidate.update(state="excluded", reasons=["verification_failed"])
                if result.WhichOneof("outcome") == "failure":
                    candidate["failure_reason"] = pb.FailureReason.Name(result.failure.reason)
                return candidate, None
            job = self._job(db, result.job_id, result.attempt)
            require(
                job.verification_plan == pb.ArtifactRef(**member["verification_plan"]),
                "verification plan changed",
            )
            reviews = self._selection_reviews(db, task_id, proposal, result)
            candidate["votes"] = {name: review["verdict"] for name, review in reviews.items()}
            if any(r["verdict"] != "approve" for r in reviews.values()):
                candidate.update(state="excluded", reasons=["review_veto_or_abstention"])
                return candidate, None
            if job.compiler.target == "linux-riscv64":
                candidate.update(state="excluded", reasons=["emulated_target_correctness_only"])
                return candidate, None
            latest_job = db.execute(
                "SELECT job_id, attempt FROM jobs WHERE task_id=? ORDER BY rowid DESC", (task_id,)
            ).fetchall()
            for record in latest_job:
                other = self._job(db, record["job_id"], record["attempt"])
                if other.WhichOneof("subject") == "proposal_id" and other.proposal_id == identifier:
                    require(
                        (other.job_id, other.attempt) == (result.job_id, result.attempt),
                        "newer verification is incomplete",
                    )
                    break
            row = db.execute(
                "SELECT a.* FROM artifacts a JOIN benchmark_runs b ON b.report_key=a.key "
                "WHERE b.task_id=? AND b.job_id=? AND b.attempt=? ORDER BY b.rowid DESC LIMIT 1",
                (task_id, result.job_id, result.attempt),
            ).fetchone()
            if row is None:
                return candidate, None
            ref = pb.ArtifactRef(**dict(row))
            if db.execute(
                "SELECT 1 FROM benchmark_runs WHERE task_id=? AND source_sha256=? "
                "AND candidate_invalid=1",
                (task_id, proposal.source.bundle.sha256),
            ).fetchone():
                candidate.update(
                    state="excluded",
                    reasons=["candidate_failed_during_measurement"],
                    benchmark_report=reference(ref),
                )
                return candidate, None
            report, raw, plan_ref, _ = self._benchmark_evidence(db, ref)
            require(reference(plan_ref) == member["benchmark_plan"], "benchmark plan changed")
            candidate.update(benchmark_report=reference(ref), benchmark_run_id=report["run_id"])
            if not report["analysis"]["qualified"]:
                candidate.update(state="excluded", reasons=["benchmark_not_qualified"])
                return candidate, None
            session = db.execute(
                "SELECT policy_key FROM selection_sessions WHERE task_id=?", (task_id,)
            ).fetchone()
            policy = SelectionPolicy.parse(self.artifacts.get(self._ref(db, session[0])))
            statistics = weighted_statistics(
                raw,
                BenchmarkPlan.parse(self.artifacts.get(plan_ref)),
                list(job.manifest.benchmark_test_ids),
                policy,
                cohort_size,
            )
            candidate["statistics"] = statistics
            candidate.update(
                state="eligible" if statistics["reliable"] else "excluded",
                reasons=[] if statistics["reliable"] else ["no_reliable_weighted_gain"],
            )
            identity = {
                "baseline_binary": report["binaries"]["baseline"],
                "environment": {k: raw["environment_before"][k] for k in ENVIRONMENT_KEYS},
            }
        except (
            ContractError,
            StorageError,
            ValueError,
            KeyError,
            TypeError,
            StopIteration,
        ) as error:
            candidate.update(
                state="excluded",
                reasons=["invalid_or_missing_evidence"],
                evidence_error=type(error).__name__,
            )
        return candidate, identity

    def _selection_audit(self, db, task_id, *, provisional=False):
        task = self._task(db, task_id)
        session = db.execute(
            "SELECT * FROM selection_sessions WHERE task_id=?", (task_id,)
        ).fetchone()
        require(session is not None, "selection cohort is not frozen")
        policy_ref = self._ref(db, session["policy_key"])
        policy = SelectionPolicy.parse(self.artifacts.get(policy_ref))
        members = json.loads(session["cohort_json"])
        baseline = self._selection_baseline(db, task_id)
        candidates, anchor = [], None
        for member in members:
            progress = db.execute(
                "SELECT * FROM selection_progress WHERE task_id=? AND proposal_id=?",
                (task_id, member["proposal_id"]),
            ).fetchone()
            if progress is not None and progress["status"] in (
                "generation_failed",
                "assessment_failed",
            ):
                self.artifacts.get(self._ref(db, progress["evidence_key"]))
                candidate = {
                    "proposal_id": member["proposal_id"],
                    "role": role_spec(member["role"]).name,
                    "state": "excluded",
                    "reasons": [progress["status"]],
                    "votes": {},
                    "statistics": None,
                }
            else:
                candidate, identity = self._selection_candidate(db, task_id, member, len(members))
                if progress is None:
                    candidate.update(state="pending", reasons=["evaluation_not_committed"])
                else:
                    self.artifacts.get(self._ref(db, progress["evidence_key"]))
                if candidate["state"] == "eligible":
                    if anchor is None:
                        anchor = identity
                    elif anchor != identity:
                        candidate.update(
                            state="excluded", reasons=["incomparable_environment_or_baseline"]
                        )
            candidate["evaluation"] = (
                None if progress is None else reference(self._ref(db, progress["evidence_key"]))
            )
            candidates.append(candidate)
        eligible = sorted(
            (c for c in candidates if c["state"] == "eligible"), key=lambda c: rank_key(c, policy)
        )
        # Identical source snapshots receive one place in the ranking, never extra votes.
        unique, seen = [], set()
        for candidate in eligible:
            if candidate["source_sha256"] in seen:
                candidate.update(state="excluded", reasons=["duplicate_source"])
            else:
                seen.add(candidate["source_sha256"])
                unique.append(candidate)
        pending = any(c["state"] == "pending" for c in candidates)
        debate = db.execute("SELECT * FROM debate_runs WHERE task_id=?", (task_id,)).fetchone()
        heldout_failed = False
        if debate is not None and not provisional:
            pending = pending or debate["final_state"] != "completed"
            if debate["final_report_key"] is not None:
                final = json.loads(self.artifacts.get(self._ref(db, debate["final_report_key"])))
                heldout_failed = not final["passed"]
                if heldout_failed:
                    unique = []  # One final candidate, never adaptive retries on the held-out set.
        blocked = task["status"] == "failed"
        selected = unique[0] if unique and not pending and not blocked else None
        if debate is not None and not provisional and selected is not None:
            require(
                debate["final_proposal_id"] == selected["proposal_id"],
                "held-out evaluation belongs to a different candidate",
            )
        selected_source = None
        if not blocked and not pending:
            selected_source = reference(self._submission(task).source.bundle)
            if selected:
                proposal = self._load(
                    db, "proposals", pb.Proposal, "proposal_id", selected["proposal_id"]
                )
                selected_source = reference(proposal.source.bundle)
        audit = {
            "schema_version": 1,
            "task_id": task_id,
            "policy": reference(policy_ref),
            "settings": asdict(policy),
            "voting_rule": "correctness_and_critic_unanimous_no_author_vote",
            "tie_breakers": TIE_BREAKERS,
            "baseline_job_id": baseline.job_id,
            "status": "blocked"
            if blocked
            else "pending"
            if pending
            else "selected"
            if selected
            else "retained_baseline",
            "reason": "baseline_invalidated"
            if blocked
            else "evaluation_pending"
            if pending
            else "weighted_reliable_improvement"
            if selected
            else "no_reliable_improvement",
            "selected_job_id": None
            if blocked or pending
            else selected["verification_job_id"]
            if selected
            else baseline.job_id,
            "selected_attempt": None
            if blocked or pending
            else selected["verification_attempt"]
            if selected
            else baseline.attempt,
            "selected_proposal_id": None if selected is None else selected["proposal_id"],
            "selected_source": selected_source,
            "ranking": [c["proposal_id"] for c in unique],
            "candidates": candidates,
        }
        if debate is not None:
            audit["debate"] = {
                "policy": reference(self._ref(db, debate["policy_key"])),
                "agent_calls": debate["calls"],
                "heldout": None
                if debate["final_report_key"] is None
                else reference(self._ref(db, debate["final_report_key"])),
            }
            if heldout_failed and not blocked and not pending:
                audit["reason"] = "heldout_evaluation_failed"
        return audit

    def preview_debate_selection(self, task_id):
        with self._transaction(write=False) as db:
            require(
                db.execute("SELECT 1 FROM debate_runs WHERE task_id=?", (task_id,)).fetchone()
                is not None,
                "unknown debate",
            )
            return self._selection_audit(db, task_id, provisional=True)

    def preview_selection(self, task_id):
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT outcome_key FROM selection_sessions WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is not None and row[0] is not None:
                return json.loads(self.artifacts.get(self._ref(db, row[0])))
            return self._selection_audit(db, task_id)

    def _selection_event_guard(self, db, task_id, proposal_id):
        self._assert_managed_execution(db, task_id, proposal_id)
        session = db.execute(
            "SELECT 1 FROM selection_sessions WHERE task_id=?", (task_id,)
        ).fetchone()
        if session is None:
            return
        require(
            self._selection_member(db, task_id, proposal_id) is not None,
            "proposal is not in the frozen selection cohort",
        )
        if db.execute(
            "SELECT 1 FROM selection_progress WHERE task_id=? AND proposal_id=?",
            (task_id, proposal_id),
        ).fetchone():
            raise StateError("candidate evaluation is already committed")

    def selection_report(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            row = db.execute(
                "SELECT outcome_key FROM selection_sessions WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None or row[0] is None:
                return None
            ref = self._ref(db, row[0])
            return ref, json.loads(self.artifacts.get(ref))

    def _save_selection_audit(self, db, task_id, audit):
        ref = self.artifacts.put(json.dumps(audit, sort_keys=True, allow_nan=False).encode())
        previous = db.execute(
            "SELECT outcome_key FROM selection_sessions WHERE task_id=?", (task_id,)
        ).fetchone()
        if previous is not None and previous[0] is not None:
            if previous[0] != ref.key:
                raise ConflictError("selection audit is immutable")
            return
        self._artifact(db, task_id, ref, "selection-result")
        db.execute(
            "UPDATE selection_sessions SET outcome_key=? WHERE task_id=?", (ref.key, task_id)
        )

    def record_blocked_selection(self, task_id):
        with self._transaction() as db:
            previous = db.execute(
                "SELECT outcome_key FROM selection_sessions WHERE task_id=?", (task_id,)
            ).fetchone()
            if previous is not None and previous[0] is not None:
                return json.loads(self.artifacts.get(self._ref(db, previous[0])))
            audit = self._selection_audit(db, task_id)
            require(
                audit["status"] == "blocked", "only failed references produce blocked selection"
            )
            self._save_selection_audit(db, task_id, audit)
        return audit

    def _guard_weighted_decision(self, db, task_id, job_id, attempt, reason, scores):
        if (
            db.execute("SELECT 1 FROM selection_sessions WHERE task_id=?", (task_id,)).fetchone()
            is None
        ):
            return
        audit = self._selection_audit(db, task_id)
        require(
            audit["status"] not in ("pending", "blocked"),
            "selection cohort is incomplete or invalid",
        )
        expected = self.selection_scores(audit)
        if (job_id, attempt, reason, scores) != (
            audit["selected_job_id"],
            audit["selected_attempt"],
            audit["reason"],
            expected,
        ):
            raise StateError("decision does not match frozen weighted selection policy")
        self._save_selection_audit(db, task_id, audit)

    @staticmethod
    def selection_scores(audit):
        if audit["selected_proposal_id"] is None:
            return {
                "performance_gain": 0.0,
                "memory_gain": 0.0,
                "weighted_score": 0.0,
                "weighted_lower_bound": 0.0,
            }
        selected = next(
            c for c in audit["candidates"] if c["proposal_id"] == audit["selected_proposal_id"]
        )
        stats = selected["statistics"]
        return {
            "performance_gain": stats["performance_gain"],
            "memory_gain": stats["memory_gain"],
            "weighted_score": stats["weighted_point_score"],
            "weighted_lower_bound": stats["weighted_lower_bound"],
        }
