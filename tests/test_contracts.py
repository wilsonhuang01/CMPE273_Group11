"""Contract boundary checks independent of a compiler, broker, or running service."""

import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from compiler_optimizer.shared.contracts import (
    BASE_FLAGS,
    MAX_MESSAGE_BYTES,
    validate_message,
    validate_proposal_for_request,
    validate_result_for_job,
)
from compiler_optimizer.shared.manifest import ContractError, parse_manifest, validate_manifest
from compiler_optimizer.shared.messaging import (
    decode_event,
    decode_message,
    encode_event,
    encode_message,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/sum-array/submission.json"
TASK_ID = str(uuid4())
PROPOSAL_ID = str(uuid4())
JOB_ID = str(uuid4())
WORKER_ID = str(uuid4())


def metadata(task_id=TASK_ID):
    return pb.Metadata(
        schema_version=1,
        message_id=str(uuid4()),
        task_id=task_id,
        created_at_unix_ms=1_800_000_000_000,
    )


def source(version=1):
    return pb.SourceVersion(
        version=version,
        bundle=pb.ArtifactRef(
            key=f"sources/v{version}.tar", sha256=str(version) * 64, size_bytes=2048
        ),
        parent_source_sha256="" if version == 1 else "1" * 64,
    )


def compiler(language="c17"):
    return pb.CompilerSettings(
        executable="clang" if language == "c17" else "clang++",
        version="18.1.8",
        image_digest="sha256:" + "a" * 64,
        language=language,
        target="linux-x86_64",
        flags=["-std=c17" if language == "c17" else "-std=c++17", *BASE_FLAGS],
    )


def manifest_data():
    return json.loads(EXAMPLE.read_text())


def job():
    return pb.VerificationJob(
        meta=metadata(),
        job_id=JOB_ID,
        proposal_id=PROPOSAL_ID,
        source=source(2),
        manifest=parse_manifest(EXAMPLE.read_bytes()),
        compiler=compiler(),
        attempt=1,
        deadline_unix_ms=1_800_000_060_000,
    )


def result():
    return pb.VerificationResult(
        meta=metadata(),
        job_id=JOB_ID,
        worker_id=WORKER_ID,
        attempt=1,
        proposal_id=PROPOSAL_ID,
        source=source(2),
        compiler=compiler(),
        hardware="Linux x86-64; example CPU; worker image and compiler recorded separately",
        success=pb.VerificationSuccess(
            passed_test_ids=["empty", "mixed"],
            benchmarks=[
                pb.BenchmarkMeasurement(
                    test_id="mixed",
                    wall_seconds=[0.01, 0.011],
                    peak_memory_bytes=4096,
                    warmup_runs=1,
                )
            ],
        ),
    )


def all_messages():
    return [
        pb.Submission(
            meta=metadata(),
            manifest=parse_manifest(EXAMPLE.read_bytes()),
            package=pb.ArtifactRef(key="packages/example.tar", sha256="a" * 64, size_bytes=4096),
            source=source(),
            compiler=compiler(),
        ),
        pb.OptimizationRequest(
            meta=metadata(),
            proposal_id=PROPOSAL_ID,
            role=pb.PERFORMANCE,
            manifest=parse_manifest(EXAMPLE.read_bytes()),
            source=source(),
            compiler=compiler(),
        ),
        pb.Proposal(
            meta=metadata(),
            proposal_id=PROPOSAL_ID,
            role=pb.PERFORMANCE,
            source=source(2),
            rationale="Reduce repeated array loads.",
            transformation="loop-unroll",
            compiler=compiler(),
        ),
        job(),
        pb.WorkerHeartbeat(
            meta=metadata(""),
            worker_id=WORKER_ID,
            incarnation_id=str(uuid4()),
            sequence=1,
            state=pb.IDLE,
            targets=["linux-x86_64"],
        ),
        result(),
        pb.Receipt(meta=metadata(), accepted_message_id=str(uuid4())),
    ]


@pytest.mark.parametrize("message", all_messages(), ids=lambda msg: msg.DESCRIPTOR.name)
def test_round_trip_all_top_level_messages(message):
    assert decode_message(type(message), encode_message(message)) == message


@pytest.mark.parametrize("index", [2, 3, 4, 5])
def test_queue_routes_and_amqp_properties(index):
    message = all_messages()[index]
    event = encode_event(message)
    assert decode_event(event.routing_key, event.body) == message
    assert event.message_id == message.meta.message_id
    assert event.correlation_id == message.meta.task_id
    assert event.message_type == message.DESCRIPTOR.full_name
    assert event.delivery_mode == 2
    assert event.content_type == "application/x-protobuf"


def set_field(obj, path, value):
    for component in path[:-1]:
        obj = obj[component] if isinstance(obj, dict | list) else getattr(obj, component)
    if isinstance(obj, dict | list):
        obj[path[-1]] = value
    else:
        setattr(obj, path[-1], value)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("schema_version",), 2),
        (("name",), "bad\n"),
        (("language",), "cuda"),
        (("sources",), ["main.cpp"]),
        (("sources",), ["main.c", "main.c"]),
        (("sources",), ["/main.c"]),
        (("sources",), ["../main.c"]),
        (("sources",), ["src/../main.c"]),
        (("sources",), ["src//main.c"]),
        (("sources",), ["C:\\main.c"]),
        (("tests", 0, "stdin"), "./input"),
        (("tests", 0, "expected_stdout"), "bad\0path"),
        (("tests", 1, "id"), "empty"),
        (("tests", 0, "args"), ["é" * 513]),
        (("tests", 0, "args"), ["bad\0arg"]),
        (("tests", 0, "args"), ["x"] * 33),
        (("tests", 0, "extra"), "unknown"),
        (("tests",), []),
        (("benchmark_test_ids",), ["missing"]),
        (("benchmark_test_ids",), ["mixed", "mixed"]),
        (("limits", "run_wall_seconds"), 11),
        (("limits", "run_memory_mib"), 0),
        (("limits", "output_kib"), True),
        (("extra",), "unknown"),
    ],
)
def test_reject_invalid_manifest(path, value):
    data = manifest_data()
    set_field(data, path, value)
    with pytest.raises(ContractError):
        parse_manifest(data)


