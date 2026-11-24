"""Coordinator-owned durable proposal outbox, checkpoints, and final pipeline reports."""

import fcntl
import json
import os
from contextlib import contextmanager
from uuid import UUID

from compiler_optimizer.shared.agent_roles import REQUIRED_REVIEWS, ROLES, role_spec
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.contracts import validate_proposal_for_request
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.measurement import measurement_work
from compiler_optimizer.shared.messaging import decode_message, encode_message
from compiler_optimizer.shared.patches import apply_patch
from compiler_optimizer.shared.reviews import review_context, review_operation, validate_review
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import ConflictError, StateError
from compiler_optimizer.workers.package import bundle, unbundle
from compiler_optimizer.workers.verification_plan import VerificationPlan


def reference(ref):
    return {"key": ref.key, "sha256": ref.sha256, "size_bytes": ref.size_bytes}


class PipelineStorage:
    def list_requests(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                decode_message(pb.OptimizationRequest, row[0])
                for row in db.execute(
                    "SELECT payload FROM requests WHERE task_id=? ORDER BY rowid", (task_id,)
                )
            ]

    def begin_agent_iteration(self, task_id, role, operation, input_ref):
        spec = role_spec(role)
        self.artifacts.get(input_ref)
        require(
            isinstance(operation, str) and 0 < len(operation) <= 128,
            "invalid agent operation identity",
        )
        with self._transaction() as db:
            self._task(db, task_id)
            prior = db.execute(
                "SELECT * FROM agent_iterations WHERE task_id=? AND role=? "
                "AND operation_id=? ORDER BY iteration DESC LIMIT 1",
                (task_id, role, operation),
            ).fetchone()
            if prior is not None:
                if prior["input_key"] != input_ref.key:
                    raise ConflictError("agent operation inputs changed")
                if prior["status"] == "completed":
                    row = db.execute(
                        "SELECT * FROM artifacts WHERE key=?", (prior["output_key"],)
                    ).fetchone()
                    return None, pb.ArtifactRef(**dict(row))
            task = self._task(db, task_id)
            require(
                task["status"] == "running"
                or (role in (pb.CORRECTNESS, pb.CRITIC) and task["status"] == "failed"),
                "agent requires an active task or a failed reference review",
            )
            count = db.execute(
                "SELECT COUNT(*) FROM agent_iterations WHERE task_id=? AND role=?", (task_id, role)
            ).fetchone()[0]
            scoped_count = count
            debate = db.execute("SELECT 1 FROM debate_runs WHERE task_id=?", (task_id,)).fetchone()
            if debate is not None:
                scoped_count = db.execute(
                    "SELECT COUNT(*) FROM agent_iterations "
                    "WHERE task_id=? AND role=? AND operation_id=?",
                    (task_id, role, operation),
                ).fetchone()[0]
            elif role in (pb.CORRECTNESS, pb.CRITIC, pb.BENCHMARK):
                subject = operation.split(":", 1)[0]
                require(
                    str(UUID(subject)) == subject,
                    "review/benchmark operation needs a proposal UUID",
                )
                scoped_count = db.execute(
                    "SELECT COUNT(*) FROM agent_iterations WHERE task_id=? "
                    "AND role=? AND (operation_id=? OR operation_id LIKE ?)",
                    (task_id, role, subject, subject + ":%"),
                ).fetchone()[0]
            if scoped_count >= spec.max_iterations:
                raise StateError(f"{spec.name} agent iteration limit reached")
            self._debate_call(db, task_id)
            self._artifact(db, task_id, input_ref, "agent-input", owner=f"{spec.name}/{count + 1}")
            db.execute(
                "INSERT INTO agent_iterations VALUES (?, ?, ?, ?, ?, NULL, 'started')",
                (task_id, role, count + 1, operation, input_ref.key),
            )
            return count + 1, None

    def end_agent_iteration(self, task_id, role, iteration, output_ref):
        spec = role_spec(role)
        with self._transaction() as db:
            task = self._task(db, task_id)
            require(
                task["status"] == "running"
                or (role in (pb.CORRECTNESS, pb.CRITIC) and task["status"] == "failed"),
                "agent requires an active task or a failed reference review",
            )
            self._artifact(
                db, task_id, output_ref, "agent-output", owner=f"{spec.name}/{iteration}"
            )
            cursor = db.execute(
                "UPDATE agent_iterations SET status='completed', output_key=? "
                "WHERE task_id=? AND role=? AND iteration=? AND status='started'",
                (output_ref.key, task_id, role, iteration),
            )
            if cursor.rowcount != 1:
                raise StateError("agent iteration is not active")

    def fail_agent_iteration(self, task_id, role, iteration):
        with self._transaction() as db:
            db.execute(
                "UPDATE agent_iterations SET status='failed' WHERE task_id=? AND role=? "
                "AND iteration=? AND status='started'",
                (task_id, role, iteration),
            )

    def list_agent_iterations(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM agent_iterations WHERE task_id=? ORDER BY role, iteration",
                    (task_id,),
                )
            ]

    def record_agent_rpc(self, task_id, role, operation, request_sha256, receipt):
        from uuid import uuid4

        role_spec(role)
        ref = self.artifacts.put(json.dumps(receipt, sort_keys=True, allow_nan=False).encode())
        call_id = str(uuid4())
        with self._transaction() as db:
            self._task(db, task_id)
            self._artifact(db, task_id, ref, "agent-rpc", owner=call_id)
            for name in ("request", "response"):
                if name in receipt:
                    evidence = pb.ArtifactRef(**receipt[name])
                    self.artifacts.get(evidence)
                    self._artifact(db, task_id, evidence, "agent-rpc-" + name, owner=call_id)
            db.execute(
                "INSERT INTO agent_rpc_calls VALUES(?,?,?,?,?,?,?)",
                (call_id, task_id, role, operation, request_sha256, receipt["status"], ref.key),
            )

    def list_agent_rpc_calls(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM agent_rpc_calls WHERE task_id=? ORDER BY rowid", (task_id,)
                )
            ]

    @contextmanager
    def pipeline_lock(self):
        """Serialize local milestone orchestrators; the database is never held during execution."""
        fd = os.open(self.root / "pipeline.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StateError("another pipeline owns this state directory") from error
            yield
        finally:
            os.close(fd)

    def get_pipeline(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            row = db.execute("SELECT * FROM pipeline_runs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["configuration"] = json.loads(result.pop("configuration_json"))
        return result

    def enqueue_pipeline(
        self, proposal, patch_ref, verification_ref, benchmark_ref, *, selection_policy_ref=None
    ):
        value = {
            "schema_version": 2,
            "required_reviews": [role_spec(role).name for role in REQUIRED_REVIEWS],
            "patch": reference(patch_ref),
            "verification_plan": reference(verification_ref),
            "benchmark_plan": reference(benchmark_ref),
        }
        if selection_policy_ref is not None:
            from compiler_optimizer.shared.selection import SelectionPolicy

            SelectionPolicy.parse(self.artifacts.get(selection_policy_ref))
            value.update(schema_version=3, selection_policy=reference(selection_policy_ref))
        configuration = json.dumps(
            value,
            sort_keys=True,
        )
        payload = encode_message(proposal)
        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM pipeline_runs WHERE task_id=?", (proposal.meta.task_id,)
            ).fetchone()
            if existing:
                if (existing["proposal_id"], existing["configuration_json"]) != (
                    proposal.proposal_id,
                    configuration,
                ):
                    raise ConflictError("task already has a different pipeline")
                if not self._seen(db, proposal, payload):
                    raise ConflictError("pipeline proposal changed")
                return False
            self._task(db, proposal.meta.task_id, running=True)
            if selection_policy_ref is not None:
                self._artifact(db, proposal.meta.task_id, selection_policy_ref, "selection-policy")
            request = self._load(
                db, "requests", pb.OptimizationRequest, "proposal_id", proposal.proposal_id
            )
            validate_proposal_for_request(proposal, request)
            require(
                role_spec(proposal.role).objective is not None,
                "pipeline requires a candidate-generating role",
            )
            patch, changed = apply_patch(
                self.artifacts.get(patch_ref),
                unbundle(self.artifacts.get(request.source.bundle)),
                request.source.bundle.sha256,
                compiler=request.compiler,
            )
            require(
                self.artifacts.get(proposal.source.bundle) == bundle(changed)
                and proposal.rationale == patch["rationale"]
                and proposal.transformation == patch["transformation"],
                "proposal does not match structured patch",
            )
            VerificationPlan.parse(self.artifacts.get(verification_ref))
            benchmark_plan = BenchmarkPlan.parse(self.artifacts.get(benchmark_ref))
            require(
                benchmark_plan.objective == role_spec(proposal.role).objective,
                "benchmark objective does not match generator role",
            )
            db.execute(
                "INSERT INTO proposals VALUES (?, ?, ?)",
                (proposal.proposal_id, proposal.meta.task_id, payload),
            )
            self._record(db, proposal, payload)
            self._artifact(
                db,
                proposal.meta.task_id,
                proposal.source.bundle,
                "source",
                owner=proposal.proposal_id,
            )
            for ref, kind in (
                (patch_ref, "patch"),
                (verification_ref, "correctness"),
                (benchmark_ref, "benchmark"),
            ):
                self._artifact(db, proposal.meta.task_id, ref, kind, owner=proposal.proposal_id)
            db.execute(
                "INSERT INTO pipeline_runs VALUES (?, ?, ?, 'queued', 0, NULL)",
                (proposal.meta.task_id, proposal.proposal_id, configuration),
            )
        return True

    def pipeline_phase(self, task_id, phase):
        if phase not in {"published", "verifying", "benchmarking"}:
            raise ValueError("invalid active pipeline phase")
        with self._transaction() as db:
            self._task(db, task_id, running=True)
            cursor = db.execute(
                "UPDATE pipeline_runs SET phase=?, published=1 "
                "WHERE task_id=? AND report_key IS NULL",
                (phase, task_id),
            )
            if cursor.rowcount != 1:
                raise StateError("pipeline is missing or already finished")

    def finish_pipeline(self, task_id, report_ref):
        report = json.loads(self.artifacts.get(report_ref))
        with self._transaction() as db:
            task = self._task(db, task_id)
            run = db.execute("SELECT * FROM pipeline_runs WHERE task_id=?", (task_id,)).fetchone()
            require(
                run is not None
                and report.get("schema_version") in (1, 2, 3)
                and report.get("task_id") == task_id
                and report.get("proposal_id") == run["proposal_id"]
                and report.get("status") == task["status"]
                and task["status"] in {"completed", "failed"},
                "invalid final pipeline report",
            )
            if run["report_key"]:
                if run["report_key"] != report_ref.key:
                    raise ConflictError("pipeline already has a different result")
                return False
            session = db.execute(
                "SELECT outcome_key FROM selection_sessions WHERE task_id=?", (task_id,)
            ).fetchone()
            if session is not None:
                require(
                    session[0] is not None
                    and report.get("selection")
                    == json.loads(self.artifacts.get(self._ref(db, session[0]))),
                    "pipeline selection audit mismatch",
                )
            decision = db.execute("SELECT * FROM decisions WHERE task_id=?", (task_id,)).fetchone()
            proposal = self._load(db, "proposals", pb.Proposal, "proposal_id", run["proposal_id"])
            config = json.loads(run["configuration_json"])
            require(
                report["schema_version"] == config["schema_version"], "pipeline schema mismatch"
            )
            require(
                report.get("patch") == config["patch"]
                and report.get("rationale") == proposal.rationale
                and report.get("transformation") == proposal.transformation
                and report.get("agent") == role_spec(proposal.role).name,
                "pipeline agent evidence mismatch",
            )
            self.artifacts.get(pb.ArtifactRef(**config["patch"]))
            rows = [
                decode_message(pb.VerificationResult, row[0])
                for row in db.execute(
                    "SELECT payload FROM results WHERE task_id=? ORDER BY rowid", (task_id,)
                )
            ]
            candidates = [
                value
                for value in rows
                if value.WhichOneof("subject") == "proposal_id"
                and value.proposal_id == proposal.proposal_id
            ]
            terminal = report.get("recovery")
            verified = None
            if terminal is not None:
                recovery = db.execute(
                    "SELECT * FROM recovery_jobs WHERE task_id=? AND proposal_id=? "
                    "AND scope='evaluate'",
                    (task_id, proposal.proposal_id),
                ).fetchone()
                require(
                    recovery is not None
                    and recovery["state"] in ("quarantined", "exhausted")
                    and recovery["outcome_key"] is not None
                    and terminal
                    == json.loads(self.artifacts.get(self._ref(db, recovery["outcome_key"]))),
                    "pipeline recovery receipt mismatch",
                )
                require(
                    report.get("verification") == "recovery_terminal"
                    and report.get("verification_job_id") is None
                    and report.get("correctness_report") is None
                    and report.get("benchmark") is None
                    and report.get("benchmark_work") is None
                    and report.get("reviews") == {}
                    and report.get("candidate_selected") is False
                    and report.get("failure")
                    == {"reason": "recovery_terminal", "message": terminal["reason"]},
                    "terminal pipeline cannot claim candidate evidence",
                )
            else:
                require(bool(candidates), "pipeline needs a verification result")
                verified = candidates[-1]
                passed = verified.WhichOneof("outcome") == "success"
                require(
                    report.get("verification_job_id") == verified.job_id
                    and report.get("verification") == ("passed" if passed else "rejected")
                    and report.get("correctness_report") == reference(verified.correctness_report),
                    "pipeline correctness evidence mismatch",
                )
                self.artifacts.get(verified.correctness_report)
                reviews = self._pipeline_reviews(db, task_id, proposal, verified)
                require(report.get("reviews", {}) == reviews, "pipeline review evidence mismatch")
                reviews_approved = all(value["verdict"] == "approve" for value in reviews.values())
                if passed and reviews_approved:
                    job = self._job(db, verified.job_id, verified.attempt)
                    work = self._pipeline_measurement_work(
                        db,
                        task_id,
                        proposal,
                        job,
                        verified,
                        pb.ArtifactRef(**config["benchmark_plan"]),
                    )
                    require(
                        report.get("benchmark_work") == work, "pipeline measurement work mismatch"
                    )
                    row = db.execute(
                        "SELECT a.* FROM artifacts a JOIN benchmark_runs b "
                        "ON b.report_key=a.key WHERE b.task_id=? AND b.job_id=? "
                        "AND b.attempt=? ORDER BY b.rowid DESC LIMIT 1",
                        (task_id, verified.job_id, verified.attempt),
                    ).fetchone()
                    require(
                        row is not None
                        and report.get("benchmark")
                        == json.loads(self.artifacts.get(pb.ArtifactRef(**dict(row)))),
                        "pipeline benchmark evidence mismatch",
                    )
                elif not passed:
                    require(
                        report.get("benchmark") is None
                        and report.get("failure")
                        == {
                            "reason": pb.FailureReason.Name(verified.failure.reason),
                            "message": verified.failure.message,
                        },
                        "pipeline failure evidence mismatch",
                    )
                else:
                    require(
                        report.get("benchmark") is None, "rejected reviews cannot reach measurement"
                    )
                if not passed or not reviews_approved:
                    require(
                        report.get("benchmark_work") is None,
                        "rejected candidate has measurement work",
                    )
            require(
                report.get("selected_job_id") == (None if decision is None else decision["job_id"]),
                "pipeline report/decision mismatch",
            )
            if decision is not None:
                row = db.execute(
                    "SELECT payload FROM results WHERE job_id=? AND attempt=?",
                    (decision["job_id"], decision["attempt"]),
                ).fetchone()
                selected = decode_message(pb.VerificationResult, row[0])
                require(
                    terminal is None or selected.WhichOneof("subject") == "baseline",
                    "terminal candidate cannot be selected",
                )
                require(
                    report.get("selected_source") == reference(selected.source.bundle)
                    and report.get("reason") == decision["reason"]
                    and report.get("candidate_selected")
                    == (verified is not None and selected.job_id == verified.job_id),
                    "pipeline selected source mismatch",
                )
            else:
                require(
                    terminal is None or selected.WhichOneof("subject") == "baseline",
                    "terminal candidate cannot be selected",
                )
                require(
                    report.get("selected_source") is None
                    and report.get("candidate_selected") is False,
                    "failed pipeline has no selection",
                )
            self._artifact(db, task_id, report_ref, "result", owner=run["proposal_id"])
            db.execute(
                "UPDATE pipeline_runs SET phase='completed', report_key=? WHERE task_id=?",
                (report_ref.key, task_id),
            )
        return True

    def _pipeline_reviews(self, db, task_id, proposal, result):
        run = db.execute(
            "SELECT * FROM pipeline_runs WHERE task_id=? AND proposal_id=?",
            (task_id, proposal.proposal_id),
        ).fetchone()
        if run is None:
            return self._selection_reviews(db, task_id, proposal, result)
        required = json.loads(run["configuration_json"]).get("required_reviews", [])
        reviews = {}
        context = review_context(proposal, result)
        for name in required:
            roles = [role for role, spec in ROLES.items() if spec.name == name]
            require(
                len(roles) == 1 and roles[0] in (pb.CORRECTNESS, pb.CRITIC),
                "unknown required review role",
            )
            row = db.execute(
                "SELECT a.* FROM artifacts a JOIN agent_iterations i ON i.output_key=a.key "
                "WHERE i.task_id=? AND i.role=? AND i.operation_id=? "
                "AND i.status='completed' ORDER BY i.iteration DESC LIMIT 1",
                (task_id, roles[0], review_operation(proposal, result)),
            ).fetchone()
            require(row is not None, "required agent review is missing")
            value = json.loads(self.artifacts.get(pb.ArtifactRef(**dict(row))))
            reviews[name] = validate_review(value, context, roles[0])
        if self._selection_member(db, task_id, proposal.proposal_id):
            reviews.update(self._selection_reviews(db, task_id, proposal, result))
        return reviews

    def _pipeline_measurement_work(self, db, task_id, proposal, job, result, plan_ref):
        run = db.execute(
            "SELECT * FROM pipeline_runs WHERE task_id=? AND proposal_id=?",
            (task_id, proposal.proposal_id),
        ).fetchone()
        if run is None or json.loads(run["configuration_json"])["schema_version"] == 1:
            return self._selection_work(db, task_id, proposal, job, result, plan_ref)
        config = json.loads(run["configuration_json"])
        require(config["benchmark_plan"] == reference(plan_ref), "measurement plan changed")
        row = db.execute(
            "SELECT a.* FROM artifacts a JOIN agent_iterations i ON i.output_key=a.key "
            "WHERE i.task_id=? AND i.role=? AND i.operation_id=? "
            "AND i.status='completed' ORDER BY i.iteration DESC LIMIT 1",
            (task_id, pb.BENCHMARK, review_operation(proposal, result)),
        ).fetchone()
        require(row is not None, "Benchmark Agent measurement work is missing")
        work = json.loads(self.artifacts.get(pb.ArtifactRef(**dict(row))))
        expected = measurement_work(
            self._submission(self._task(db, task_id)),
            proposal,
            job,
            result,
            json.loads(self.artifacts.get(result.correctness_report)),
            BenchmarkPlan.parse(self.artifacts.get(plan_ref)),
            plan_ref,
        )
        require(work == expected, "measurement work differs from frozen job/plan/binaries")
        return work

    def measurement_work(self, task_id, job_id, attempt, plan_ref):
        with self._transaction(write=False) as db:
            job, result, _ = self._verified_candidate(db, task_id, job_id, attempt)
            if self._selection_member(db, task_id, result.proposal_id) is not None:
                if db.execute(
                    "SELECT 1 FROM benchmark_runs WHERE task_id=? AND job_id=?", (task_id, job_id)
                ).fetchone() and not self.managed_benchmark_retry_allowed(
                    db, task_id, result.proposal_id, job_id
                ):
                    raise StateError(
                        "frozen selection permits one committed benchmark per candidate"
                    )
                self._selection_event_guard(db, task_id, result.proposal_id)
            proposal = self._load(db, "proposals", pb.Proposal, "proposal_id", result.proposal_id)
            return self._pipeline_measurement_work(db, task_id, proposal, job, result, plan_ref)

    def pipeline_report(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            row = db.execute(
                "SELECT a.* FROM artifacts a JOIN pipeline_runs p "
                "ON p.report_key=a.key WHERE p.task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            ref = pb.ArtifactRef(**dict(row))
            return ref, json.loads(self.artifacts.get(ref))
