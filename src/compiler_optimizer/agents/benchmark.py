"""Benchmark Agent organizes frozen work; sandbox workers perform the measurements."""

from compiler_optimizer.agents.correctness import checked_verification
from compiler_optimizer.agents.journal import AgentJournal
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.measurement import measurement_work
from compiler_optimizer.shared.reviews import review_operation
from compiler_optimizer.shared.v1 import contracts_pb2 as pb


class BenchmarkAgent:
    def __init__(self, repository):
        self.repository = repository

    def organize(self, proposal, result, plan_ref):
        repository = self.repository
        task_id = proposal.meta.task_id
        correctness = checked_verification(repository, proposal, result)
        require(result.WhichOneof("outcome") == "success", "Benchmark Agent requires passed tests")
        task = repository.get_task(task_id)
        if task["status"] == "running":
            repository.get_verified_candidate(task_id, result.job_id, result.attempt)
        job = next(
            job
            for job in repository.list_jobs(task_id)
            if job.job_id == result.job_id and job.attempt == result.attempt
        )
        plan = BenchmarkPlan.parse(repository.artifacts.get(plan_ref))
        expected = measurement_work(
            task["submission"], proposal, job, result, correctness, plan, plan_ref
        )
        work = AgentJournal(repository, pb.BENCHMARK).run(
            task_id, review_operation(proposal, result), expected, lambda: expected
        )
        require(work == expected, "cached measurement work differs from frozen inputs")
        return work
