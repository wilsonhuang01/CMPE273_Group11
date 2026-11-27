"""Immutable content-addressed blobs on a trusted local filesystem."""

import hashlib
import io
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO

from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.errors import ArtifactError, NotFoundError

CHUNK = 64 * 1024


def artifact_key(digest: str) -> str:
    """Store-relative key shared by every artifact backend."""
    return f"sha256/{digest[:2]}/{digest}"


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ArtifactStore:
    """Publish complete blobs atomically; verify digest and size on every read.

    The service owns root and its ancestors. Sandboxes must never mount it writable.
    No archive extraction or user-chosen storage keys are supported.
    """

    def __init__(self, root: str | Path, *, max_bytes: int = 128 * 1024**2):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        root = Path(root).absolute()
        if root.is_symlink():
            raise ArtifactError("artifact root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve()
        self.max_bytes = max_bytes

    def _directory(self, path: Path) -> None:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ArtifactError("artifact directory is not a real directory") from None
        else:
            _sync_directory(path.parent)

    _key = staticmethod(artifact_key)

    def check_health(self) -> None:
        if not stat.S_ISDIR(self.root.lstat().st_mode):
            raise ArtifactError("artifact root is not a real directory")

    def _path(self, ref: pb.ArtifactRef) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", ref.sha256) or ref.key != self._key(ref.sha256):
            raise ArtifactError("artifact key must match its SHA-256 digest")
        if ref.size_bytes > self.max_bytes:
            raise ArtifactError("artifact exceeds configured size limit")
        for parent in (self.root / "sha256", self.root / "sha256" / ref.sha256[:2]):
            try:
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise ArtifactError("artifact directory is not a real directory")
            except FileNotFoundError:
                raise NotFoundError(ref.key) from None
        return self.root / ref.key

    def get(self, ref: pb.ArtifactRef) -> bytes:
        path = self._path(ref)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise NotFoundError(ref.key) from None
        except OSError as error:
            raise ArtifactError(f"cannot safely open artifact: {ref.key}") from error
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ArtifactError("artifact must be a regular file")
            if os.fstat(source.fileno()).st_size != ref.size_bytes:
                raise ArtifactError("artifact size mismatch")
            data = source.read(self.max_bytes + 1)
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ArtifactError("artifact digest or size mismatch")
        return data

    def put(self, data: bytes) -> pb.ArtifactRef:
        if not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")
        return self.put_stream(io.BytesIO(data))

    def put_file(self, path: str | Path) -> pb.ArtifactRef:
        with Path(path).open("rb") as source:
            return self.put_stream(source)

    def put_stream(self, source: BinaryIO) -> pb.ArtifactRef:
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        temporary = Path(temporary)
        try:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ArtifactError("artifact exceeds configured size limit")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            sha256 = digest.hexdigest()
            ref = pb.ArtifactRef(key=self._key(sha256), sha256=sha256, size_bytes=size)
            self._directory(self.root / "sha256")
            self._directory(self.root / "sha256" / sha256[:2])
            destination = self.root / ref.key
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                self.get(ref)  # Never overwrite a corrupt or conflicting existing object.
            _sync_directory(destination.parent)
            return ref
        finally:
            temporary.unlink(missing_ok=True)
