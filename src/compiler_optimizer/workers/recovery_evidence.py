"""Classify stored failures without treating worker/toolchain faults as program crashes."""

import json

from compiler_optimizer.shared.recovery import (
    ProgramCrash,
    RetryableExecution,
    infrastructure_failure,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.baseline import REASONS

CRASH_REASONS = {
    pb.SIGNAL_TERMINATION,
    pb.TIMEOUT,
    pb.MEMORY_LIMIT,
    pb.OUTPUT_LIMIT,
    pb.SANITIZER_FAILURE,
    pb.NONZERO_EXIT,
}


def check_verification(repository, result):
    if result.WhichOneof("outcome") != "failure":
        return
    report = json.loads(repository.artifacts.get(result.correctness_report))
    if report["baseline_invalid"]:
        return
    failure = result.failure
    evidence = {
        "verification_job_id": result.job_id,
        "attempt": result.attempt,
        "correctness_report": reference(result.correctness_report),
    }
    if infrastructure_failure(failure):
        raise RetryableExecution("verification_infrastructure_failure", evidence)
    if failure.reason in CRASH_REASONS and failure.stage in (pb.TEST, pb.MEASURE):
        signature = {
            "reason": pb.FailureReason.Name(failure.reason),
            "test_id": failure.test_id,
            "signal": failure.signal_number,
            "exit_code": failure.exit_code,
            "profile": report["sanitizer"]
            if (
                failure.reason == pb.SANITIZER_FAILURE
                or "sanitizer" in failure.message.split(":", 1)[0]
            )
            else "normal",
        }
        raise ProgramCrash(signature, evidence)


def check_benchmark(repository, report):
    raw = json.loads(repository.artifacts.get(pb.ArtifactRef(**report["raw"])))
    if raw["status"] != "failed":
        return
    error = raw["error"]
    evidence = {
        "benchmark_run_id": report["run_id"],
        "raw": report["raw"],
        "verification_job_id": report["job_id"],
        "attempt": report["attempt"],
    }
    if error["kind"] in ("infrastructure", "measurement"):
        raise RetryableExecution("benchmark_infrastructure_failure", evidence)
    if error["kind"] == "program_failure" and error["role"] == "candidate":
        for test_id, case in raw["cases"].items():
            for pair in [*case["warmups"], *case["pairs"]]:
                sample = pair["samples"].get("candidate", {})
                failure = sample.get("failure")
                if failure is not None:
                    reason = REASONS[failure]
                    code = sample.get("returncode", 0)
                    raise ProgramCrash(
                        {
                            "reason": pb.FailureReason.Name(reason),
                            "test_id": test_id,
                            "signal": -code if code < 0 else 0,
                            "exit_code": code if code > 0 else 0,
                            "profile": "normal",
                        },
                        evidence,
                    )
