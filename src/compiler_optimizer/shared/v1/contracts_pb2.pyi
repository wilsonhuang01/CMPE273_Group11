from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AgentRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AGENT_ROLE_UNSPECIFIED: _ClassVar[AgentRole]
    PERFORMANCE: _ClassVar[AgentRole]
    MEMORY: _ClassVar[AgentRole]
    CORRECTNESS: _ClassVar[AgentRole]
    ARCHITECTURE: _ClassVar[AgentRole]
    CRITIC: _ClassVar[AgentRole]
    BENCHMARK: _ClassVar[AgentRole]

class WorkerState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKER_STATE_UNSPECIFIED: _ClassVar[WorkerState]
    IDLE: _ClassVar[WorkerState]
    BUSY: _ClassVar[WorkerState]
    QUARANTINED: _ClassVar[WorkerState]

class FailureReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FAILURE_REASON_UNSPECIFIED: _ClassVar[FailureReason]
    COMPILATION_ERROR: _ClassVar[FailureReason]
    OUTPUT_MISMATCH: _ClassVar[FailureReason]
    NONZERO_EXIT: _ClassVar[FailureReason]
    SIGNAL_TERMINATION: _ClassVar[FailureReason]
    TIMEOUT: _ClassVar[FailureReason]
    MEMORY_LIMIT: _ClassVar[FailureReason]
    OUTPUT_LIMIT: _ClassVar[FailureReason]
    INFRASTRUCTURE_ERROR: _ClassVar[FailureReason]
    UNKNOWN_FAILURE: _ClassVar[FailureReason]
    SANITIZER_FAILURE: _ClassVar[FailureReason]

class FailureStage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FAILURE_STAGE_UNSPECIFIED: _ClassVar[FailureStage]
    COMPILE: _ClassVar[FailureStage]
    TEST: _ClassVar[FailureStage]
    MEASURE: _ClassVar[FailureStage]
    INFRASTRUCTURE: _ClassVar[FailureStage]
AGENT_ROLE_UNSPECIFIED: AgentRole
PERFORMANCE: AgentRole
MEMORY: AgentRole
CORRECTNESS: AgentRole
ARCHITECTURE: AgentRole
CRITIC: AgentRole
BENCHMARK: AgentRole
WORKER_STATE_UNSPECIFIED: WorkerState
IDLE: WorkerState
BUSY: WorkerState
QUARANTINED: WorkerState
FAILURE_REASON_UNSPECIFIED: FailureReason
COMPILATION_ERROR: FailureReason
OUTPUT_MISMATCH: FailureReason
NONZERO_EXIT: FailureReason
SIGNAL_TERMINATION: FailureReason
TIMEOUT: FailureReason
MEMORY_LIMIT: FailureReason
OUTPUT_LIMIT: FailureReason
INFRASTRUCTURE_ERROR: FailureReason
UNKNOWN_FAILURE: FailureReason
SANITIZER_FAILURE: FailureReason
FAILURE_STAGE_UNSPECIFIED: FailureStage
COMPILE: FailureStage
TEST: FailureStage
MEASURE: FailureStage
INFRASTRUCTURE: FailureStage

class Metadata(_message.Message):
    __slots__ = ("schema_version", "message_id", "task_id", "created_at_unix_ms")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    message_id: str
    task_id: str
    created_at_unix_ms: int
    def __init__(self, schema_version: _Optional[int] = ..., message_id: _Optional[str] = ..., task_id: _Optional[str] = ..., created_at_unix_ms: _Optional[int] = ...) -> None: ...

class ArtifactRef(_message.Message):
    __slots__ = ("key", "sha256", "size_bytes")
    KEY_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    key: str
    sha256: str
    size_bytes: int
    def __init__(self, key: _Optional[str] = ..., sha256: _Optional[str] = ..., size_bytes: _Optional[int] = ...) -> None: ...

class ResourceLimits(_message.Message):
    __slots__ = ("compile_wall_seconds", "compile_memory_mib", "run_wall_seconds", "run_memory_mib", "output_kib")
    COMPILE_WALL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    COMPILE_MEMORY_MIB_FIELD_NUMBER: _ClassVar[int]
    RUN_WALL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    RUN_MEMORY_MIB_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_KIB_FIELD_NUMBER: _ClassVar[int]
    compile_wall_seconds: int
    compile_memory_mib: int
    run_wall_seconds: int
    run_memory_mib: int
    output_kib: int
    def __init__(self, compile_wall_seconds: _Optional[int] = ..., compile_memory_mib: _Optional[int] = ..., run_wall_seconds: _Optional[int] = ..., run_memory_mib: _Optional[int] = ..., output_kib: _Optional[int] = ...) -> None: ...

