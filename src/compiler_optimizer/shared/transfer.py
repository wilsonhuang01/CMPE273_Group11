"""Wire limits and validation shared by the artifact service and filesystem-free client."""

import hashlib
import re
from uuid import UUID

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.targets import TARGETS

TRANSFER_CHUNK_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 12 * 1024**2  # 10 MiB content plus bounded ZIP metadata.
MAX_ARTIFACT_BYTES = 128 * 1024**2
MAX_UPLOAD_FRAMES = 512
MAX_TRANSFER_SECONDS = 120
MAX_TASK_ARTIFACTS = 2048


def validate_task_id(value):
    try:
        parsed = UUID(value)
        require(str(parsed) == value and parsed.int != 0, "invalid task ID")
    except ValueError as error:
        raise ValueError("invalid task ID") from error


def validate_reference(ref, maximum=MAX_ARTIFACT_BYTES):
    require(re.fullmatch(r"[0-9a-f]{64}", ref.sha256) is not None, "invalid artifact digest")
    require(ref.key == f"sha256/{ref.sha256[:2]}/{ref.sha256}", "artifact key/digest mismatch")
    require(ref.size_bytes <= maximum, "artifact exceeds transfer limit")


def content_reference(data):
    digest = hashlib.sha256(data).hexdigest()
    return pb.ArtifactRef(key=f"sha256/{digest[:2]}/{digest}", sha256=digest, size_bytes=len(data))


def validate_upload_header(header):
    require(header.meta.schema_version == 1, "unsupported upload version")
    validate_task_id(header.meta.task_id)
    validate_task_id(header.meta.message_id)
    require(0 < header.meta.created_at_unix_ms <= 253402300799999, "invalid upload timestamp")
    validate_reference(header.package, MAX_PACKAGE_BYTES)
    require(header.package.size_bytes > 0, "empty package")
    require(header.target == "" or header.target in TARGETS, "unsupported upload target")
