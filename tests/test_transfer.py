"""Separate-filesystem upload/download, malicious archives, immutable refs and RPC security."""

import hashlib
import io
import json
import stat
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import grpc
import pytest
from test_dispatch import dispatcher
from test_selection import MeasuredRunner

from compiler_optimizer.client import (
    OptimizerClient,
    client_channel,
    main,
    prepare_upload,
    upload_frames,
)
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.security import BearerAuth
from compiler_optimizer.runtime.transfer import ArtifactTransferService
from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.transfer import MAX_PACKAGE_BYTES, content_reference
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.package import (
    MAX_CONTENT_BYTES,
    bundle,
    package_from_files,
    read_package,
)

TOKEN = "unit-test-token-" + "a" * 32
EXAMPLE = Path(__file__).resolve().parents[1] / "examples/sum-array"


@contextmanager
def running_service(tmp_path, *, tls=None, token=TOKEN):
    repo = Repository(tmp_path / "server")
    package = read_package(EXAMPLE)
    runner = MeasuredRunner(package)
    resolver = Mock(side_effect=lambda *_: runner.compiler(package.manifest.language))
    with ThreadPoolExecutor(max_workers=4) as pool:
        server = grpc.server(
            pool,
            interceptors=(BearerAuth(token),),
            options=[
                ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ],
        )
        service = CoordinatorService(repo)
        rpc.add_CoordinatorServiceServicer_to_server(service, server)
        rpc.add_ArtifactTransferServiceServicer_to_server(
            ArtifactTransferService(repo, service, compiler_resolver=resolver), server
        )
        if tls:
            cert, key = tls
            port = server.add_secure_port(
                "127.0.0.1:0",
                grpc.ssl_server_credentials(
                    (
                        (
                            key.read_bytes(),
                            cert.read_bytes(),
                        ),
                    )
                ),
            )
            channel = grpc.secure_channel(
                f"localhost:{port}", grpc.ssl_channel_credentials(cert.read_bytes())
            )
        else:
            port = server.add_insecure_port("127.0.0.1:0")
            channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        server.start()
        try:
            with channel:
                yield repo, runner, OptimizerClient(channel, token=token), channel, resolver, port
        finally:
            server.stop(0).wait(timeout=5)


def test_client_uploads_and_downloads_complete_evidence_without_shared_state(tmp_path):
    client_root = tmp_path / "client"
    client_root.mkdir()
    package = read_package(EXAMPLE)
    header = prepare_upload(package, client_root / "request.json")
    with running_service(tmp_path) as (repo, runner, client, _, resolver, _):
        receipt = client.submit(header, package.package_bundle)
        assert receipt.meta.task_id == header.meta.task_id
        assert client.status(header.meta.task_id).stage == "queued"
        assert (
            client.submit(header, package.package_bundle).accepted_message_id
            == receipt.accepted_message_id
        )
        assert resolver.call_count == 1  # A retry reuses the accepted compiler and frozen workflow.
        assert len(repo.list_tasks()) == 1
        dispatcher(repo, runner).run_once()
        output = client.download(header.meta.task_id, client_root / "download")
        result = json.loads((output / "result.json").read_text())
        assert result["status"] == "completed"
        assert (output / "source/main.c").is_file()
        assert (output / "selected-source.zip").is_file()
        assert json.loads((output / "report.json").read_text())["status"] == "selected"
        catalog = json.loads((output / "artifacts.json").read_text())
        for row in catalog["artifacts"]:
            ref = row["artifact"]
            data = (output / "artifacts" / ref["sha256"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == ref["sha256"]
        assert not list(client_root.rglob("*.sqlite3"))
        with pytest.raises(ContractError, match="already exists"):
            client.download(header.meta.task_id, output)


def test_upload_larger_than_rpc_message_limit_uses_chunks(tmp_path):
    package = read_package(EXAMPLE)
    package = package_from_files({**package.files, "extra.bin": b"x" * (2 * MAX_MESSAGE_BYTES)})
    header = prepare_upload(package, tmp_path / "request.json")
    with running_service(tmp_path) as (repo, _, client, _, _, _):
        client.submit(header, package.package_bundle)
        assert repo.get_task(header.meta.task_id)["submission"].package == header.package


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "truncated",
        "header",
        "repeated-header",
        "empty-frame",
        "large-chunk",
        "declared-size",
        "target",
        "traversal",
        "symlink",
        "content-limit",
    ],
)
def test_invalid_uploads_never_create_tasks_or_publish_artifacts(tmp_path, case):
    package = read_package(EXAMPLE)
    header = prepare_upload(package, tmp_path / "request.json")
    data = package.package_bundle
    if case == "traversal":
        data = bundle({"../outside": b"escape"})
    elif case == "content-limit":
        data = bundle({"oversized": b"x" * (MAX_CONTENT_BYTES + 1)})
    elif case == "symlink":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            info = zipfile.ZipInfo("main.c")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../outside")
        data = output.getvalue()
    header.package.CopyFrom(content_reference(data))
    frames = list(upload_frames(header, data))
    if case == "hash":
        frames[-1].data = b"!" + frames[-1].data[1:]
    elif case == "truncated":
        frames.pop()
    elif case == "header":
        frames = frames[1:]
    elif case == "repeated-header":
        frames.insert(1, pb.PackageUploadFrame(header=header))
    elif case == "empty-frame":
        frames.insert(1, pb.PackageUploadFrame())
    elif case == "large-chunk":
        frames[1].data = b"x" * (65536 + 1)
    elif case == "declared-size":
        frames[0].header.package.size_bytes = MAX_PACKAGE_BYTES + 1
    elif case == "target":
        frames[0].header.target = "not-a-target"
    with running_service(tmp_path) as (repo, _, _, channel, resolver, _):
        with pytest.raises(grpc.RpcError) as error:
            rpc.ArtifactTransferServiceStub(channel).UploadPackage(
                iter(frames), timeout=10, metadata=(("authorization", "Bearer " + TOKEN),)
            )
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert repo.list_tasks() == [] and not resolver.called
        assert not list(repo.artifacts.root.rglob(".upload-*"))
        assert not (repo.artifacts.root / "sha256").exists()
    assert not (tmp_path / "outside").exists()