class TestCase(_message.Message):
    __slots__ = ("id", "stdin", "expected_stdout", "args")
    ID_FIELD_NUMBER: _ClassVar[int]
    STDIN_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_STDOUT_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    id: str
    stdin: str
    expected_stdout: str
    args: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, id: _Optional[str] = ..., stdin: _Optional[str] = ..., expected_stdout: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ...) -> None: ...

class SubmissionManifest(_message.Message):
    __slots__ = ("schema_version", "name", "language", "sources", "tests", "benchmark_test_ids", "limits")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    TESTS_FIELD_NUMBER: _ClassVar[int]
    BENCHMARK_TEST_IDS_FIELD_NUMBER: _ClassVar[int]
    LIMITS_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    name: str
    language: str
    sources: _containers.RepeatedScalarFieldContainer[str]
    tests: _containers.RepeatedCompositeFieldContainer[TestCase]
    benchmark_test_ids: _containers.RepeatedScalarFieldContainer[str]
    limits: ResourceLimits
    def __init__(self, schema_version: _Optional[int] = ..., name: _Optional[str] = ..., language: _Optional[str] = ..., sources: _Optional[_Iterable[str]] = ..., tests: _Optional[_Iterable[_Union[TestCase, _Mapping]]] = ..., benchmark_test_ids: _Optional[_Iterable[str]] = ..., limits: _Optional[_Union[ResourceLimits, _Mapping]] = ...) -> None: ...

class CompilerSettings(_message.Message):
    __slots__ = ("executable", "version", "image_digest", "language", "target", "flags")
    EXECUTABLE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    IMAGE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    FLAGS_FIELD_NUMBER: _ClassVar[int]
    executable: str
    version: str
    image_digest: str
    language: str
    target: str
    flags: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, executable: _Optional[str] = ..., version: _Optional[str] = ..., image_digest: _Optional[str] = ..., language: _Optional[str] = ..., target: _Optional[str] = ..., flags: _Optional[_Iterable[str]] = ...) -> None: ...

class SourceVersion(_message.Message):
    __slots__ = ("version", "bundle", "parent_source_sha256")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    BUNDLE_FIELD_NUMBER: _ClassVar[int]
    PARENT_SOURCE_SHA256_FIELD_NUMBER: _ClassVar[int]
    version: int
    bundle: ArtifactRef
    parent_source_sha256: str
    def __init__(self, version: _Optional[int] = ..., bundle: _Optional[_Union[ArtifactRef, _Mapping]] = ..., parent_source_sha256: _Optional[str] = ...) -> None: ...

class Submission(_message.Message):
    __slots__ = ("meta", "manifest", "package", "source", "compiler")
    META_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    COMPILER_FIELD_NUMBER: _ClassVar[int]
    meta: Metadata
    manifest: SubmissionManifest
    package: ArtifactRef
    source: SourceVersion
    compiler: CompilerSettings
    def __init__(self, meta: _Optional[_Union[Metadata, _Mapping]] = ..., manifest: _Optional[_Union[SubmissionManifest, _Mapping]] = ..., package: _Optional[_Union[ArtifactRef, _Mapping]] = ..., source: _Optional[_Union[SourceVersion, _Mapping]] = ..., compiler: _Optional[_Union[CompilerSettings, _Mapping]] = ...) -> None: ...

class OptimizationRequest(_message.Message):
    __slots__ = ("meta", "proposal_id", "role", "manifest", "source", "compiler")
    META_FIELD_NUMBER: _ClassVar[int]
    PROPOSAL_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    COMPILER_FIELD_NUMBER: _ClassVar[int]
    meta: Metadata
    proposal_id: str
    role: AgentRole
    manifest: SubmissionManifest
    source: SourceVersion
    compiler: CompilerSettings
    def __init__(self, meta: _Optional[_Union[Metadata, _Mapping]] = ..., proposal_id: _Optional[str] = ..., role: _Optional[_Union[AgentRole, str]] = ..., manifest: _Optional[_Union[SubmissionManifest, _Mapping]] = ..., source: _Optional[_Union[SourceVersion, _Mapping]] = ..., compiler: _Optional[_Union[CompilerSettings, _Mapping]] = ...) -> None: ...

