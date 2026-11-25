"""Semantic validation for v1 protobuf messages at service/queue boundaries."""

import math
import re
from uuid import UUID

from google.protobuf.message import Message

from compiler_optimizer.shared.manifest import (
    ContractError,
    require,
    validate_manifest,
    validate_path,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.targets import TARGETS, X86, compiler_flags

TARGET = X86.name
MAX_MESSAGE_BYTES = 1024 * 1024
BASE_FLAGS = list(X86.flags)
SHA256 = re.compile(r"[0-9a-f]{64}")
TEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
MODEL_BACKEND = re.compile(r"model-v1:[a-z0-9-]{1,32}:[A-Za-z0-9._:/-]{1,200}")


def valid_backend(name):
    """Deterministic rules, replayed patches, or a model identity (provider and model ID)."""
    return name in ("deterministic-v1", "replay-v1") or MODEL_BACKEND.fullmatch(name) is not None


def _uuid(value: str, name: str) -> None:
    try:
        parsed = UUID(value)
        require(
            str(parsed) == value and parsed.int != 0, f"{name} must be a canonical nonzero UUID"
        )
    except ValueError as error:
        raise ContractError(f"{name} must be a canonical nonzero UUID") from error


def _timestamp(value: int, name: str) -> None:
    require(0 < value <= 253402300799999, f"{name} must be Unix milliseconds in years 1970–9999")


def _hash(value: str) -> None:
    require(SHA256.fullmatch(value) is not None, "expected a lowercase SHA-256 hex digest")


def _metadata(meta: pb.Metadata, *, task_optional: bool = False) -> None:
    require(meta.schema_version == 1, "unsupported schema_version")
    _uuid(meta.message_id, "message_id")
    if meta.task_id or not task_optional:
        _uuid(meta.task_id, "task_id")
    _timestamp(meta.created_at_unix_ms, "created_at_unix_ms")


def _artifact(artifact: pb.ArtifactRef, *, nonempty: bool = False) -> None:
    validate_path(artifact.key)
    _hash(artifact.sha256)
    if nonempty:
        require(artifact.size_bytes > 0, "source/package artifact cannot be empty")


def _source(source: pb.SourceVersion) -> None:
    require(source.version > 0, "source version must be positive")
    _artifact(source.bundle, nonempty=True)
    if source.version == 1:
        require(not source.parent_source_sha256, "baseline source cannot have a parent")
    else:
        _hash(source.parent_source_sha256)
        require(source.parent_source_sha256 != source.bundle.sha256, "proposal source must change")


def _compiler(compiler: pb.CompilerSettings) -> None:
    require(compiler.language in ("c17", "cpp17"), "unsupported compiler language")
    executable = "clang" if compiler.language == "c17" else "clang++"
    require(compiler.executable == executable, "compiler executable does not match language")
    require(
        bool(re.fullmatch(r"18\.\d+\.\d+(?:[ -][^\r\n]{1,200})?", compiler.version)),
        "compiler version must identify a Clang 18 release",
    )
    require(compiler.image_digest.startswith("sha256:"), "image_digest must be content-addressed")
    _hash(compiler.image_digest.removeprefix("sha256:"))
    require(compiler.target in TARGETS, "unsupported target architecture")
    require(compiler.execution_profile in ("", "parallel-2"), "unknown execution profile")
    require(
        not compiler.execution_profile or compiler.target != "linux-riscv64",
        "parallel execution requires native CPU target",
    )
    require(
        list(compiler.flags)
        == compiler_flags(compiler.target, compiler.language, compiler.execution_profile),
        "compiler flags must match the controlled v1 build profile",
    )


def _manifest_compiler(message) -> None:
    validate_manifest(message.manifest)
    require(message.manifest.language == message.compiler.language, "manifest/compiler mismatch")


def _subject(message) -> None:
    subject = message.WhichOneof("subject")
    require(subject is not None, "baseline or proposal_id is required")
    if subject == "baseline":
        require(
            message.baseline and message.source.version == 1, "baseline requires source version 1"
        )
    else:
        _uuid(message.proposal_id, "proposal_id")
        require(message.source.version > 1, "proposal requires source version greater than 1")


def _role(role: int) -> None:
    require(
        role
        in (pb.PERFORMANCE, pb.MEMORY, pb.CORRECTNESS, pb.ARCHITECTURE, pb.CRITIC, pb.BENCHMARK),
        "unknown or unspecified agent role",
    )


def _unique_ids(ids, name: str, *, nonempty: bool = True, maximum: int = 100) -> None:
    require((bool(ids) or not nonempty) and len(ids) <= maximum, f"invalid {name} count")
    require(len(set(ids)) == len(ids), f"duplicate {name}")
    require(all(TEST_ID.fullmatch(item) for item in ids), f"invalid {name}")


def _success(success: pb.VerificationSuccess) -> None:
    _unique_ids(success.passed_test_ids, "passed test IDs", maximum=132)
    _unique_ids([metric.test_id for metric in success.benchmarks], "benchmark test IDs")
    for metric in success.benchmarks:
        require(metric.test_id in success.passed_test_ids, "benchmark test must have passed")
        require(0 < len(metric.wall_seconds) <= 1000, "expected 1–1000 timing samples")
        require(
            all(math.isfinite(value) and value > 0 for value in metric.wall_seconds),
            "timings must be finite positive seconds",
        )
        require(metric.peak_memory_bytes > 0, "peak memory measurement is required")


def _failure(failure: pb.FailureDetails) -> None:
    require(
        failure.reason in pb.FailureReason.values() and failure.reason != 0,
        "unknown or unspecified failure reason",
    )
    require(
        failure.stage in (pb.COMPILE, pb.TEST, pb.MEASURE, pb.INFRASTRUCTURE),
        "unknown or unspecified failure stage",
    )
    require(0 < len(failure.message.encode()) <= 4096, "failure diagnostic must be 1–4096 bytes")
    if failure.stage in (pb.TEST, pb.MEASURE):
        require(
            failure.HasField("test_id") and bool(TEST_ID.fullmatch(failure.test_id)),
            "test/measurement failure needs a valid test ID",
        )
    else:
        require(not failure.HasField("test_id"), "compile/infrastructure failures have no test ID")
    if failure.reason == pb.COMPILATION_ERROR:
        require(failure.stage == pb.COMPILE, "compilation error must occur during compilation")
    if failure.reason == pb.OUTPUT_MISMATCH:
        require(failure.stage in (pb.TEST, pb.MEASURE), "output mismatch requires an executed test")
    if failure.reason == pb.SANITIZER_FAILURE:
        require(
            failure.stage in (pb.TEST, pb.MEASURE), "sanitizer failure requires an executed test"
        )
    if failure.reason == pb.INFRASTRUCTURE_ERROR:
        require(
            failure.stage == pb.INFRASTRUCTURE, "infrastructure error needs infrastructure stage"
        )
    if failure.stage == pb.INFRASTRUCTURE:
        require(
            failure.reason in (pb.INFRASTRUCTURE_ERROR, pb.UNKNOWN_FAILURE),
            "infrastructure stage cannot claim a program failure",
        )
    require(
        not (failure.HasField("exit_code") and failure.HasField("signal_number")),
        "failure cannot report both an exit code and a signal",
    )
    if failure.HasField("exit_code"):
        require(1 <= failure.exit_code <= 255, "failure exit code must be 1–255")
    if failure.HasField("signal_number"):
        require(0 < failure.signal_number <= 64, "invalid Linux signal number")
    if failure.reason == pb.NONZERO_EXIT:
        require(failure.HasField("exit_code"), "nonzero exit requires an exit code")
    if failure.reason == pb.SIGNAL_TERMINATION:
        require(failure.HasField("signal_number"), "signal termination requires a signal number")


def validate_message(message: Message) -> None:
    """Validate a top-level v1 contract before sending or after receiving it.

    Does not fetch artifacts, check filesystem contents, or consult task history.
    """
    supported = (
        pb.Submission,
        pb.OptimizationRequest,
        pb.Proposal,
        pb.VerificationJob,
        pb.WorkerHeartbeat,
        pb.VerificationResult,
        pb.Receipt,
    )
    require(isinstance(message, supported), "unsupported top-level message type")
    require(message.ByteSize() <= MAX_MESSAGE_BYTES, "message exceeds 1 MiB")
    _metadata(message.meta, task_optional=isinstance(message, (pb.WorkerHeartbeat, pb.Receipt)))

    if isinstance(message, pb.Receipt):
        _uuid(message.accepted_message_id, "accepted_message_id")
        return
    if isinstance(message, pb.WorkerHeartbeat):
        _uuid(message.worker_id, "worker_id")
        _uuid(message.incarnation_id, "incarnation_id")
        require(message.sequence > 0, "heartbeat sequence must be positive")
        require(message.state in (pb.IDLE, pb.BUSY, pb.QUARANTINED), "invalid worker state")
        require(
            bool(message.targets)
            and len(set(message.targets)) == len(message.targets)
            and set(message.targets) <= set(TARGETS),
            "unsupported worker targets",
        )
        if message.state == pb.BUSY:
            _uuid(message.current_job_id, "current_job_id")
            _uuid(message.meta.task_id, "task_id")
        else:
            require(
                not message.HasField("current_job_id") and not message.meta.task_id,
                "idle/quarantined worker cannot report an active task/job",
            )
        return

    _source(message.source)
    _compiler(message.compiler)
    if isinstance(message, (pb.Submission, pb.OptimizationRequest, pb.VerificationJob)):
        _manifest_compiler(message)
    if isinstance(message, pb.Submission):
        _artifact(message.package, nonempty=True)
        require(message.source.version == 1, "submission must contain a baseline source")
    elif isinstance(message, (pb.Proposal, pb.OptimizationRequest)):
        _uuid(message.proposal_id, "proposal_id")
        _role(message.role)
        if isinstance(message, pb.OptimizationRequest) and message.HasField(
            "revision_context_json"
        ):
            from compiler_optimizer.shared.debate import revision_context

            revision_context(message.revision_context_json)
        if isinstance(message, pb.Proposal):
            if message.HasField("ir"):
                from compiler_optimizer.workers.llvm_ir import validate_ir

                validate_ir(message.ir, message.compiler, message.source.parent_source_sha256)
                require(
                    not message.HasField("structured_patch"),
                    "LLVM recipes cannot carry source patches",
                )
            require(message.source.version > 1, "proposal must derive from a parent source")
            require(0 < len(message.rationale.encode()) <= 16384, "rationale must be 1–16384 bytes")
            require(bool(TEST_ID.fullmatch(message.transformation)), "invalid transformation ID")
            require(
                message.HasField("structured_patch") == message.HasField("agent_provenance"),
                "agent patch and provenance must be supplied together",
            )
            if message.HasField("structured_patch"):
                _artifact(message.structured_patch, nonempty=True)
                evidence = message.agent_provenance
                _uuid(evidence.service_id, "agent service_id")
                require(
                    evidence.schema_version == 1
                    and evidence.role == message.role
                    and valid_backend(evidence.backend)
                    and 0 < len(evidence.release.encode()) <= 128
                    and SHA256.fullmatch(evidence.request_sha256)
                    and SHA256.fullmatch(evidence.response_sha256),
                    "invalid agent provenance",
                )
        elif message.HasField("replay_patch"):
            _artifact(message.replay_patch, nonempty=True)
    elif isinstance(message, (pb.VerificationJob, pb.VerificationResult)):
        _uuid(message.job_id, "job_id")
        _subject(message)
        require(message.attempt > 0, "attempt must start at 1")
        if isinstance(message, pb.VerificationJob):
            if message.HasField("ir"):
                from compiler_optimizer.workers.llvm_ir import validate_ir

                require(
                    message.WhichOneof("subject") == "proposal_id",
                    "baseline cannot carry an LLVM proposal",
                )
                validate_ir(message.ir, message.compiler, message.source.parent_source_sha256)
            _unique_ids(
                message.additional_test_ids, "additional test IDs", nonempty=False, maximum=32
            )
            require(
                not set(message.additional_test_ids) & {case.id for case in message.manifest.tests},
                "additional test IDs collide with supplied tests",
            )
            require(
                message.HasField("verification_plan") == message.HasField("verification_inputs"),
                "verification plan and inputs must be provided together",
            )
            require(
                not message.additional_test_ids or message.HasField("verification_plan"),
                "additional tests require a persisted verification plan",
            )
            if message.HasField("verification_plan"):
                _artifact(message.verification_plan, nonempty=True)
                _artifact(message.verification_inputs, nonempty=True)
            _timestamp(message.deadline_unix_ms, "deadline_unix_ms")
            require(
                message.deadline_unix_ms > message.meta.created_at_unix_ms,
                "deadline must follow job creation",
            )
        else:
            _uuid(message.worker_id, "worker_id")
            require(0 < len(message.hardware.encode()) <= 4096, "hardware description is required")
            if message.HasField("log"):
                _artifact(message.log)
            if message.HasField("correctness_report"):
                _artifact(message.correctness_report, nonempty=True)
            outcome = message.WhichOneof("outcome")
            require(outcome is not None, "success or failure outcome is required")
            if outcome == "success":
                _success(message.success)
            else:
                _failure(message.failure)


def validate_proposal_for_request(proposal: pb.Proposal, request: pb.OptimizationRequest) -> None:
    """Bind an agent response to its assigned task, parent source, and build profile."""
    validate_message(request)
    validate_message(proposal)
    require(
        proposal.meta.task_id == request.meta.task_id
        and proposal.proposal_id == request.proposal_id,
        "proposal task/ID does not match request",
    )
    require(proposal.role == request.role, "proposal role does not match request")
    require(proposal.compiler == request.compiler, "proposal compiler does not match request")
    require(
        proposal.source.version == request.source.version + 1
        and proposal.source.parent_source_sha256 == request.source.bundle.sha256,
        "proposal does not derive from requested parent source",
    )
    require(
        proposal.meta.created_at_unix_ms >= request.meta.created_at_unix_ms,
        "proposal predates request",
    )


def validate_result_for_job(result: pb.VerificationResult, job: pb.VerificationJob) -> None:
    """Reject stale or mismatched results before applying them to a stored job."""
    validate_message(job)
    validate_message(result)
    require(
        result.meta.task_id == job.meta.task_id and result.job_id == job.job_id,
        "result task/job IDs do not match",
    )
    require(result.attempt == job.attempt, "stale or unexpected result attempt")
    require(
        result.WhichOneof("subject") == job.WhichOneof("subject")
        and result.proposal_id == job.proposal_id,
        "result subject does not match",
    )
    require(
        result.source == job.source and result.compiler == job.compiler,
        "result source/compiler differs from job",
    )
    require(
        result.meta.created_at_unix_ms >= job.meta.created_at_unix_ms,
        "result predates job creation",
    )
    test_ids = {test.id for test in job.manifest.tests} | set(job.additional_test_ids)
    if job.HasField("verification_plan"):
        require(
            result.HasField("correctness_report"), "verification job needs a correctness report"
        )
        require(result.HasField("log"), "verification job needs invocation logs")
    if result.WhichOneof("outcome") == "success":
        require(set(result.success.passed_test_ids) == test_ids, "result must cover every test")
        require(
            {metric.test_id for metric in result.success.benchmarks}
            == set(job.manifest.benchmark_test_ids),
            "benchmark coverage differs from job",
        )
    elif result.failure.HasField("test_id"):
        require(
            result.failure.test_id in test_ids
            or (job.HasField("verification_plan") and result.failure.test_id == "coverage-fuzz"),
            "failure references an unknown test",
        )
