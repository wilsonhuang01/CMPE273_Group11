"""Deterministic, bounded measurement work derived exclusively from trusted frozen inputs."""

from uuid import NAMESPACE_URL, uuid5

from compiler_optimizer.shared.agent_roles import role_spec
from compiler_optimizer.shared.benchmarking import MAX_INVOCATIONS
from compiler_optimizer.shared.manifest import require


def measurement_work(submission, proposal, job, result, correctness, plan, plan_ref):
    require(result.WhichOneof("outcome") == "success", "measurement work requires passed tests")
    require(
        plan.objective == role_spec(proposal.role).objective,
        "measurement objective does not match generator role",
    )
    count = 2 * (plan.warmups + plan.repetitions) * len(job.manifest.benchmark_test_ids)
    require(0 < count <= MAX_INVOCATIONS, "measurement work exceeds invocation budget")
    work_id = str(
        uuid5(
            NAMESPACE_URL,
            f"compiler-optimizer:benchmark:{job.job_id}:{job.attempt}:{plan_ref.sha256}",
        )
    )
    return {
        "schema_version": 1,
        "work_id": work_id,
        "task_id": job.meta.task_id,
        "proposal_id": proposal.proposal_id,
        "verification_job_id": job.job_id,
        "verification_attempt": job.attempt,
        "correctness_report_sha256": result.correctness_report.sha256,
        "package_sha256": submission.package.sha256,
        "baseline_source_sha256": submission.source.bundle.sha256,
        "candidate_source_sha256": proposal.source.bundle.sha256,
        "compiler_image": job.compiler.image_digest,
        "plan": {"key": plan_ref.key, "sha256": plan_ref.sha256, "size_bytes": plan_ref.size_bytes},
        "objective": plan.objective,
        "test_ids": list(job.manifest.benchmark_test_ids),
        "binaries": {
            role: next(
                check["binary_sha256"]
                for check in correctness["checks"]
                if check["phase"] == role + "_compile"
            )
            for role in ("baseline", "candidate")
        },
        "warmup_pairs_per_case": plan.warmups,
        "measured_pairs_per_case": plan.repetitions,
        "maximum_invocations": count,
        "execution_budget_seconds": count * (job.manifest.limits.run_wall_seconds + 30) + 70,
        "run_memory_mib": job.manifest.limits.run_memory_mib,
        "output_kib": job.manifest.limits.output_kib,
        "target": job.compiler.target,
        "cpu_quota": 1,
        "requested_cpu_id": plan.cpu_id,
        "serial_execution": True,
        "normal_binaries_only": True,
        "qualification_requires_native_amd64": job.compiler.target == "linux-x86_64",
    }