def test_artifact_download_rejects_unregistered_hashes_and_wrong_metadata(tmp_path):
    package = read_package(EXAMPLE)
    header = prepare_upload(package, tmp_path / "request.json")
    with running_service(tmp_path) as (repo, _, client, channel, _, _):
        client.submit(header, package.package_bundle)
        unlinked = repo.artifacts.put(b"another task's private content")
        query = pb.ArtifactRequest(task_id=header.meta.task_id, artifact=unlinked)
        transfer = rpc.ArtifactTransferServiceStub(channel)
        with pytest.raises(grpc.RpcError) as error:
            list(transfer.DownloadArtifact(query, timeout=5, metadata=client.metadata))
        assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
        query.artifact.CopyFrom(header.package)
        query.artifact.size_bytes += 1
        with pytest.raises(grpc.RpcError) as error:
            list(transfer.DownloadArtifact(query, timeout=5, metadata=client.metadata))
        assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_corrupt_server_artifact_is_rejected_before_any_bytes_are_returned(tmp_path):
    package = read_package(EXAMPLE)
    header = prepare_upload(package, tmp_path / "request.json")
    with running_service(tmp_path) as (repo, _, client, _, _, _):
        client.submit(header, package.package_bundle)
        (repo.artifacts.root / header.package.key).write_bytes(b"corrupt")
        with pytest.raises(grpc.RpcError) as error:
            client.fetch(header.meta.task_id, header.package, tmp_path / "artifact")
        assert error.value.code() == grpc.StatusCode.DATA_LOSS
        assert (tmp_path / "artifact").stat().st_size == 0


def test_unauthenticated_clients_cannot_upload_list_download_or_read_status(tmp_path):
    header = prepare_upload(read_package(EXAMPLE), tmp_path / "request.json")
    with running_service(tmp_path) as (repo, _, _, channel, resolver, _):
        transfer = rpc.ArtifactTransferServiceStub(channel)
        calls = [
            lambda: transfer.UploadPackage(iter([pb.PackageUploadFrame(header=header)]), timeout=5),
            lambda: transfer.ListArtifacts(
                pb.OptimizationQuery(task_id=header.meta.task_id), timeout=5
            ),
            lambda: list(
                transfer.DownloadArtifact(
                    pb.ArtifactRequest(task_id=header.meta.task_id, artifact=header.package),
                    timeout=5,
                )
            ),
            lambda: rpc.CoordinatorServiceStub(channel).GetOptimizationResult(
                pb.OptimizationQuery(task_id=header.meta.task_id), timeout=5
            ),
        ]
        for call in calls:
            with pytest.raises(grpc.RpcError) as error:
                call()
            assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
        assert not repo.list_tasks() and not resolver.called


