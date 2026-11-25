"""Submit, inspect and download optimization work without coordinator filesystem access."""

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict

from compiler_optimizer.runtime.security import is_loopback, validate_token
from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.transfer import (
    MAX_PACKAGE_BYTES,
    MAX_TASK_ARTIFACTS,
    MAX_TRANSFER_SECONDS,
    TRANSFER_CHUNK_BYTES,
    content_reference,
    validate_reference,
    validate_task_id,
    validate_upload_header,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.workers.package import read_package, unbundle


def message_json(message):
    return MessageToDict(message, preserving_proto_field_name=True)


def upload_frames(header, data):
    validate_upload_header(header)
    require(content_reference(data) == header.package, "package changed after request creation")
    yield pb.PackageUploadFrame(header=header)
    for offset in range(0, len(data), TRANSFER_CHUNK_BYTES):
        yield pb.PackageUploadFrame(data=data[offset : offset + TRANSFER_CHUNK_BYTES])


def prepare_upload(package, request_file, target=""):
    """Persist identity before the network call so a lost receipt can be retried safely."""
    ref = content_reference(package.package_bundle)
    validate_reference(ref, MAX_PACKAGE_BYTES)
    path = Path(request_file)
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            data = stream.read(16385)
        require(len(data) <= 16384, "request file is too large")
        header = ParseDict(json.loads(data), pb.PackageUploadHeader())
        validate_upload_header(header)
        require(
            header.package == ref and header.target == target, "request file binds another upload"
        )
        return header
    header = pb.PackageUploadHeader(
        meta=pb.Metadata(
            schema_version=1,
            message_id=str(uuid4()),
            task_id=str(uuid4()),
            created_at_unix_ms=time.time_ns() // 1_000_000,
        ),
        package=ref,
        target=target,
    )
    validate_upload_header(header)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x") as output:
        json.dump(message_json(header), output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    return header


def _write_selected_ir(data, report, refs, artifacts, staging):
    from compiler_optimizer.workers.instrumentation import BUILD_PROFILES
    from compiler_optimizer.workers.llvm_ir import descriptor
    from compiler_optimizer.workers.llvm_policy import PIPELINES, TRIPLES

    ir = ParseDict(json.loads(data), pb.IRPassProposal())
    profiles = [v.build_profile for v in ir.variants]
    require(
        ir.schema_version == 1
        and ir.pipeline_id in PIPELINES
        and ir.target_triple in TRIPLES.values()
        and 2 <= len(profiles) <= 3
        and len(set(profiles)) == len(profiles)
        and "normal" in profiles
        and set(profiles) <= BUILD_PROFILES.keys(),
        "invalid selected LLVM descriptor",
    )
    require(
        data == descriptor(ir) and json.loads(data) == report.get("selected_ir"),
        "selected LLVM descriptor differs from selection report",
    )
    (staging / "selected-ir.json").write_bytes(data)
    directory = staging / "ir"
    directory.mkdir()
    for variant in ir.variants:
        for role, ref in (
            ("baseline", variant.baseline_bitcode),
            ("optimized", variant.optimized_bitcode),
            ("stages", variant.stage_report),
        ):
            validate_reference(ref)
            require(refs.get(ref.sha256) == ref, "unregistered selected LLVM artifact")
            suffix = ".json" if role == "stages" else ".bc"
            shutil.copyfile(
                artifacts / ref.sha256, directory / (variant.build_profile + "-" + role + suffix)
            )


class OptimizerClient:
    def __init__(self, channel, *, token="", timeout=60):
        validate_token(token)
        require(
            math.isfinite(timeout) and 0 < timeout <= MAX_TRANSFER_SECONDS, "invalid RPC timeout"
        )
        self.coordinator = rpc.CoordinatorServiceStub(channel)
        self.transfer = rpc.ArtifactTransferServiceStub(channel)
        self.metadata = (("authorization", "Bearer " + token),) if token else ()
        self.timeout = timeout

    def submit(self, header, data):
        validate_upload_header(header)
        require(content_reference(data) == header.package, "package changed after request creation")
        receipt = self.transfer.UploadPackage(
            upload_frames(header, data), timeout=self.timeout, metadata=self.metadata
        )
        require(
            receipt.meta.task_id == header.meta.task_id
            and receipt.accepted_message_id == header.meta.message_id,
            "receipt does not match uploaded request",
        )
        return receipt

    def status(self, task_id):
        validate_task_id(task_id)
        result = self.coordinator.GetOptimizationResult(
            pb.OptimizationQuery(task_id=task_id), timeout=self.timeout, metadata=self.metadata
        )
        require(result.task_id == task_id, "status belongs to another task")
        return result

    def wait(self, task_id, seconds=600):
        require(math.isfinite(seconds) and seconds > 0, "invalid wait limit")
        deadline = time.monotonic() + seconds
        while True:
            result = self.status(task_id)
            if result.status in {"completed", "failed"}:
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError("task is still running; poll status using the same task ID")
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def fetch(self, task_id, ref, destination):
        validate_task_id(task_id)
        validate_reference(ref)
        size, digest = 0, hashlib.sha256()
        with Path(destination).open("xb") as output:
            for chunk in self.transfer.DownloadArtifact(
                pb.ArtifactRequest(task_id=task_id, artifact=ref),
                timeout=self.timeout,
                metadata=self.metadata,
            ):
                require(
                    chunk.offset == size and 0 < len(chunk.data) <= TRANSFER_CHUNK_BYTES,
                    "invalid download chunk/offset",
                )
                size += len(chunk.data)
                require(size <= ref.size_bytes, "download exceeds declared size")
                digest.update(chunk.data)
                output.write(chunk.data)
        require(
            size == ref.size_bytes and digest.hexdigest() == ref.sha256, "download hash mismatch"
        )

    def download(self, task_id, destination, *, maximum_bytes=512 * 1024**2):
        """Verify a complete terminal-task snapshot before publishing a fresh output directory."""
        require(type(maximum_bytes) is int and maximum_bytes > 0, "invalid download budget")
        output = Path(destination).absolute()
        require(not output.exists() and not output.is_symlink(), "output directory already exists")
        result = self.status(task_id)
        require(
            result.status in {"completed", "failed"}, "wait for a terminal task before download"
        )
        catalog = self.transfer.ListArtifacts(
            pb.OptimizationQuery(task_id=task_id), timeout=self.timeout, metadata=self.metadata
        )
        require(catalog.task_id == task_id, "artifact catalog belongs to another task")
        require(len(catalog.artifacts) <= MAX_TASK_ARTIFACTS, "artifact catalog is too large")
        refs = {}
        for row in catalog.artifacts:
            validate_reference(row.artifact)
            prior = refs.setdefault(row.artifact.sha256, row.artifact)
            require(prior == row.artifact, "conflicting artifact metadata")
        require(
            sum(ref.size_bytes for ref in refs.values()) <= maximum_bytes,
            "download budget exceeded",
        )
        for field in ("report", "selected_source"):
            if result.HasField(field):
                ref = result.report if field == "report" else result.selected_source.bundle
                require(refs.get(ref.sha256) == ref, "result references an unregistered artifact")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".optimizer-download-", dir=output.parent) as temp:
            staging = Path(temp)
            artifacts = staging / "artifacts"
            artifacts.mkdir()
            for digest, ref in refs.items():
                self.fetch(task_id, ref, artifacts / digest)
            (staging / "result.json").write_text(json.dumps(message_json(result), indent=2) + "\n")
            (staging / "artifacts.json").write_text(
                json.dumps(message_json(catalog), indent=2) + "\n"
            )
            if result.HasField("report"):
                shutil.copyfile(artifacts / result.report.sha256, staging / "report.json")
            else:
                (staging / "report.json").write_text(result.report_json + "\n")
            if result.HasField("selected_source"):
                validate_reference(result.selected_source.bundle, MAX_PACKAGE_BYTES)
                data = (artifacts / result.selected_source.bundle.sha256).read_bytes()
                report = json.loads((staging / "report.json").read_bytes())
                if report.get("selected_representation") == "llvm-ir-v1":
                    _write_selected_ir(data, report, refs, artifacts, staging)
                else:
                    sources = unbundle(data)
                    (staging / "selected-source.zip").write_bytes(data)
                    for name, contents in sources.items():
                        source = staging / "source" / name
                        source.parent.mkdir(parents=True, exist_ok=True)
                        with source.open("xb") as stream:
                            stream.write(contents)
            # Reserve the destination exclusively; never replace an existing directory.
            output.mkdir(mode=0o700)
            try:
                for child in staging.iterdir():
                    child.rename(output / child.name)
            except BaseException:
                shutil.rmtree(output)
                raise
        return output


def client_channel(endpoint, *, tls=False, ca=None, token=""):
    host, separator, port = endpoint.rpartition(":")
    require(separator and host and port.isdigit() and 1 <= int(port) <= 65535, "invalid endpoint")
    require(is_loopback(host) or (tls and token), "remote endpoints require TLS and authentication")
    require(not ca or tls, "--ca requires --tls")
    options = (
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    )
    if tls:
        roots = Path(ca).read_bytes() if ca else None
        return grpc.secure_channel(endpoint, grpc.ssl_channel_credentials(roots), options=options)
    return grpc.insecure_channel(endpoint, options=options)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="127.0.0.1:50052")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--ca", help="Trusted CA PEM; otherwise use gRPC default trust roots")
    parser.add_argument(
        "--token-env",
        default="OPTIMIZER_API_TOKEN",
        help="Environment variable containing bearer token",
    )
    parser.add_argument(
        "--timeout", type=float, default=60, help="Per-RPC deadline in seconds, at most 120"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("package")
    submit.add_argument(
        "--request-file", required=True, help="Persist identity for retry after a lost receipt"
    )
    submit.add_argument("--target", default="", help="Server target; omit for native CPU")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--wait-seconds", type=float, default=600)
    status = commands.add_parser("status")
    status.add_argument("task_id")
    download = commands.add_parser("download")
    download.add_argument("task_id")
    download.add_argument("--output", required=True)
    download.add_argument("--maximum-bytes", type=int, default=512 * 1024**2)
    args = parser.parse_args(argv)
    try:
        token = os.environ.get(args.token_env, "")
        with client_channel(args.endpoint, tls=args.tls, ca=args.ca, token=token) as channel:
            client = OptimizerClient(channel, token=token, timeout=args.timeout)
            if args.command == "submit":
                package = read_package(args.package)
                header = prepare_upload(package, args.request_file, args.target)
                print("task_id: " + header.meta.task_id, file=sys.stderr)
                result = client.submit(header, package.package_bundle)
                if args.wait:
                    result = client.wait(header.meta.task_id, args.wait_seconds)
                print(json.dumps(message_json(result), sort_keys=True))
            elif args.command == "status":
                print(json.dumps(message_json(client.status(args.task_id)), sort_keys=True))
            else:
                output = client.download(
                    args.task_id, args.output, maximum_bytes=args.maximum_bytes
                )
                print(json.dumps({"task_id": args.task_id, "output": str(output)}))
    except grpc.RpcError as error:
        print(
            json.dumps({"error": error.code().name, "message": "coordinator request failed"}),
            file=sys.stderr,
        )
        return 1
    except (ContractError, ValueError, OSError, TimeoutError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