class Proposal(_message.Message):
    __slots__ = ("meta", "proposal_id", "role", "source", "rationale", "transformation", "compiler")
    META_FIELD_NUMBER: _ClassVar[int]
    PROPOSAL_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    RATIONALE_FIELD_NUMBER: _ClassVar[int]
    TRANSFORMATION_FIELD_NUMBER: _ClassVar[int]
    COMPILER_FIELD_NUMBER: _ClassVar[int]
    meta: Metadata
    proposal_id: str
    role: AgentRole
    source: SourceVersion
    rationale: str
    transformation: str
    compiler: CompilerSettings
    def __init__(self, meta: _Optional[_Union[Metadata, _Mapping]] = ..., proposal_id: _Optional[str] = ..., role: _Optional[_Union[AgentRole, str]] = ..., source: _Optional[_Union[SourceVersion, _Mapping]] = ..., rationale: _Optional[str] = ..., transformation: _Optional[str] = ..., compiler: _Optional[_Union[CompilerSettings, _Mapping]] = ...) -> None: ...

class VerificationJob(_message.Message):
    __slots__ = ("meta", "job_id", "baseline", "proposal_id", "source", "manifest", "compiler", "attempt", "deadline_unix_ms", "additional_test_ids", "verification_plan", "verification_inputs")
    META_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    BASELINE_FIELD_NUMBER: _ClassVar[int]
    PROPOSAL_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_FIELD_NUMBER: _ClassVar[int]
    COMPILER_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    ADDITIONAL_TEST_IDS_FIELD_NUMBER: _ClassVar[int]
    VERIFICATION_PLAN_FIELD_NUMBER: _ClassVar[int]
    VERIFICATION_INPUTS_FIELD_NUMBER: _ClassVar[int]
    meta: Metadata
    job_id: str
    baseline: bool
    proposal_id: str
    source: SourceVersion
    manifest: SubmissionManifest
    compiler: CompilerSettings
    attempt: int
    deadline_unix_ms: int
    additional_test_ids: _containers.RepeatedScalarFieldContainer[str]
    verification_plan: ArtifactRef
    verification_inputs: ArtifactRef
    def __init__(self, meta: _Optional[_Union[Metadata, _Mapping]] = ..., job_id: _Optional[str] = ..., baseline: bool = ..., proposal_id: _Optional[str] = ..., source: _Optional[_Union[SourceVersion, _Mapping]] = ..., manifest: _Optional[_Union[SubmissionManifest, _Mapping]] = ..., compiler: _Optional[_Union[CompilerSettings, _Mapping]] = ..., attempt: _Optional[int] = ..., deadline_unix_ms: _Optional[int] = ..., additional_test_ids: _Optional[_Iterable[str]] = ..., verification_plan: _Optional[_Union[ArtifactRef, _Mapping]] = ..., verification_inputs: _Optional[_Union[ArtifactRef, _Mapping]] = ...) -> None: ...

class WorkerHeartbeat(_message.Message):
    __slots__ = ("meta", "worker_id", "incarnation_id", "sequence", "state", "current_job_id", "targets")
    META_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    INCARNATION_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    CURRENT_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TARGETS_FIELD_NUMBER: _ClassVar[int]
    meta: Metadata
    worker_id: str
    incarnation_id: str
    sequence: int
    state: WorkerState
    current_job_id: str
    targets: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, meta: _Optional[_Union[Metadata, _Mapping]] = ..., worker_id: _Optional[str] = ..., incarnation_id: _Optional[str] = ..., sequence: _Optional[int] = ..., state: _Optional[_Union[WorkerState, str]] = ..., current_job_id: _Optional[str] = ..., targets: _Optional[_Iterable[str]] = ...) -> None: ...

class FailureDetails(_message.Message):
    __slots__ = ("reason", "stage", "message", "test_id", "exit_code", "signal_number")
    REASON_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    TEST_ID_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_NUMBER_FIELD_NUMBER: _ClassVar[int]
    reason: FailureReason
    stage: FailureStage
    message: str
    test_id: str
    exit_code: int
    signal_number: int
    def __init__(self, reason: _Optional[_Union[FailureReason, str]] = ..., stage: _Optional[_Union[FailureStage, str]] = ..., message: _Optional[str] = ..., test_id: _Optional[str] = ..., exit_code: _Optional[int] = ..., signal_number: _Optional[int] = ...) -> None: ...

