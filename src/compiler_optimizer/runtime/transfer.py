"""Bounded package intake and task-scoped, hash-verified artifact downloads."""

import hashlib
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager

import grpc

from compiler_optimizer.evaluation.__main__ import native_target
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.transfer import (
    MAX_TASK_ARTIFACTS,
    MAX_TRANSFER_SECONDS,
    MAX_UPLOAD_FRAMES,
    TRANSFER_CHUNK_BYTES,
    validate_reference,
    validate_task_id,
    validate_upload_header,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage.errors import (
    ArtifactError,
    ConflictError,
    NotFoundError,
    StateError,
)
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import package_from_files, unbundle
from compiler_optimizer.workers.targets import DEFAULT_IMAGES


@contextmanager
def transfer_errors(context):
    try:
        yield
    except (ContractError, ValueError):
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid transfer request or package")
    except PermissionError:
        context.abort(grpc.StatusCode.PERMISSION_DENIED, "artifact is not registered to this task")
    except NotFoundError:
        context.abort(grpc.StatusCode.NOT_FOUND, "task or artifact not found")
    except ArtifactError:
        context.abort(grpc.StatusCode.DATA_LOSS, "artifact integrity check failed")
    except ConflictError:
        context.abort(grpc.StatusCode.ALREADY_EXISTS, "submission identity conflict")
    except StateError:
        context.abort(
            grpc.StatusCode.FAILED_PRECONDITION, "transfer is not valid in this task state"
        )
    except (InfrastructureError, OSError, sqlite3.Error, subprocess.SubprocessError):
        context.abort(grpc.StatusCode.UNAVAILABLE, "transfer storage or compiler unavailable")


class ArtifactTransferService(rpc.ArtifactTransferServiceServicer):
    def __init__(self, repository, coordinator=None, *, compiler_resolver=None):
        self.repository = repository
        self.coordinator = coordinator or CoordinatorService(repository)
        self.compiler_resolver = compiler_resolver or self._compiler

    @staticmethod
    def _compiler(target, language):
        target = target or native_target()
        return DockerRunner(DEFAULT_IMAGES[target], target=target).compiler(language)

    def UploadPackage(self, request_iterator, context):
        with transfer_errors(context):
            remaining = context.time_remaining()
            require(
                remaining is not None and 0 < remaining <= MAX_TRANSFER_SECONDS,
                "upload requires a deadline of at most 120 seconds",
            )
            header = None
            size = 0
            digest = hashlib.sha256()
            with tempfile.SpooledTemporaryFile(max_size=1024**2) as stream:
                for index, frame in enumerate(request_iterator):
                    require(index < MAX_UPLOAD_FRAMES, "too many upload frames")
                    require(context.is_active(), "upload cancelled")
                    if index == 0:
                        require(frame.WhichOneof("frame") == "header", "upload header required")
                        header = frame.header
                        validate_upload_header(header)
                        continue
                    require(frame.WhichOneof("frame") == "data", "unexpected upload frame")
                    require(0 < len(frame.data) <= TRANSFER_CHUNK_BYTES, "invalid chunk size")
                    size += len(frame.data)
                    require(size <= header.package.size_bytes, "upload exceeds declared size")
                    digest.update(frame.data)
                    stream.write(frame.data)
                require(header is not None, "upload header required")
                require(
                    size == header.package.size_bytes
                    and digest.hexdigest() == header.package.sha256,
                    "upload hash/size mismatch",
                )
                stream.seek(0)
                data = stream.read()
            package = package_from_files(unbundle(data))
            require(
                data == package.package_bundle, "package must use the canonical snapshot format"
            )
            try:
                prior = self.repository.get_task(header.meta.task_id)["submission"]
            except NotFoundError:
                prior = None
            if prior is not None:
                if (
                    prior.meta != header.meta
                    or prior.package != header.package
                    or (header.target and prior.compiler.target != header.target)
                ):
                    raise ConflictError("upload changes an existing submission")
                submission = prior
            else:
                compiler = self.compiler_resolver(header.target, package.manifest.language)
                submission = pb.Submission(
                    meta=header.meta,
                    manifest=package.manifest,
                    compiler=compiler,
                    package=self.repository.artifacts.put(data),
                    source=pb.SourceVersion(
                        version=1, bundle=self.repository.artifacts.put(package.source_bundle)
                    ),
                )
            return self.coordinator.SubmitSubmission(submission, context)

    def ListArtifacts(self, request, context):
        with transfer_errors(context):
            validate_task_id(request.task_id)
            rows = self.repository.list_artifacts(request.task_id)
            require(len(rows) <= MAX_TASK_ARTIFACTS, "too many task artifacts")
            result = pb.TaskArtifacts(task_id=request.task_id)
            for row in rows:
                result.artifacts.add(
                    artifact=pb.ArtifactRef(
                        **{key: row[key] for key in ("key", "sha256", "size_bytes")}
                    ),
                    kind=row["kind"],
                    owner_id=row["owner_id"],
                )
            require(
                result.ByteSize() <= MAX_MESSAGE_BYTES, "artifact catalog exceeds message limit"
            )
            return result

    def DownloadArtifact(self, request, context):
        with transfer_errors(context):
            remaining = context.time_remaining()
            require(
                remaining is not None and 0 < remaining <= MAX_TRANSFER_SECONDS,
                "download requires a deadline of at most 120 seconds",
            )
            validate_task_id(request.task_id)
            validate_reference(request.artifact)
            rows = self.repository.list_artifacts(request.task_id)
            if not any(
                all(
                    row[key] == getattr(request.artifact, key)
                    for key in ("key", "sha256", "size_bytes")
                )
                for row in rows
            ):
                raise PermissionError("artifact is outside task scope")
            # Verify the entire bounded object before returning any bytes.
            data = self.repository.artifacts.get(request.artifact)
            for offset in range(0, len(data), TRANSFER_CHUNK_BYTES):
                if not context.is_active():
                    return
                yield pb.ArtifactChunk(
                    offset=offset, data=data[offset : offset + TRANSFER_CHUNK_BYTES]
                )
