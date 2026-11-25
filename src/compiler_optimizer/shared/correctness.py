"""Bind persisted correctness evidence to the exact job and verification plan."""

import re

from compiler_optimizer.shared.manifest import require


def validate_evidence(report, result, job, submission, plan):
    require(
        isinstance(report, dict)
        and type(report.get("schema_version")) is int
        and report["schema_version"] == 1,
        "invalid correctness report schema",
    )
    expected = {
        "task_id": job.meta.task_id,
        "job_id": job.job_id,
        "proposal_id": job.proposal_id,
        "plan_sha256": job.verification_plan.sha256,
        "inputs_sha256": job.verification_inputs.sha256,
        "baseline_source_sha256": submission.source.bundle.sha256,
        "candidate_source_sha256": job.source.bundle.sha256,
        "compiler_image": job.compiler.image_digest,
        "sanitizer": plan["sanitizer"],
        "log_sha256": result.log.sha256,
    }
    require(
        all(report.get(key) == value for key, value in expected.items()),
        "correctness report does not match job/source/plan/log",
    )
    if job.HasField("ir"):
        import json

        from compiler_optimizer.workers.llvm_ir import descriptor

        require(
            report.get("ir") == json.loads(descriptor(job.ir)), "correctness LLVM identity mismatch"
        )
    else:
        require("ir" not in report, "unexpected LLVM correctness evidence")
    successful = result.WhichOneof("outcome") == "success"
    if job.compiler.target == "linux-riscv64":
        require(
            report.get("execution")
            == {"target": "linux-riscv64", "mode": "qemu-user", "sanitizer_mode": "trap"},
            "RISC-V evidence must identify the emulator and trapping sanitizer",
        )
    require(
        type(report.get("correctness_passed")) is bool
        and report["correctness_passed"] == successful,
        "correctness report/result mismatch",
    )
    require(type(report.get("baseline_invalid")) is bool, "missing baseline validity evidence")
    checks = report.get("checks")
    require(
        isinstance(checks, list)
        and len(checks) <= 512
        and all(isinstance(check, dict) for check in checks),
        "invalid correctness checks",
    )
    if successful:
        require(
            report.get("status") == "verified" and not report["baseline_invalid"],
            "successful verification requires a valid reference",
        )
        ids = {case.id for case in job.manifest.tests} | set(job.additional_test_ids)
        for phase in ("differential", "sanitizer"):
            rows = [check for check in checks if check.get("phase") == phase]
            require(
                len(rows) == len(ids)
                and {row.get("test_id") for row in rows} == ids
                and all(row.get("passed") is True for row in rows),
                "correctness evidence is incomplete",
            )
        for phase in (
            "baseline_compile",
            "candidate_compile",
            "baseline_sanitizer_compile",
            "candidate_sanitizer_compile",
        ):
            rows = [check for check in checks if check.get("phase") == phase]
            require(
                len(rows) == 1
                and isinstance(rows[0].get("binary_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", rows[0]["binary_sha256"]) is not None,
                "missing verification build evidence",
            )
            if job.HasField("ir"):
                profile = "normal" if "sanitizer" not in phase else plan["sanitizer"]
                variants = [v for v in job.ir.variants if v.build_profile == profile]
                require(len(variants) == 1, "missing LLVM build profile")
                ref = (
                    variants[0].baseline_bitcode
                    if phase.startswith("baseline")
                    else variants[0].optimized_bitcode
                )
                require(
                    rows[0].get("ir_bitcode_sha256") == ref.sha256
                    and rows[0].get("ir_build_profile") == profile,
                    "verification executable differs from assigned LLVM bitcode",
                )
