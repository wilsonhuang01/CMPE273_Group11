"""Resume automated verification, required reviews, and organized measurements."""

import json
from contextlib import nullcontext

from compiler_optimizer.agents.benchmark import BenchmarkAgent
from compiler_optimizer.agents.correctness import CorrectnessAgent
from compiler_optimizer.agents.critic import CriticAgent
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import StateError
from compiler_optimizer.workers.benchmark import run_benchmark
from compiler_optimizer.workers.verification_plan import VerificationPlan
from compiler_optimizer.workers.verify import run_verification


def evaluate_proposal(
    repository,
    task_id,
    proposal_id,
    runner,
    *,
    review_backends=None,
    configuration=None,
    recovery=None,
    verification_only=False,
    agent_client=None,
):
    run = repository.get_pipeline(task_id)
    if configuration is None:
        require(run is not None and run["proposal_id"] == proposal_id, "unknown pipeline proposal")
        config = run["configuration"]
    else:
        session = repository.get_selection_session(task_id)
        member = next((m for m in session["members"] if m["proposal_id"] == proposal_id), None)
        require(
            member is not None
            and configuration["verification_plan"] == member["verification_plan"]
            and configuration["benchmark_plan"] == member["benchmark_plan"],
            "worker configuration differs from frozen cohort",
        )
        config = configuration
    if recovery is not None:
        proposal = next(
            p for p in repository.list_proposals(task_id) if p.proposal_id == proposal_id
        )
        return recovery.evaluate(
            proposal,
            config,
            lambda guarded, force: _evaluate_stages(
                repository,
                task_id,
                proposal_id,
                guarded,
                run,
                config,
                review_backends,
                recovery_attempt=True,
                force_verification=force,
                verification_only=verification_only,
                agent_client=agent_client,
            ),
            scope="preparation" if verification_only else "evaluate",
        )
    return _evaluate_stages(
        repository,
        task_id,
        proposal_id,
        runner,
        run,
        config,
        review_backends,
        verification_only=verification_only,
        agent_client=agent_client,
    )


def _evaluate_stages(
    repository,
    task_id,
    proposal_id,
    runner,
    run,
    config,
    review_backends,
    *,
    recovery_attempt=False,
    force_verification=False,
    verification_only=False,
    agent_client=None,
):
    from compiler_optimizer.shared.recovery import infrastructure_failure
    from compiler_optimizer.workers.recovery_evidence import check_benchmark, check_verification

    verification_plan = VerificationPlan.parse(
        repository.artifacts.get(pb.ArtifactRef(**config["verification_plan"]))
    )
    benchmark_plan = BenchmarkPlan.parse(
        repository.artifacts.get(pb.ArtifactRef(**config["benchmark_plan"]))
    )
    results = [
        result
        for result in repository.list_results(task_id)
        if result.WhichOneof("subject") == "proposal_id" and result.proposal_id == proposal_id
    ]
    retry = (
        bool(results)
        and recovery_attempt
        and results[-1].WhichOneof("outcome") == "failure"
        and infrastructure_failure(results[-1].failure)
    )
    if results and not retry and not force_verification:
        verified = results[-1]
        job = next(
            job
            for job in repository.list_jobs(task_id)
            if job.job_id == verified.job_id and job.attempt == verified.attempt
        )
        require(
            job.verification_plan == pb.ArtifactRef(**config["verification_plan"]),
            "existing verification used a different pipeline plan",
        )
    else:
        if run is not None:
            repository.pipeline_phase(task_id, "verifying")
        prior = results[-1] if results and results[-1].WhichOneof("outcome") == "failure" else None
        verified = run_verification(
            repository,
            task_id,
            proposal_id,
            runner,
            verification_plan,
            job_id=prior.job_id if prior else None,
            attempt=prior.attempt + 1 if prior else 1,
        )
    if recovery_attempt:
        check_verification(repository, verified)
    if force_verification:
        # Passing or differently failing confirmation is not grounds for a shared blacklist.
        from compiler_optimizer.shared.recovery import RecoveryTerminal

        raise RecoveryTerminal({"reason": "unstable_program_failure"})
    benchmark = None
    work = None
    proposal = next(p for p in repository.list_proposals(task_id) if p.proposal_id == proposal_id)
    reviews = {}
    for role in config.get("required_reviews", []):
        reviewers = {"correctness": CorrectnessAgent, "critic": CriticAgent}
        require(role in reviewers, "required reviewer is not implemented")
        window = (
            runner.work_window(memory_mib=256) if hasattr(runner, "work_window") else nullcontext()
        )
        with window:
            reviews[role] = reviewers[role](repository, (review_backends or {}).get(role)).review(
                proposal, verified
            )
    cohort = repository.cohort_work(task_id)
    if verification_only or (cohort is not None and cohort["state"] in ("cancelling", "cancelled")):
        return verified, None, reviews, None
    if verified.WhichOneof("outcome") == "success" and all(
        review["verdict"] == "approve" for review in reviews.values()
    ):
        if repository.debate_limit(task_id):
            raise StateError("debate call/time budget exhausted before measurement")
        if proposal.compiler.target == "linux-riscv64":
            return verified, None, reviews, None
        if config["schema_version"] >= 2:
            window = (
                runner.work_window(memory_mib=256)
                if hasattr(runner, "work_window")
                else nullcontext()
            )
            with window:
                work = BenchmarkAgent(repository, agent_client).organize(
                    proposal, verified, pb.ArtifactRef(**config["benchmark_plan"])
                )
        reports = [
            record
            for record in repository.list_benchmarks(task_id)
            if record["job_id"] == verified.job_id and record["attempt"] == verified.attempt
        ]
        if reports:
            artifact = next(
                ref
                for ref in repository.list_artifacts(task_id)
                if ref["key"] == reports[-1]["report_key"]
            )
            benchmark = json.loads(
                repository.artifacts.get(
                    pb.ArtifactRef(
                        **{key: artifact[key] for key in ("key", "sha256", "size_bytes")}
                    )
                )
            )
            require(
                benchmark["plan"] == config["benchmark_plan"],
                "existing benchmark used a different pipeline plan",
            )
            if recovery_attempt:
                raw = json.loads(repository.artifacts.get(pb.ArtifactRef(**benchmark["raw"])))
                if raw["status"] == "failed" and raw.get("error", {}).get("kind") in (
                    "infrastructure",
                    "measurement",
                ):
                    benchmark = run_benchmark(
                        repository,
                        task_id,
                        verified.job_id,
                        runner,
                        benchmark_plan,
                        attempt=verified.attempt,
                    )
        else:
            if run is not None:
                repository.pipeline_phase(task_id, "benchmarking")
            benchmark = run_benchmark(
                repository,
                task_id,
                verified.job_id,
                runner,
                benchmark_plan,
                attempt=verified.attempt,
            )
        if recovery_attempt:
            check_benchmark(repository, benchmark)
    return verified, benchmark, reviews, work