def test_manifest_required_fields_and_json_errors():
    data = manifest_data()
    del data["tests"][0]["args"]
    with pytest.raises(ContractError):
        parse_manifest(data)
    for invalid in [b'{"schema_version":1,"schema_version":1}', b"{", b"\xff", b"[]", "\ud800"]:
        with pytest.raises(ContractError):
            parse_manifest(invalid)


def test_manifest_round_trip_and_cpp17():
    validate_manifest(parse_manifest(EXAMPLE.read_bytes()))
    data = manifest_data()
    data.update(language="cpp17", sources=["main.cpp"])
    validate_manifest(parse_manifest(data))
    settings = compiler("cpp17")
    request = all_messages()[1]
    request.compiler.CopyFrom(settings)
    request.manifest.CopyFrom(parse_manifest(data))
    validate_message(request)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("meta", "schema_version"), 0),
        (("meta", "schema_version"), 2),
        (("meta", "message_id"), "not-a-uuid"),
        (("meta", "task_id"), ""),
        (("meta", "created_at_unix_ms"), 0),
        (("job_id",), ""),
        (("attempt",), 0),
        (("deadline_unix_ms",), 1),
        (("source", "version"), 0),
        (("source", "bundle", "sha256"), "Z" * 64),
        (("source", "bundle", "key"), "../source.tar"),
        (("source", "bundle", "size_bytes"), 0),
        (("source", "parent_source_sha256"), "2" * 64),
        (("compiler", "target"), "cuda"),
        (("compiler", "version"), "19.0.0"),
        (("compiler", "executable"), "/bin/sh"),
        (("compiler", "image_digest"), "latest"),
        (("manifest", "language"), "cpp17"),
        (("baseline",), False),
    ],
)
def test_reject_invalid_verification_job(path, value):
    message = job()
    set_field(message, path, value)
    with pytest.raises(ContractError):
        validate_message(message)


def test_baseline_subject_and_compiler_flags():
    message = job()
    message.baseline = True
    message.source.CopyFrom(source())
    validate_message(message)
    message.compiler.flags.append("-ffast-math")
    with pytest.raises(ContractError, match="flags"):
        validate_message(message)
    message = job()
    message.ClearField("proposal_id")
    with pytest.raises(ContractError, match="baseline or proposal_id"):
        validate_message(message)


def test_worker_state_and_heartbeat_sequence():
    message = all_messages()[4]
    message.state = pb.BUSY
    with pytest.raises(ContractError):
        validate_message(message)
    message.meta.task_id = TASK_ID
    message.current_job_id = JOB_ID
    validate_message(message)
    message.state = pb.QUARANTINED
    with pytest.raises(ContractError):
        validate_message(message)
    message.ClearField("current_job_id")
    message.meta.task_id = ""
    validate_message(message)
    message.sequence = 0
    with pytest.raises(ContractError, match="sequence"):
        validate_message(message)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 0.0])
