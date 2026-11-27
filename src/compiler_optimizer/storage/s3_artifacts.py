"""Immutable content-addressed blobs in Amazon S3 (action R).

Same contract as the local ``ArtifactStore``: the store chooses every key from the
SHA-256 digest, never overwrites an existing object and verifies size and digest on
every read. Objects are written with a conditional ``If-None-Match: *`` put and an
S3-verified SHA-256 checksum, so a concurrent or retried writer of identical bytes
deduplicates and conflicting contents are detected rather than replaced.
"""

import base64
import hashlib
import io
import re
import tempfile
from typing import BinaryIO

from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.artifacts import CHUNK, artifact_key
from compiler_optimizer.storage.errors import ArtifactError, NotFoundError, UnavailableError

# Bodies up to this size are hashed and uploaded from memory; larger ones spool to disk.
SPOOL_BYTES = 8 * 1024**2
_MISSING = {"NoSuchKey", "404", "NotFound"}
_EXISTS = {"PreconditionFailed", "412"}
_RACING = {"ConditionalRequestConflict", "409"}
_PUT_ATTEMPTS = 3


def _error_code(error: Exception) -> str | None:
    """Return the S3 error code of a botocore ``ClientError`` without importing botocore."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        return None if code is None else str(code)
    return None


def s3_client(*, region: str | None = None, endpoint_url: str | None = None):
    """Create a bounded boto3 S3 client using the default AWS credential chain."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:  # pragma: no cover - depends on the optional extra.
        raise RuntimeError(
            "the S3 artifact backend requires boto3: pip install -e '.[aws]'"
        ) from error
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        config=Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 4, "mode": "standard"},
            # Only send/validate checksums when an operation asks for them; puts always do.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


class S3ArtifactStore:
    """Publish complete blobs to ``s3://bucket/prefix/sha256/<xx>/<digest>``.

    ``ArtifactRef.key`` stays the store-relative ``sha256/<xx>/<digest>`` used by the
    local backend, so references are portable between backends and the bucket prefix
    is deployment configuration rather than part of the persisted record.
    """

    def __init__(
        self,
        client,
        bucket: str,
        *,
        prefix: str = "",
        kms_key_id: str | None = None,
        max_bytes: int = 128 * 1024**2,
    ):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", bucket or ""):
            raise ValueError("invalid S3 bucket name")
        prefix = prefix.strip("/")
        if prefix and (
            not re.fullmatch(r"[A-Za-z0-9!_.*'()\-/]{1,512}", prefix)
            or any(part in ("", ".", "..") for part in prefix.split("/"))
        ):
            raise ValueError("invalid S3 artifact prefix")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix
        self.kms_key_id = kms_key_id or None
        self.max_bytes = max_bytes

    def _object_key(self, ref: pb.ArtifactRef) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", ref.sha256) or ref.key != artifact_key(ref.sha256):
            raise ArtifactError("artifact key must match its SHA-256 digest")
        if ref.size_bytes > self.max_bytes:
            raise ArtifactError("artifact exceeds configured size limit")
        return f"{self.prefix}/{ref.key}" if self.prefix else ref.key

    def check_health(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as error:
            raise UnavailableError(f"artifact bucket unavailable: {self.bucket}") from error

    def get(self, ref: pb.ArtifactRef) -> bytes:
        key = self._object_key(ref)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            if _error_code(error) in _MISSING:
                raise NotFoundError(ref.key) from None
            raise UnavailableError(f"cannot read artifact: {ref.key}") from error
        body = response["Body"]
        try:
            if response.get("ContentLength") != ref.size_bytes:
                raise ArtifactError("artifact size mismatch")
            digest = hashlib.sha256()
            data = bytearray()
            while chunk := self._read(body, ref):
                data += chunk
                if len(data) > ref.size_bytes:
                    raise ArtifactError("artifact size mismatch")
                digest.update(chunk)
        finally:
            body.close()
        if len(data) != ref.size_bytes or digest.hexdigest() != ref.sha256:
            raise ArtifactError("artifact digest or size mismatch")
        return bytes(data)

    @staticmethod
    def _read(body, ref) -> bytes:
        try:
            return body.read(CHUNK)
        except Exception as error:  # Interrupted download: transient, never an integrity verdict.
            raise UnavailableError(f"cannot read artifact: {ref.key}") from error

    def put(self, data: bytes) -> pb.ArtifactRef:
        if not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")
        return self.put_stream(io.BytesIO(data))

    def put_file(self, path) -> pb.ArtifactRef:
        with open(path, "rb") as source:
            return self.put_stream(source)

    def put_stream(self, source: BinaryIO) -> pb.ArtifactRef:
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as spool:
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(CHUNK):
                size += len(chunk)
                if size > self.max_bytes:
                    raise ArtifactError("artifact exceeds configured size limit")
                digest.update(chunk)
                spool.write(chunk)
            sha256 = digest.hexdigest()
            ref = pb.ArtifactRef(key=artifact_key(sha256), sha256=sha256, size_bytes=size)
            arguments = {
                "Bucket": self.bucket,
                "Key": self._object_key(ref),
                "ContentLength": size,
                "ContentType": "application/octet-stream",
                "ChecksumSHA256": base64.b64encode(digest.digest()).decode(),
                "IfNoneMatch": "*",
                "Metadata": {"sha256": sha256},
            }
            if self.kms_key_id:
                arguments |= {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms_key_id}
            for attempt in range(_PUT_ATTEMPTS):
                spool.seek(0)
                try:
                    self.client.put_object(Body=spool, **arguments)
                    return ref
                except Exception as error:
                    code = _error_code(error)
                    if code in _EXISTS:
                        self.get(ref)  # Never accept a corrupt or conflicting existing object.
                        return ref
                    if code in _RACING and attempt + 1 < _PUT_ATTEMPTS:
                        continue  # Another writer's conditional put is in flight; retry.
                    raise UnavailableError(f"cannot store artifact: {ref.key}") from error
        raise UnavailableError(f"cannot store artifact: {ref.key}")  # pragma: no cover