class BenchmarkMeasurement(_message.Message):
    __slots__ = ("test_id", "wall_seconds", "peak_memory_bytes", "warmup_runs")
    TEST_ID_FIELD_NUMBER: _ClassVar[int]
    WALL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    PEAK_MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    WARMUP_RUNS_FIELD_NUMBER: _ClassVar[int]
    test_id: str
    wall_seconds: _containers.RepeatedScalarFieldContainer[float]
    peak_memory_bytes: int
    warmup_runs: int
    def __init__(self, test_id: _Optional[str] = ..., wall_seconds: _Optional[_Iterable[float]] = ..., peak_memory_bytes: _Optional[int] = ..., warmup_runs: _Optional[int] = ...) -> None: ...

class VerificationSuccess(_message.Message):
    __slots__ = ("passed_test_ids", "benchmarks")
    PASSED_TEST_IDS_FIELD_NUMBER: _ClassVar[int]
    BENCHMARKS_FIELD_NUMBER: _ClassVar[int]
    passed_test_ids: _containers.RepeatedScalarFieldContainer[str]
    benchmarks: _containers.RepeatedCompositeFieldContainer[BenchmarkMeasurement]
    def __init__(self, passed_test_ids: _Optional[_Iterable[str]] = ..., benchmarks: _Optional[_Iterable[_Union[BenchmarkMeasurement, _Mapping]]] = ...) -> None: ...

class VerificationResult(_message.Message):
    __slots__ = ("meta", "job_id", "worker_id", "attempt", "baseline", "proposal_id", "source", "compiler", "hardware", "success", "failure", "log", "correctness_report")
    META_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    BASELINE_FIELD_NUMBER: _ClassVar[int]
    PROPOSAL_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    COMPILER_FIELD_NUMBER: _ClassVar[int]
    HARDWARE_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    FAILURE_FIELD_NUMBER: _ClassVar[int]
    LOG_FIELD_NUMBER: _ClassVar[int]
    CORRECTNESS_REPORT_FIELD_NUMBER: _ClassVar[int]
    meta: Metadata
    job_id: str
    worker_id: str
    attempt: int
    baseline: bool
    proposal_id: str
    source: SourceVersion
    compiler: CompilerSettings
    hardware: str
    success: VerificationSuccess
    failure: FailureDetails
    log: ArtifactRef
    correctness_report: ArtifactRef
    def __init__(self, meta: _Optional[_Union[Metadata, _Mapping]] = ..., job_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., attempt: _Optional[int] = ..., baseline: bool = ..., proposal_id: _Optional[str] = ..., source: _Optional[_Union[SourceVersion, _Mapping]] = ..., compiler: _Optional[_Union[CompilerSettings, _Mapping]] = ..., hardware: _Optional[str] = ..., success: _Optional[_Union[VerificationSuccess, _Mapping]] = ..., failure: _Optional[_Union[FailureDetails, _Mapping]] = ..., log: _Optional[_Union[ArtifactRef, _Mapping]] = ..., correctness_report: _Optional[_Union[ArtifactRef, _Mapping]] = ...) -> None: ...

class Receipt(_message.Message):
    __slots__ = ("meta", "accepted_message_id")
    META_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    meta: Metadata
    accepted_message_id: str
    def __init__(self, meta: _Optional[_Union[Metadata, _Mapping]] = ..., accepted_message_id: _Optional[str] = ...) -> None: ...

class OptimizationQuery(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

class OptimizationResult(_message.Message):
    __slots__ = ("task_id", "status", "stage", "report_json", "report", "selected_source")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    REPORT_JSON_FIELD_NUMBER: _ClassVar[int]
    REPORT_FIELD_NUMBER: _ClassVar[int]
    SELECTED_SOURCE_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    status: str
    stage: str
    report_json: str
    report: ArtifactRef
    selected_source: SourceVersion
    def __init__(self, task_id: _Optional[str] = ..., status: _Optional[str] = ..., stage: _Optional[str] = ..., report_json: _Optional[str] = ..., report: _Optional[_Union[ArtifactRef, _Mapping]] = ..., selected_source: _Optional[_Union[SourceVersion, _Mapping]] = ...) -> None: ...
