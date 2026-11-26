"""Benchmark exact verified binaries with paired, CPU-pinned measurements."""

import argparse
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.shared.benchmarking import (
    MAX_INVOCATIONS,
    BenchmarkPlan,
    analyze,
    pair_order,
)
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.recovery import RecoveryTerminal
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import StorageError
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.verification_plan import VerificationPlan
from compiler_optimizer.workers.verify import _snapshot


class MeasurementFailure(Exception):
    def __init__(self, kind, role, message):
        self.error = {"kind": kind, "role": role, "message": message}


def _put(repository, value):
    return repository.artifacts.put(json.dumps(value, sort_keys=True, allow_nan=False).encode())


def _ref(ref):
    return {"key": ref.key, "sha256": ref.sha256, "size_bytes": ref.size_bytes}


def run_benchmark(repository, task_id, job_id, runner, plan=None, *, attempt=1):
    window = runner.measurement_window() if hasattr(runner, "measurement_window") else nullcontext()
    with window:
        return _run_benchmark(repository, task_id, job_id, runner, plan, attempt=attempt)


def _run_benchmark(repository, task_id, job_id, runner, plan=None, *, attempt=1):
    plan = BenchmarkPlan() if plan is None else plan
    plan_ref = repository.artifacts.put(plan.encode())
    job, result, correctness = repository.get_verified_candidate(task_id, job_id, attempt)
    require(
        job.compiler.target in ("linux-x86_64", "linux-aarch64"),
        "emulated RISC-V is correctness-only and cannot qualify performance",
    )
    repository.measurement_work(task_id, job_id, attempt, plan_ref)
    repository.begin_measurement(task_id, job_id, attempt, plan_ref)
    _, original, _ = _snapshot(repository, task_id)
    require(
        runner.compiler(original.manifest.language) == job.compiler,
        "benchmark must use the verified compiler image/settings",
    )
    test_ids = list(job.manifest.benchmark_test_ids)
    require(
        2 * (plan.warmups + plan.repetitions) * len(test_ids) <= MAX_INVOCATIONS,
        "benchmark exceeds invocation budget",
    )
    comparator = VerificationPlan.parse(repository.artifacts.get(job.verification_plan))
    binaries, digests = {}, {}
    artifacts = repository.list_artifacts(task_id)
    for role in ("baseline", "candidate"):
        digest = next(
            check["binary_sha256"]
            for check in correctness["checks"]
            if check["phase"] == role + "_compile"
        )
        refs = [
            ref
            for ref in artifacts
            if ref["sha256"] == digest
            and ref["kind"] == "binary"
            and ref["owner_id"] == f"{job_id}/{attempt}"
        ]
        require(bool(refs), "missing verified executable")
        binaries[role] = repository.artifacts.get(
            pb.ArtifactRef(**{key: refs[0][key] for key in ("key", "sha256", "size_bytes")})
        )
        digests[role] = digest
    raw = {
        "schema_version": 1,
        "status": "failed",
        "environment_before": None,
        "environment_after": None,
        "cases": {},
    }
    try:
        before = runner.benchmark_environment(plan.cpu_id)
        raw["environment_before"] = before
        for test_id in test_ids:
            case = next(case for case in job.manifest.tests if case.id == test_id)
            rows = raw["cases"][test_id] = {"warmups": [], "pairs": []}
            for phase, count in (("warmups", plan.warmups), ("pairs", plan.repetitions)):
                for index in range(count):
                    offset = 0 if phase == "warmups" else plan.warmups
                    pair = {"order": pair_order(plan, test_id, offset + index), "samples": {}}
                    rows[phase].append(pair)
                    outputs = {}
                    for role in pair["order"]:
                        invocation = runner.execute(
                            binaries[role],
                            case,
                            original,
                            cpu_id=before["cpu_id"],
                            **({"cpu_ids": before["cpu_ids"]} if "cpu_ids" in before else {}),
                        )
                        sandbox = invocation.sandbox or {}
                        removed = sandbox.get("watchdog", {}).get("state") == "removed"
                        if not removed or sandbox.get("cpu_id") != before["cpu_id"]:
                            raise InfrastructureError("measurement sandbox/cleanup unconfirmed")
                        valid = comparator.matches(
                            invocation.stdout, original.files[case.expected_stdout]
                        )
                        sample = {
                            "wall_seconds": invocation.wall_seconds,
                            "peak_memory_bytes": invocation.peak_memory_bytes,
                            "returncode": invocation.returncode,
                            "failure": invocation.failure,
                            "output_valid": valid,
                            "cpu_id": sandbox["cpu_id"],
                            "watchdog_removed": removed,
                            "build_profile": invocation.build_profile,
                            "stdout_sha256": hashlib.sha256(invocation.stdout).hexdigest(),
                            "stderr_sha256": hashlib.sha256(invocation.stderr).hexdigest(),
                        }
                        if "execution_profile" in before:
                            sample.update(
                                execution_profile=sandbox.get("execution_profile"),
                                cpu_ids=sandbox.get("cpu_ids"),
                                cpu_quota=sandbox.get("cpu_quota"),
                                threads=sandbox.get("threads"),
                                cpu_seconds=invocation.cpu_seconds,
                            )
                        pair["samples"][role] = sample
                        if invocation.failure or invocation.returncode:
                            raw["failure_invocation"] = invocation.log()
                            raise MeasurementFailure(
                                "program_failure",
                                role,
                                str(invocation.failure or invocation.returncode),
                            )
                        if not valid:
                            raw["failure_invocation"] = invocation.log()
                            raise MeasurementFailure("output_mismatch", role, test_id)
                        if invocation.wall_seconds <= 0 or invocation.peak_memory_bytes <= 0:
                            raise MeasurementFailure("measurement", None, "missing resource usage")
                        outputs[role] = invocation.stdout
                    if not comparator.matches(outputs["candidate"], outputs["baseline"]):
                        raise MeasurementFailure("output_mismatch", "candidate", test_id)
        raw["environment_after"] = runner.benchmark_environment(before["cpu_id"])
        raw["status"] = "completed"
    except MeasurementFailure as error:
        raw["error"] = error.error
    except InfrastructureError as error:
        raw["error"] = {"kind": "infrastructure", "role": None, "message": str(error)[:4096]}
    raw_ref = _put(repository, raw)
    require(raw_ref.size_bytes <= 64 * 1024**2, "benchmark evidence exceeds 64 MiB")
    report = {
        "schema_version": 1,
        "run_id": str(uuid4()),
        "task_id": task_id,
        "job_id": job_id,
        "attempt": attempt,
        "candidate_source_sha256": job.source.bundle.sha256,
        "correctness_sha256": result.correctness_report.sha256,
        "compiler_image": job.compiler.image_digest,
        "compiler": {
            "executable": job.compiler.executable,
            "version": job.compiler.version,
            "flags": list(job.compiler.flags),
            "target": job.compiler.target,
            "language": job.compiler.language,
        },
        "binaries": digests,
        "plan": _ref(plan_ref),
        "raw": _ref(raw_ref),
        "analysis": analyze(raw, plan, test_ids),
    }
    report_ref = _put(repository, report)
    repository.record_benchmark(report_ref)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--verification-job", required=True)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--state", default="var")
    parser.add_argument("--plan")
    parser.add_argument("--image", default="cmpe273-clang:18")
    parser.add_argument(
        "--target", default="linux-x86_64", choices=("linux-x86_64", "linux-aarch64")
    )
    parser.add_argument("--execution-profile", choices=("", "parallel-2"), default="")
    args = parser.parse_args()
    try:
        plan = BenchmarkPlan.parse(Path(args.plan).read_bytes()) if args.plan else BenchmarkPlan()
        repository = Repository(args.state)
        runner = DockerRunner(
            args.image,
            target=args.target,
            execution_profile=args.execution_profile,
            watchdog_directory=Path(args.state) / "watchdogs",
        )
        from compiler_optimizer.workers.execution import execute_benchmark

        report = execute_benchmark(
            repository, args.task_id, args.verification_job, runner, plan, attempt=args.attempt
        )
        print(json.dumps({"run_id": report["run_id"], **report["analysis"]}, sort_keys=True))
        raw = json.loads(repository.artifacts.get(pb.ArtifactRef(**report["raw"])))
        return 0 if raw["status"] == "completed" else 1
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


if __name__ == "__main__":
    raise SystemExit(main())
