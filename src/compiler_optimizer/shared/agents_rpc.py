"""Version-one agent RPC identities, bounds and provenance validation."""

import hashlib
import json
from uuid import UUID

from compiler_optimizer.shared.agent_roles import ROLES
from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES, validate_message
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb

AGENT_NAMES = tuple(spec.name for spec in ROLES.values())
AGENT_PORTS = {name: 50061 + index for index, name in enumerate(AGENT_NAMES)}
MAX_AGENT_ARTIFACT_BYTES = 64 * 1024**2
MAX_AGENT_FRAMES = 2048


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def wire(message):
    require(message.ByteSize() <= MAX_MESSAGE_BYTES, "agent message exceeds 1 MiB")
    return message.SerializeToString(deterministic=True)


def payload(response):
    if isinstance(response, pb.Proposal):
        copy = pb.Proposal()
        copy.CopyFrom(response)
        copy.ClearField("agent_provenance")
        return wire(copy)
    return response.record_json.encode()


def validate_provenance(provenance, request, response, role):
    require(
        provenance.schema_version == 1 and provenance.role == role,
        "agent provenance role/version mismatch",
    )
    require(
        str(UUID(provenance.service_id)) == provenance.service_id, "invalid agent service identity"
    )
    require(
        provenance.backend in ("deterministic-v1", "replay-v1")
        and 0 < len(provenance.release.encode()) <= 128,
        "invalid agent backend provenance",
    )
    require(
        provenance.request_sha256 == sha(wire(request))
        and provenance.response_sha256 == sha(payload(response)),
        "agent provenance hash mismatch",
    )


def validate_assessment(request, role):
    wire(request)
    require(
        request.role == role and role in (pb.CORRECTNESS, pb.CRITIC, pb.BENCHMARK),
        "incorrect agent assessment role",
    )
    for message in (request.submission, request.proposal, request.job, request.verification):
        validate_message(message)
    require(
        request.meta == request.proposal.meta
        and all(
            m.meta.task_id == request.meta.task_id
            for m in (request.submission, request.job, request.verification)
        ),
        "agent task identity mismatch",
    )
    require(
        request.proposal.source.parent_source_sha256 == request.submission.source.bundle.sha256
        and request.proposal.compiler == request.submission.compiler,
        "agent proposal differs from submission",
    )
    require(
        request.job.proposal_id == request.proposal.proposal_id
        and request.verification.proposal_id == request.proposal.proposal_id,
        "agent assessment proposal mismatch",
    )
    require(
        request.job.manifest == request.submission.manifest
        and request.job.compiler == request.submission.compiler
        and request.job.source == request.proposal.source,
        "agent assessment differs from frozen submission",
    )
    require(
        request.HasField("benchmark_plan") == (role == pb.BENCHMARK),
        "unexpected/missing benchmark policy",
    )


def parse_record(data):
    require(len(data.encode()) <= MAX_MESSAGE_BYTES, "agent record exceeds 1 MiB")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate agent record key")
            result[key] = value
        return result

    return json.loads(
        data,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")),
    )
