"""Verify a stored proposal against its immutable baseline before benchmarking."""

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.recovery import RecoveryTerminal
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import StorageError
from compiler_optimizer.workers.baseline import REASONS, metadata
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import bundle, package_from_files, unbundle
from compiler_optimizer.workers.targets import DEFAULT_IMAGES
from compiler_optimizer.workers.verification_plan import VerificationPlan, generate_cases


class Rejected(Exception):
    def __init__(self, details, *, baseline=False):
        self.details = details
        self.baseline = baseline


def _snapshot(repository, task_id):
    task = repository.get_task(task_id)
    require(task["status"] == "running", "candidate verification requires a running task")
    original = package_from_files(unbundle(repository.artifacts.get(task["submission"].package)))
    require(original.manifest == task["submission"].manifest, "stored package manifest mismatch")
    require(
        bundle(original.sources) == repository.artifacts.get(task["submission"].source.bundle),
        "stored package/source baseline mismatch",
    )
    baselines = [
        result
        for result in repository.list_results(task_id)
        if result.WhichOneof("subject") == "baseline" and result.WhichOneof("outcome") == "success"
    ]
    require(bool(baselines), "a passing baseline is required before verifying proposals")
    return task, original, baselines[-1]


def register_candidate(
    repository, task_id, candidate_directory, *, rationale="Manual source proposal"
):
    """Convenience intake for a source-only directory; fixtures/settings cannot change."""
    task, original, _ = _snapshot(repository, task_id)
    sources = read_source_directory(candidate_directory, original)
    require(
        not set(sources) & (set(original.files) - set(original.sources)),
        "candidate headers cannot overwrite fixtures or metadata",
    )
    merged = {
        name: contents for name, contents in original.files.items() if name not in original.sources
    }
    merged.update(sources)
    package_from_files(merged)
    source_data = bundle(sources)
    require(source_data != original.source_bundle, "candidate must change the baseline source")
    proposal_id = str(uuid4())
    submission = task["submission"]
    request = pb.OptimizationRequest(
        meta=metadata(task_id),
        proposal_id=proposal_id,
        role=pb.PERFORMANCE,
        manifest=submission.manifest,
        source=submission.source,
        compiler=submission.compiler,
    )
    proposal = pb.Proposal(
        meta=metadata(task_id),
        proposal_id=proposal_id,
        role=pb.PERFORMANCE,
        source=pb.SourceVersion(
            version=2,
            bundle=repository.artifacts.put(source_data),
            parent_source_sha256=submission.source.bundle.sha256,
        ),
        rationale=rationale,
        transformation="manual-source-change",
        compiler=submission.compiler,
    )
    repository.save_request(request)
    repository.save_proposal(proposal)
    return proposal_id


def read_source_directory(directory, original):
    # Directory inspection stays identical to submission ingestion, without ever
    # copying candidate files or running its build scripts on the host.
    from compiler_optimizer.workers.package import read_directory

    files = read_directory(directory)
    required = set(original.manifest.sources)
    require(required <= files.keys(), "candidate is missing a translation unit")
    require(
        all(name in required or name.endswith((".h", ".hpp")) for name in files),
        "candidate directory may contain only listed sources and local headers",
    )
    return files


