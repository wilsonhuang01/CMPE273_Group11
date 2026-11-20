"""Compile, test, measure, and persist a C17/C++17 baseline using ephemeral Docker containers."""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.monitoring.events import verification_committed
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

REASONS = {
    "timeout": pb.TIMEOUT,
    "memory_limit": pb.MEMORY_LIMIT,
    "output_limit": pb.OUTPUT_LIMIT,
    "signal": pb.SIGNAL_TERMINATION,
    "nonzero_exit": pb.NONZERO_EXIT,
}


def metadata(task_id):
    return pb.Metadata(
        schema_version=1,
        message_id=str(uuid4()),
        task_id=task_id,
        created_at_unix_ms=time.time_ns() // 1_000_000,
    )


def run_baseline(package, repository, runner) -> pb.VerificationResult:
    """Synchronous coordinator/worker path; successful tasks remain running for agents."""
    task_id = str(uuid4())
    manifest = package.manifest
    compiler = runner.compiler(manifest.language)
    submission = pb.Submission(
        meta=metadata(task_id),
        manifest=manifest,
        compiler=compiler,
        package=repository.artifacts.put(package.package_bundle),
        source=pb.SourceVersion(version=1, bundle=repository.artifacts.put(package.source_bundle)),
    )
    repository.save_submission(submission)
    repository.transition(task_id, expected_revision=0, status="running", reason="Baseline started")
    job_meta = metadata(task_id)
    count = len(manifest.tests) + len(manifest.benchmark_test_ids)
    budget = (
        manifest.limits.compile_wall_seconds
        + manifest.limits.run_wall_seconds * count
        + 30 * (count + 1)
    )
    job = pb.VerificationJob(
        meta=job_meta,
        job_id=str(uuid4()),
        baseline=True,
        source=submission.source,
        manifest=manifest,
        compiler=compiler,
        attempt=1,
        deadline_unix_ms=job_meta.created_at_unix_ms + budget * 1000,
    )
    repository.save_job(job)
    result = pb.VerificationResult(
        meta=metadata(task_id),
        job_id=job.job_id,
        worker_id=str(uuid4()),
        attempt=1,
        baseline=True,
        source=job.source,
        compiler=compiler,
        hardware=runner.hardware,
    )
    logs = {
        "schema_version": 1,
        "task_id": task_id,
        "job_id": job.job_id,
        "compiler": {
            "image_digest": compiler.image_digest,
            "version": compiler.version,
            "flags": list(compiler.flags),
        },
        "invocations": [],
    }

    def failure(invocation, stage, test_id=None):
        reason = REASONS[invocation.failure]
        if stage == pb.COMPILE and invocation.failure == "nonzero_exit":
            reason = pb.COMPILATION_ERROR
        details = pb.FailureDetails(
            reason=reason, stage=stage, message="Baseline " + invocation.failure
        )
        if test_id is not None:
            details.test_id = test_id
        if invocation.returncode < 0:
            details.signal_number = -invocation.returncode
        elif invocation.returncode > 0:
            details.exit_code = invocation.returncode
        result.failure.CopyFrom(details)

    try:
        compiled = runner.compile(package)
        logs["invocations"].append({"stage": "compile", **compiled.log()})
        if compiled.failure:
            failure(compiled, pb.COMPILE)
        else:
            if not compiled.binary:
                raise InfrastructureError("compiler did not produce an executable")
            binary_ref = repository.artifacts.put(compiled.binary)
            repository.attach_artifact(
                task_id, binary_ref, kind="binary", job_id=job.job_id, attempt=1
            )
            success = pb.VerificationSuccess()
            for stage, cases in (
                (pb.TEST, list(manifest.tests)),
                (
                    pb.MEASURE,
                    [case for case in manifest.tests if case.id in manifest.benchmark_test_ids],
                ),
            ):
                for case in cases:
                    measured = runner.execute(compiled.binary, case, package)
                    logs["invocations"].append(
                        {
                            "stage": "test" if stage == pb.TEST else "measure",
                            "test_id": case.id,
                            **measured.log(),
                        }
                    )
                    if measured.failure:
                        failure(measured, stage, case.id)
                        break
                    if measured.stdout != package.files[case.expected_stdout]:
                        result.failure.CopyFrom(
                            pb.FailureDetails(
                                reason=pb.OUTPUT_MISMATCH,
                                stage=stage,
                                test_id=case.id,
                                message="Baseline stdout differs from supplied expected bytes",
                            )
                        )
                        break
                    if stage == pb.TEST:
                        success.passed_test_ids.append(case.id)
                    else:
                        if measured.peak_memory_bytes <= 0:
                            raise InfrastructureError("worker did not report peak memory")
                        success.benchmarks.add(
                            test_id=case.id,
                            wall_seconds=[measured.wall_seconds],
                            peak_memory_bytes=measured.peak_memory_bytes,
                            warmup_runs=0,
                        )
                if result.WhichOneof("outcome") == "failure":
                    break
            if result.WhichOneof("outcome") != "failure":
                result.success.CopyFrom(success)
    except InfrastructureError as error:
        result.failure.CopyFrom(
            pb.FailureDetails(
                reason=pb.INFRASTRUCTURE_ERROR, stage=pb.INFRASTRUCTURE, message=str(error)[:4096]
            )
        )
    result.meta.created_at_unix_ms = max(job.meta.created_at_unix_ms, time.time_ns() // 1_000_000)
    result.log.CopyFrom(repository.artifacts.put(json.dumps(logs, sort_keys=True).encode()))
    if repository.save_result(result):
        verification_committed(result)
    if result.WhichOneof("outcome") == "failure":
        repository.transition(
            task_id,
            expected_revision=1,
            status="failed",
            reason="Baseline rejected: " + pb.FailureReason.Name(result.failure.reason),
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help="Submission directory containing submission.json")
    parser.add_argument("--state", default="var", help="Coordinator-owned local storage directory")
    parser.add_argument("--image", help="Locally built compiler image")
    parser.add_argument("--target", choices=tuple(DEFAULT_IMAGES), default="linux-x86_64")
    args = parser.parse_args()
    try:
        package = read_package(args.package)
        runner = DockerRunner(
            args.image or DEFAULT_IMAGES[args.target],
            target=args.target,
            watchdog_directory=Path(args.state) / "watchdogs",
        )
        result = run_baseline(package, Repository(args.state), runner)
    except (ContractError, InfrastructureError) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    passed = result.WhichOneof("outcome") == "success"
    print(
        json.dumps(
            {
                "task_id": result.meta.task_id,
                "job_id": result.job_id,
                "baseline": "passed" if passed else "rejected",
                "failure": None if passed else pb.FailureReason.Name(result.failure.reason),
                "compiler_version": result.compiler.version,
                "image_digest": result.compiler.image_digest,
                "benchmarks": [
                    {
                        "test_id": item.test_id,
                        "wall_seconds": list(item.wall_seconds),
                        "peak_memory_bytes": item.peak_memory_bytes,
                    }
                    for item in result.success.benchmarks
                ],
            }
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
