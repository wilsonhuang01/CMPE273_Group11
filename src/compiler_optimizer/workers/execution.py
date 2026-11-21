"""Standalone execution adapters sharing the fleet's leases and quarantine identity."""

import time

from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.scheduler.recovery import RecoveryController
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.recovery import RecoveryTerminal, RetryDelivery
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.recovery_evidence import check_benchmark, check_verification


def local_recovery(repository, runner):
    return RecoveryController.local(repository, runner, Settings.from_env("worker"))


def wait_for_execution(action, recovery, task_id):
    """Honor durable backoff; retries and cache outages consume the frozen attempt budget."""
    try:
        while True:
            try:
                return action()
            except RetryDelivery as retry:
                retry.confirmed()
                time.sleep(retry.delay_ms / 1000)
    finally:
        recovery.publish_quarantines(task_id)


def _check_frozen_plans(repository, task_id, proposal_id, config):
    session = repository.get_selection_session(task_id)
    if session is not None:
        member = next((m for m in session["members"] if m["proposal_id"] == proposal_id), None)
        if member is not None:
            require(
                all(config[k] == member[k] for k in ("verification_plan", "benchmark_plan")),
                "execution plans differ from frozen cohort",
            )
    pipeline = repository.get_pipeline(task_id)
    if pipeline is not None and pipeline["proposal_id"] == proposal_id:
        require(
            all(
                config[k] == pipeline["configuration"][k]
                for k in ("verification_plan", "benchmark_plan")
            ),
            "execution plans differ from frozen pipeline",
        )
    jobs = [
        j
        for j in repository.list_jobs(task_id)
        if j.WhichOneof("subject") == "proposal_id" and j.proposal_id == proposal_id
    ]
    require(
        all(reference(j.verification_plan) == config["verification_plan"] for j in jobs),
        "verification plan is frozen",
    )


def execute_verification(
    repository, task_id, proposal_id, runner, plan, benchmark_plan, *, recovery=None
):
    from compiler_optimizer.workers.verify import run_verification

    recovery = recovery or local_recovery(repository, runner)
    proposals = [p for p in repository.list_proposals(task_id) if p.proposal_id == proposal_id]
    require(len(proposals) == 1, "proposal is not stored under this task")
    proposal = proposals[0]
    config = {
        "verification_plan": reference(repository.artifacts.put(plan.encode())),
        "benchmark_plan": reference(repository.artifacts.put(benchmark_plan.encode())),
    }

    _check_frozen_plans(repository, task_id, proposal_id, config)

    def verify(guarded, force):
        prior = [
            r
            for r in repository.list_results(task_id)
            if r.WhichOneof("subject") == "proposal_id" and r.proposal_id == proposal_id
        ]
        last = prior[-1] if prior else None
        result = run_verification(
            repository,
            task_id,
            proposal_id,
            guarded,
            plan,
            job_id=last.job_id if last else None,
            attempt=last.attempt + 1 if last else 1,
        )
        check_verification(repository, result)
        if force:
            raise RecoveryTerminal({"reason": "unstable_program_failure"})
        return result, None, {}, None

    return wait_for_execution(
        lambda: recovery.evaluate(proposal, config, verify, scope="verification"),
        recovery,
        task_id,
    )[0]


def execute_benchmark(repository, task_id, job_id, runner, plan, *, attempt=1, recovery=None):
    from compiler_optimizer.shared.v1 import contracts_pb2 as pb
    from compiler_optimizer.workers.benchmark import run_benchmark
    from compiler_optimizer.workers.verification_plan import VerificationPlan
    from compiler_optimizer.workers.verify import run_verification

    recovery = recovery or local_recovery(repository, runner)
    job, result, _ = repository.get_verified_candidate(task_id, job_id, attempt)
    proposal = next(
        p for p in repository.list_proposals(task_id) if p.proposal_id == job.proposal_id
    )
    config = {
        "verification_plan": reference(job.verification_plan),
        "benchmark_plan": reference(repository.artifacts.put(plan.encode())),
    }

    _check_frozen_plans(repository, task_id, proposal.proposal_id, config)

    def benchmark(guarded, force):
        if force:
            # A benchmark crash is confirmed by independent correctness execution,
            # never by measuring again for a more favorable score.
            prior = [
                r
                for r in repository.list_results(task_id)
                if r.WhichOneof("subject") == "proposal_id"
                and r.proposal_id == proposal.proposal_id
            ]
            last = prior[-1]
            confirmation = run_verification(
                repository,
                task_id,
                proposal.proposal_id,
                guarded,
                VerificationPlan.parse(
                    repository.artifacts.get(pb.ArtifactRef(**config["verification_plan"]))
                ),
                job_id=last.job_id,
                attempt=last.attempt + 1,
            )
            check_verification(repository, confirmation)
            raise RecoveryTerminal({"reason": "unstable_program_failure"})
        report = run_benchmark(repository, task_id, job_id, guarded, plan, attempt=attempt)
        check_benchmark(repository, report)
        return result, report, {}, None

    return wait_for_execution(
        lambda: recovery.evaluate(
            proposal, config, benchmark, scope=f"benchmark:{job_id}:{attempt}"
        ),
        recovery,
        task_id,
    )[1]