def run_verification(
    repository, task_id, proposal_id, runner, plan=None, *, job_id=None, attempt=1
):
    plan = VerificationPlan() if plan is None else plan
    plan_data = plan.encode()
    task, original, baseline = _snapshot(repository, task_id)
    require(
        runner.compiler(original.manifest.language) == task["submission"].compiler,
        "verification must use the original compiler image and settings",
    )
    require(runner.supports_sanitizers, "compiler image lacks sanitizer verification support")
    if plan.sanitizer == "address-undefined":
        require(
            runner.native_amd64 and task["submission"].compiler.target == "linux-x86_64",
            "AddressSanitizer verification requires a native Linux amd64 worker",
        )
    proposals = [
        value for value in repository.list_proposals(task_id) if value.proposal_id == proposal_id
    ]
    require(len(proposals) == 1, "proposal is not stored under this task")
    proposal = proposals[0]
    if proposal.HasField("ir"):
        from compiler_optimizer.workers.llvm_ir import validate_artifacts

        validate_artifacts(repository.artifacts, proposal)
        require(
            {"normal", plan.sanitizer} <= {v.build_profile for v in proposal.ir.variants},
            "LLVM proposal lacks requested sanitizer variant",
        )
        candidate = original
    else:
        sources = unbundle(repository.artifacts.get(proposal.source.bundle))
        require(
            not set(sources) & (set(original.files) - set(original.sources)),
            "candidate bundle cannot overwrite fixtures or metadata",
        )
        require(
            set(original.manifest.sources) <= sources.keys()
            and all(
                name in original.manifest.sources or name.endswith((".h", ".hpp"))
                for name in sources
            ),
            "invalid candidate source-only bundle",
        )
        merged = {
            name: data for name, data in original.files.items() if name not in original.sources
        }
        merged.update(sources)
        candidate = package_from_files(merged)
    if plan.coverage is not None:
        require(
            not proposal.HasField("ir")
            and original.manifest.language == "cpp17"
            and runner.native_execution
            and runner.target != "linux-riscv64",
            "coverage contract requires native C++17 source proposals",
        )
    cases = generate_cases(original, plan)
    if plan.coverage is not None:
        require(all(case.id != "coverage-fuzz" for case in cases), "reserved coverage test ID")
    inputs_data = json.dumps(
        {"schema_version": 1, "cases": [case.record() for case in cases]}, sort_keys=True
    ).encode()
    meta = metadata(task_id)
    limits = original.manifest.limits
    budget = 4 * (limits.compile_wall_seconds + 30) + (
        4 * len(cases) + 2 * len(original.manifest.benchmark_test_ids)
    ) * (limits.run_wall_seconds + 30)
    if plan.coverage is not None:
        budget += limits.compile_wall_seconds + plan.coverage.seconds + 45
    job = pb.VerificationJob(
        meta=meta,
        job_id=job_id or str(uuid4()),
        proposal_id=proposal_id,
        source=proposal.source,
        manifest=original.manifest,
        compiler=proposal.compiler,
        attempt=attempt,
        deadline_unix_ms=meta.created_at_unix_ms + budget * 1000,
        additional_test_ids=[case.id for case in cases if case.kind != "supplied"],
        verification_plan=repository.artifacts.put(plan_data),
        verification_inputs=repository.artifacts.put(inputs_data),
    )
    if proposal.HasField("ir"):
        job.ir.CopyFrom(proposal.ir)
    repository.save_job(job)
    result = pb.VerificationResult(
        meta=metadata(task_id),
        job_id=job.job_id,
        worker_id=getattr(runner, "worker_id", None) or str(uuid4()),
        attempt=attempt,
        proposal_id=proposal_id,
        source=proposal.source,
        compiler=job.compiler,
        hardware=runner.hardware,
    )
    report = {
        "schema_version": 1,
        "task_id": task_id,
        "job_id": job.job_id,
        "proposal_id": proposal_id,
        "baseline_job_id": baseline.job_id,
        "baseline_source_sha256": task["submission"].source.bundle.sha256,
        "candidate_source_sha256": proposal.source.bundle.sha256,
        "compiler_image": job.compiler.image_digest,
        "sanitizer": plan.sanitizer,
        "plan_sha256": job.verification_plan.sha256,
        "inputs_sha256": job.verification_inputs.sha256,
        "correctness_passed": False,
        "baseline_invalid": False,
        "checks": [],
    }
    if proposal.HasField("ir"):
        from compiler_optimizer.workers.llvm_ir import descriptor

        report["ir"] = json.loads(descriptor(proposal.ir))
    logs = {"schema_version": 1, "invocations": []}
    if job.compiler.target == "linux-riscv64":
        report["execution"] = {
            "target": job.compiler.target,
            "mode": "qemu-user",
            "sanitizer_mode": "trap",
        }
    log_bytes = 0

    def check(invocation, phase, *, case=None, baseline_role=False):
        nonlocal log_bytes
        entry = {"phase": phase, "test_id": None if case is None else case.id, **invocation.log()}
        log_bytes += len(json.dumps(entry).encode())
        if log_bytes > 64 * 1024**2:
            raise InfrastructureError("verification invocation evidence exceeds 64 MiB limit")
        logs["invocations"].append(entry)
        if invocation.failure is None:
            return
        if case is None and (baseline_role or invocation.failure != "nonzero_exit"):
            raise InfrastructureError("compiler/toolchain failure during " + phase)
        reason = REASONS[invocation.failure]
        stage = pb.COMPILE if case is None else pb.TEST
        diagnostic = invocation.stderr
        if (
            b"Shadow memory range interleaves" in diagnostic
            or b"ReserveShadowMemoryRange failed" in diagnostic
        ):
            raise InfrastructureError("sanitizer runtime cannot initialize on this worker")
        if (
            case is not None
            and "sanitizer" in phase
            and any(
                marker in diagnostic
                for marker in (
                    b"runtime error:",
                    b"ERROR: AddressSanitizer",
                    b"UndefinedBehaviorSanitizer",
                )
            )
        ):
            reason = pb.SANITIZER_FAILURE
        elif case is None and invocation.failure == "nonzero_exit":
            reason = pb.COMPILATION_ERROR
        details = pb.FailureDetails(
            reason=reason, stage=stage, message=phase + ": " + invocation.failure
        )
        if case:
            details.test_id = case.id
        if invocation.returncode < 0:
            details.signal_number = -invocation.returncode
        elif invocation.returncode > 0:
            details.exit_code = invocation.returncode
        raise Rejected(details, baseline=baseline_role)

    def compile_program(package, phase, profile, *, baseline_role=False):
        if proposal.HasField("ir"):
            variant = next(v for v in proposal.ir.variants if v.build_profile == profile)
            bitcode = variant.baseline_bitcode if baseline_role else variant.optimized_bitcode
            invocation = runner.llvm_stage(
                package,
                "lower",
                pipeline_id=proposal.ir.pipeline_id,
                modules=[repository.artifacts.get(bitcode)],
                build_profile=profile,
            )
        else:
            invocation = runner.compile(package, build_profile=profile)
        check(invocation, phase, baseline_role=baseline_role)
        if not invocation.binary:
            raise InfrastructureError("compiler did not produce verification executable")
        reference = repository.artifacts.put(invocation.binary)
        repository.attach_artifact(
            task_id,
            reference,
            kind="correctness" if profile != "normal" else "binary",
            job_id=job.job_id,
            attempt=attempt,
        )
        build = {"phase": phase, "binary_sha256": reference.sha256}
        if proposal.HasField("ir"):
            build.update(ir_bitcode_sha256=bitcode.sha256, ir_build_profile=profile)
        report["checks"].append(build)
        return invocation.binary

    def execute_program(binary, package, case, phase, profile, *, baseline_role=False):
        # The runner receives one immutable input at a time, never expected outputs in its sandbox.
        fixture = pb.TestCase(
            id=case.id,
            stdin="verification.stdin",
            expected_stdout="verification.stdout",
            args=list(case.args),
        )
        executable_package = replace(
            package, files={**package.files, "verification.stdin": case.stdin}
        )
        invocation = runner.execute(binary, fixture, executable_package, build_profile=profile)
        check(invocation, phase, case=case, baseline_role=baseline_role)
        return invocation

    def mismatch(case, phase, *, baseline_role=False):
        raise Rejected(
            pb.FailureDetails(
                reason=pb.OUTPUT_MISMATCH, stage=pb.TEST, test_id=case.id, message=phase
            ),
            baseline=baseline_role,
        )

    try:
        original_binary = compile_program(
            original, "baseline_compile", "normal", baseline_role=True
        )
        candidate_binary = compile_program(candidate, "candidate_compile", "normal")
        reference_outputs = {}
        for case in cases:
            first = execute_program(
                original_binary, original, case, "baseline_reference", "normal", baseline_role=True
            )
            if case.expected is not None and not plan.matches(first.stdout, case.expected):
                mismatch(case, "baseline differs from supplied expected output", baseline_role=True)
            reference_outputs[case.id] = first.stdout
            second = execute_program(
                candidate_binary, candidate, case, "candidate_differential", "normal"
            )
            if not plan.matches(second.stdout, first.stdout) or (
                case.expected is not None and not plan.matches(second.stdout, case.expected)
            ):
                mismatch(case, "candidate differs from baseline or supplied expected output")
            report["checks"].append(
                {"phase": "differential", "test_id": case.id, "kind": case.kind, "passed": True}
            )
        sanitized_baseline = compile_program(
            original, "baseline_sanitizer_compile", plan.sanitizer, baseline_role=True
        )
        sanitized_candidate = compile_program(
            candidate, "candidate_sanitizer_compile", plan.sanitizer
        )
        for case in cases:
            for binary, package, role in (
                (sanitized_baseline, original, "baseline"),
                (sanitized_candidate, candidate, "candidate"),
            ):
                invocation = execute_program(
                    binary,
                    package,
                    case,
                    role + "_sanitizer",
                    plan.sanitizer,
                    baseline_role=role == "baseline",
                )
                if not plan.matches(invocation.stdout, reference_outputs[case.id]):
                    mismatch(
                        case,
                        role + " instrumented output differs from baseline",
                        baseline_role=role == "baseline",
                    )
            report["checks"].append({"phase": "sanitizer", "test_id": case.id, "passed": True})
        if plan.coverage is not None:
            from compiler_optimizer.workers.fuzzing import run_fuzz

            campaign, campaign_ref = run_fuzz(
                repository, task_id, proposal_id, runner, policy=plan.coverage, _verified_job=job
            )
            report["coverage"] = {
                "status": campaign["status"],
                "report_sha256": campaign_ref.sha256,
                "policy": campaign["policy"],
            }
            if campaign["status"] != "bounded-no-finding":
                invocation = campaign["invocation"]
                details = pb.FailureDetails(
                    reason=REASONS[invocation["failure"]],
                    stage=pb.TEST,
                    test_id="coverage-fuzz",
                    message="coverage-guided harness rejected candidate; report "
                    + campaign_ref.sha256,
                )
                if invocation["returncode"] < 0:
                    details.signal_number = -invocation["returncode"]
                elif invocation["returncode"] > 0:
                    details.exit_code = invocation["returncode"]
                raise Rejected(details)
        report["correctness_passed"] = True
        success = pb.VerificationSuccess(passed_test_ids=[case.id for case in cases])
        # Step 6's single-sample measurements remain provisional until step 9.
        for case in cases:
            if case.id not in original.manifest.benchmark_test_ids:
                continue
            invocation = execute_program(
                candidate_binary, candidate, case, "candidate_measure", "normal"
            )
            if not plan.matches(invocation.stdout, reference_outputs[case.id]) or (
                case.expected is not None and not plan.matches(invocation.stdout, case.expected)
            ):
                mismatch(case, "candidate output changed during measurement")
            if invocation.peak_memory_bytes <= 0:
                raise InfrastructureError("missing candidate peak memory measurement")
            success.benchmarks.add(
                test_id=case.id,
                wall_seconds=[invocation.wall_seconds],
                peak_memory_bytes=invocation.peak_memory_bytes,
                warmup_runs=0,
            )
        result.success.CopyFrom(success)
    except Rejected as error:
        result.failure.CopyFrom(error.details)
        report["baseline_invalid"] = error.baseline
    except InfrastructureError as error:
        result.failure.CopyFrom(
            pb.FailureDetails(
                reason=pb.INFRASTRUCTURE_ERROR,
                stage=pb.INFRASTRUCTURE,
                message=str(error).encode()[:4096].decode(errors="ignore"),
            )
        )
    report["correctness_passed"] = result.WhichOneof("outcome") == "success"
    report["status"] = "verified" if report["correctness_passed"] else "rejected"
    result.meta.created_at_unix_ms = max(job.meta.created_at_unix_ms, time.time_ns() // 1_000_000)
    result.log.CopyFrom(repository.artifacts.put(json.dumps(logs, sort_keys=True).encode()))
    report["log_sha256"] = result.log.sha256
    result.correctness_report.CopyFrom(
        repository.artifacts.put(json.dumps(report, sort_keys=True).encode())
    )
    if repository.save_result(result):
        from compiler_optimizer.monitoring.events import verification_committed

        verification_committed(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--candidate", help="Source-only candidate directory")
    choice.add_argument("--proposal-id", help="Existing proposal ID under this task")
    parser.add_argument(
        "--plan", help="Trusted verification-plan JSON; defaults to exact output + UBSan"
    )
    parser.add_argument("--benchmark-plan", help="Benchmark plan defining quarantine context")
    parser.add_argument("--state", default="var")
    parser.add_argument("--image")
    parser.add_argument("--target", choices=tuple(DEFAULT_IMAGES), default="linux-x86_64")
    args = parser.parse_args()
    try:
        plan = (
            VerificationPlan.parse(Path(args.plan).read_bytes())
            if args.plan
            else VerificationPlan()
        )
        repository = Repository(args.state)
        runner = DockerRunner(
            args.image or DEFAULT_IMAGES[args.target],
            target=args.target,
            watchdog_directory=Path(args.state) / "watchdogs",
        )
        task, original, _ = _snapshot(repository, args.task_id)
        require(
            runner.compiler(original.manifest.language) == task["submission"].compiler,
            "use the exact baseline toolchain image; rebuild baseline after image changes",
        )
        proposal_id = args.proposal_id or register_candidate(
            repository, args.task_id, args.candidate
        )
        from compiler_optimizer.shared.benchmarking import BenchmarkPlan
        from compiler_optimizer.workers.execution import execute_verification

        benchmark_plan = (
            BenchmarkPlan.parse(Path(args.benchmark_plan).read_bytes())
            if args.benchmark_plan
            else BenchmarkPlan()
        )
        result = execute_verification(
            repository, args.task_id, proposal_id, runner, plan, benchmark_plan
        )
    except (
        ContractError,
        InfrastructureError,
        StorageError,
        OSError,
        ValueError,
        RecoveryTerminal,
    ) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    passed = result.WhichOneof("outcome") == "success"
    print(
        json.dumps(
            {
                "task_id": args.task_id,
                "proposal_id": proposal_id,
                "job_id": result.job_id,
                "verification": "passed" if passed else "rejected",
                "failure": None if passed else pb.FailureReason.Name(result.failure.reason),
                "report_sha256": result.correctness_report.sha256,
            }
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
