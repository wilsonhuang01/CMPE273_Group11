"""Correctness Agent reviews recorded evidence; execution remains the authority."""

import json

from compiler_optimizer.agents.journal import AgentJournal
from compiler_optimizer.shared.contracts import validate_result_for_job
from compiler_optimizer.shared.correctness import validate_evidence
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.messaging import encode_message
from compiler_optimizer.shared.reviews import review_context, review_operation, validate_review
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.verification_plan import VerificationPlan


def checked_verification(repository, proposal, result):
    task_id = proposal.meta.task_id
    stored = [
        row
        for row in repository.list_results(task_id)
        if row.job_id == result.job_id and row.attempt == result.attempt
    ]
    require(
        len(stored) == 1 and encode_message(stored[0]) == encode_message(result),
        "review needs a stored verification result",
    )
    job = next(
        job
        for job in repository.list_jobs(task_id)
        if job.job_id == result.job_id and job.attempt == result.attempt
    )
    require(
        job.WhichOneof("subject") == "proposal_id"
        and job.proposal_id == proposal.proposal_id
        and job.source == proposal.source
        and job.HasField("verification_plan")
        and result.HasField("correctness_report"),
        "review lacks bound correctness evidence",
    )
    validate_result_for_job(result, job)
    report = json.loads(repository.artifacts.get(result.correctness_report))
    plan_data = repository.artifacts.get(job.verification_plan)
    VerificationPlan.parse(plan_data)
    repository.artifacts.get(job.verification_inputs)
    repository.artifacts.get(result.log)
    validate_evidence(
        report, result, job, repository.get_task(task_id)["submission"], json.loads(plan_data)
    )
    return report


class CorrectnessAgent:
    def __init__(self, repository, backend=None):
        self.repository, self.backend = repository, backend

    def review(self, proposal, result):
        evidence = checked_verification(self.repository, proposal, result)
        context = review_context(proposal, result)

        def action():
            if self.backend is not None:
                response = self.backend.review(
                    json.loads(json.dumps(context)), json.loads(json.dumps(evidence))
                )
                require(
                    isinstance(response, dict)
                    and set(response) == {"verdict", "summary", "findings"},
                    "review backend may return verdict, summary and findings only",
                )
            else:
                passed = context["automated_passed"]
                response = {
                    "verdict": "approve" if passed else "reject",
                    "summary": "Recorded fixed/generated tests and sanitizer checks passed."
                    if passed
                    else "Automated verification failed; candidate is rejected.",
                    "findings": [
                        {
                            "code": "bounded-evidence" if passed else "automated-failure",
                            "severity": "info" if passed else "error",
                            "message": "Approval covers the tested domain, not formal equivalence."
                            if passed
                            else pb.FailureReason.Name(result.failure.reason),
                        }
                    ],
                }
            return validate_review(
                {**context, "role": "correctness", **response}, context, pb.CORRECTNESS
            )

        record = AgentJournal(self.repository, pb.CORRECTNESS).run(
            proposal.meta.task_id, review_operation(proposal, result), context, action
        )
        return validate_review(record, context, pb.CORRECTNESS)