@pytest.fixture
def certificate(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    config = tmp_path / "openssl.cnf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n"
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-config",
            str(config),
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def test_tls_authenticated_upload_and_cli_status(tmp_path, certificate, monkeypatch, capsys):
    header = prepare_upload(read_package(EXAMPLE), tmp_path / "request.json")
    with running_service(tmp_path, tls=certificate) as (_, _, client, _, _, port):
        client.submit(header, read_package(EXAMPLE).package_bundle)
        monkeypatch.setenv("OPTIMIZER_API_TOKEN", TOKEN)
        assert (
            main(
                [
                    "--endpoint",
                    f"localhost:{port}",
                    "--tls",
                    "--ca",
                    str(certificate[0]),
                    "status",
                    header.meta.task_id,
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["task_id"] == header.meta.task_id
        with client_channel(
            f"localhost:{port}", tls=True, ca=certificate[0], token=TOKEN
        ) as channel:
            with pytest.raises(grpc.RpcError) as error:
                OptimizerClient(channel, token="wrong-token-" + "b" * 32).status(
                    header.meta.task_id
                )
            assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.parametrize(
    "environment",
    [
        {"GRPC_HOST": "0.0.0.0"},
        {"GRPC_HOST": "0.0.0.0", "OPTIMIZER_API_TOKEN": TOKEN},
        {"GRPC_HOST": "0.0.0.0", "RPC_TLS_CERT": "cert", "RPC_TLS_KEY": "key"},
        {"RPC_TLS_CERT": "cert"},
        {"OPTIMIZER_API_TOKEN": "too-short"},
        {"OPTIMIZER_API_TOKEN": "secret\n" * 10},
    ],
)
def test_unsafe_remote_server_configuration_is_rejected(environment):
    with pytest.raises(ValueError):
        Settings.from_env("coordinator", environment)


def test_safe_remote_configuration_and_client_requirements():
    settings = Settings.from_env(
        "coordinator",
        {
            "GRPC_HOST": "0.0.0.0",
            "RPC_TLS_CERT": "cert",
            "RPC_TLS_KEY": "key",
            "OPTIMIZER_API_TOKEN": TOKEN,
        },
    )
    assert TOKEN not in repr(settings)
    with pytest.raises(ContractError, match="TLS"):
        client_channel("example.com:50052", token=TOKEN)


def test_client_detects_wrong_bytes_even_if_server_claims_success(tmp_path):
    client = OptimizerClient(Mock())
    ref = content_reference(b"correct")
    client.transfer.DownloadArtifact = Mock(return_value=iter([pb.ArtifactChunk(data=b"corrupt")]))
    with pytest.raises(ContractError, match="hash"):
        client.fetch(str(uuid4()), ref, tmp_path / "artifact")


def test_request_identity_survives_retry_and_rejects_changed_package(tmp_path):
    package = read_package(EXAMPLE)
    path = tmp_path / "request.json"
    original = prepare_upload(package, path)
    assert prepare_upload(package, path) == original
    changed = package_from_files({**package.files, "extra.h": b"#define EXTRA 1\n"})
    with pytest.raises(ContractError, match="another upload"):
        prepare_upload(changed, path)


def test_local_symlink_submission_is_rejected(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "main.c").symlink_to(EXAMPLE / "main.c")
    with pytest.raises(ContractError, match="symlinks"):
        read_package(directory)


def test_upload_requires_bounded_deadline_and_frame_count(tmp_path):
    package = read_package(EXAMPLE)
    header = prepare_upload(package, tmp_path / "request.json")
    with running_service(tmp_path) as (repo, _, _, channel, _, _):
        service = rpc.ArtifactTransferServiceStub(channel)
        metadata = (("authorization", "Bearer " + TOKEN),)
        with pytest.raises(grpc.RpcError) as error:
            service.UploadPackage(
                iter([pb.PackageUploadFrame(header=header)]), timeout=121, metadata=metadata
            )
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        frames = [pb.PackageUploadFrame(header=header)] + [pb.PackageUploadFrame(data=b"x")] * 513
        with pytest.raises(grpc.RpcError) as error:
            service.UploadPackage(iter(frames), timeout=5, metadata=metadata)
        assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert not repo.list_tasks()


def test_bad_ca_does_not_connect_to_tls_service(tmp_path, certificate):
    with running_service(tmp_path, tls=certificate) as (_, _, _, _, _, port):
        with grpc.secure_channel(f"localhost:{port}", grpc.ssl_channel_credentials()) as channel:
            with pytest.raises(grpc.RpcError) as error:
                OptimizerClient(channel, token=TOKEN, timeout=1).status(str(uuid4()))
            assert error.value.code() in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            }


def test_client_rejects_malicious_selected_source_before_publishing_output(tmp_path):
    task = str(uuid4())
    data = bundle({"../escaped.c": b"unsafe path"})
    ref = content_reference(data)
    client = OptimizerClient(Mock())
    client.status = Mock(
        return_value=pb.OptimizationResult(
            task_id=task,
            status="completed",
            report_json="{}",
            selected_source=pb.SourceVersion(version=1, bundle=ref),
        )
    )
    client.transfer.ListArtifacts = Mock(
        return_value=pb.TaskArtifacts(
            task_id=task,
            artifacts=[pb.TaskArtifact(artifact=ref, kind="source")],
        )
    )
    client.transfer.DownloadArtifact = Mock(return_value=iter([pb.ArtifactChunk(data=data)]))
    with pytest.raises(ContractError, match="traversal"):
        client.download(task, tmp_path / "results")
    assert not (tmp_path / "results").exists()
    assert not (tmp_path / "escaped.c").exists()
    assert not list(tmp_path.glob(".optimizer-download-*"))


def test_client_download_budget_prevents_transfer(tmp_path):
    package = read_package(EXAMPLE)
    header = prepare_upload(package, tmp_path / "request.json")
    with running_service(tmp_path) as (repo, runner, client, _, _, _):
        client.submit(header, package.package_bundle)
        dispatcher(repo, runner).run_once()
        client.fetch = Mock()
        with pytest.raises(ContractError, match="budget"):
            client.download(header.meta.task_id, tmp_path / "results", maximum_bytes=1)
        assert not client.fetch.called and not (tmp_path / "results").exists()