def test_reject_invalid_measurements(value):
    message = result()
    message.success.benchmarks[0].wall_seconds[0] = value
    with pytest.raises(ContractError, match="timings"):
        validate_message(message)


def test_outcome_required_and_benchmark_must_pass():
    message = result()
    message.ClearField("success")
    with pytest.raises(ContractError, match="outcome"):
        validate_message(message)
    message = result()
    message.success.benchmarks[0].test_id = "unverified"
    with pytest.raises(ContractError, match="must have passed"):
        validate_message(message)


def test_failure_details_require_evidence_and_oneof_clears_success():
    message = result()
    message.failure.CopyFrom(
        pb.FailureDetails(
            reason=pb.TIMEOUT, stage=pb.TEST, test_id="mixed", message="Wall deadline exceeded"
        )
    )
    assert message.WhichOneof("outcome") == "failure"
    validate_result_for_job(message, job())
    message.failure.reason = pb.SIGNAL_TERMINATION
    with pytest.raises(ContractError, match="signal number"):
        validate_message(message)
    message.failure.signal_number = 11
    validate_message(message)
    message.failure.exit_code = 1
    with pytest.raises(ContractError, match="both"):
        validate_message(message)
    message.failure.ClearField("exit_code")
    message.failure.stage = pb.COMPILE
    with pytest.raises(ContractError, match="no test ID"):
        validate_message(message)


@pytest.mark.parametrize(
    "change", ["attempt", "task", "source", "compiler", "coverage", "benchmark"]
)
def test_reject_stale_or_mismatched_results(change):
    message = result()
    if change == "attempt":
        message.attempt = 2
    elif change == "task":
        message.meta.task_id = str(uuid4())
    elif change == "source":
        message.source.bundle.sha256 = "3" * 64
    elif change == "compiler":
        message.compiler.version = "18.1.7"
    elif change == "coverage":
        message.success.passed_test_ids.remove("empty")
    else:
        message.success.benchmarks.add(test_id="empty", wall_seconds=[0.01], peak_memory_bytes=1)
    with pytest.raises(ContractError):
        validate_result_for_job(message, job())


def test_valid_result_matches_job():
    validate_result_for_job(result(), job())


def test_invalid_wire_payloads_and_unrouted_messages():
    for body in [b"\xff", b"", b"x" * (MAX_MESSAGE_BYTES + 1)]:
        with pytest.raises(ContractError):
            decode_event("verification.job.v1", body)
    with pytest.raises(ContractError, match="routing key"):
        decode_event("unknown", b"")
    with pytest.raises(ContractError, match="no RabbitMQ route"):
        encode_event(all_messages()[0])
    with pytest.raises(ContractError, match="unsupported top-level"):
        encode_message(pb.SourceVersion())


def test_grpc_stubs_and_rpc_types_are_generated():
    assert contracts_pb2_grpc.CoordinatorServiceStub
    assert contracts_pb2_grpc.AgentServiceStub
    assert contracts_pb2_grpc.WorkerHealthServiceStub
    rpc = pb.DESCRIPTOR.services_by_name["AgentService"].methods_by_name["GenerateProposal"]
    assert rpc.input_type.full_name == "compiler_optimizer.v1.OptimizationRequest"
    assert rpc.output_type.full_name == "compiler_optimizer.v1.Proposal"


def test_manifest_input_is_not_mutated():
    original = manifest_data()
    data = copy.deepcopy(original)
    parse_manifest(data)
    assert data == original


def test_proposal_matches_request_and_rejects_wrong_parent():
    messages = all_messages()
    request, proposal = messages[1:3]
    validate_proposal_for_request(proposal, request)
    proposal.source.parent_source_sha256 = "a" * 64
    with pytest.raises(ContractError, match="parent"):
        validate_proposal_for_request(proposal, request)


@pytest.mark.parametrize("change", ["task", "id", "role", "version", "compiler"])
def test_proposal_must_match_assignment(change):
    request, proposal = all_messages()[1:3]
    if change == "task":
        proposal.meta.task_id = str(uuid4())
    elif change == "id":
        proposal.proposal_id = str(uuid4())
    elif change == "role":
        proposal.role = pb.MEMORY
    elif change == "version":
        proposal.source.version = 3
    else:
        proposal.compiler.version = "18.1.7"
    with pytest.raises(ContractError):
        validate_proposal_for_request(proposal, request)
