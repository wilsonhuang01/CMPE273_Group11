"""Service configuration, readiness, topology, and coordinator RPC integration."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import uuid4

import grpc
import pytest

from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.dependencies import DEAD_EXCHANGE, declare_topology
from compiler_optimizer.runtime.health import Readiness, health_server
from compiler_optimizer.shared.contracts import BASE_FLAGS, validate_message
from compiler_optimizer.shared.manifest import ContractError, parse_manifest
from compiler_optimizer.shared.messaging import EXCHANGE, ROUTES
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import (
    ArtifactError,
    ConflictError,
    NotFoundError,
    StateError,
)


def test_settings_keep_credentials_out_of_repr():
    settings = Settings.from_env(
        "coordinator",
        {
            "RABBITMQ_PASSWORD": "rabbit-secret",
            "REDIS_PASSWORD": "redis-secret",
            "STATE_DIRECTORY": "/state",
            "PROBE_INTERVAL_SECONDS": "2",
        },
    )
    assert settings.state_directory == Path("/state")
    assert settings.probe_interval_seconds == 2
    assert "rabbit-secret" not in repr(settings)
    assert "redis-secret" not in repr(settings)


@pytest.mark.parametrize(
    "env",
    [
        {"HEALTH_PORT": "0"},
        {"GRPC_PORT": "65536"},
        {"REDIS_PORT": "bad"},
        {"PROBE_INTERVAL_SECONDS": "61"},
        {"RABBITMQ_PASSWORD": ""},
        {"COORDINATOR_HEALTH_URL": "file:///etc/passwd"},
        {"COORDINATOR_HEALTH_URL": "http://user:secret@host/readyz"},
    ],
)
def test_settings_reject_invalid_environment(env):
    with pytest.raises(ValueError):
        Settings.from_env("worker", env)


def test_readiness_detects_dependency_failure_recovery_and_staleness():
    clock = [0.0]
    dependency = Mock()
    state = Readiness("worker", {"rabbitmq": dependency}, clock=lambda: clock[0])
    assert not state.snapshot()["infrastructure_ready"]
    state.probe_once()
    assert state.snapshot()["infrastructure_ready"]
    assert not state.snapshot()["execution_enabled"]
    dependency.side_effect = RuntimeError("secret connection value")
    state.probe_once()
    assert not state.snapshot()["infrastructure_ready"]
    assert "secret" not in json.dumps(state.snapshot())
    dependency.side_effect = None
    state.probe_once()
    assert state.snapshot()["infrastructure_ready"]
    clock[0] = 16.0
    assert not state.snapshot()["infrastructure_ready"]


def test_topology_covers_wire_routes_and_has_durable_deadletter_sink():
    channel = Mock()
    declare_topology(channel)
    main_bindings = [
        call.kwargs
        for call in channel.queue_bind.call_args_list
        if call.kwargs["exchange"] == EXCHANGE
    ]
    assert {binding["routing_key"] for binding in main_bindings} == set(ROUTES)
    assert any(
        call.kwargs["exchange"] == DEAD_EXCHANGE and call.kwargs["routing_key"] == "#"
        for call in channel.queue_bind.call_args_list
    )
    assert all(call.kwargs["durable"] for call in channel.queue_declare.call_args_list)
    for call in channel.queue_declare.call_args_list:
        if call.kwargs["queue"] in {binding["queue"] for binding in main_bindings}:
            assert call.kwargs["arguments"]["x-dead-letter-exchange"] == DEAD_EXCHANGE


def test_http_liveness_is_separate_from_readiness():
    state = Readiness("scheduler", {"rabbitmq": Mock(side_effect=RuntimeError)})
    server = health_server(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/healthz", timeout=2) as response:
            assert response.status == 200
            assert json.load(response)["service"] == "scheduler"
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/readyz", timeout=2)
        assert error.value.code == 503
        state.probes["rabbitmq"] = lambda: None
        state.probe_once()
        with urlopen(base + "/readyz", timeout=2) as response:
            assert json.load(response)["infrastructure_ready"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class RpcAbort(Exception):
    def __init__(self, code):
        self.code = code


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ContractError("bad"), grpc.StatusCode.INVALID_ARGUMENT),
        (ConflictError("conflict"), grpc.StatusCode.ALREADY_EXISTS),
        (NotFoundError("missing"), grpc.StatusCode.NOT_FOUND),
        (ArtifactError("corrupt"), grpc.StatusCode.FAILED_PRECONDITION),
        (StateError("completed"), grpc.StatusCode.FAILED_PRECONDITION),
        (OSError("disk"), grpc.StatusCode.UNAVAILABLE),
        (RuntimeError("unexpected"), grpc.StatusCode.INTERNAL),
    ],
)
def test_intake_maps_storage_failures_to_rpc_status(failure, code):
    repo = Mock()
    repo.save_submission.side_effect = failure
    context = Mock()
    context.abort.side_effect = lambda status, _: (_ for _ in ()).throw(RpcAbort(status))
    with pytest.raises(RpcAbort) as error:
        CoordinatorService(repo).SubmitSubmission(pb.Submission(), context)
    assert error.value.code == code


def test_grpc_intake_commits_before_receipt_and_deduplicates(tmp_path):
    repo = Repository(tmp_path)
    manifest_path = Path(__file__).resolve().parents[1] / "examples/sum-array/submission.json"
    request = pb.Submission(
        meta=pb.Metadata(
            schema_version=1,
            message_id=str(uuid4()),
            task_id=str(uuid4()),
            created_at_unix_ms=time.time_ns() // 1_000_000,
        ),
        manifest=parse_manifest(manifest_path.read_bytes()),
        package=repo.artifacts.put(b"test-package"),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(b"test-source")),
        compiler=pb.CompilerSettings(
            executable="clang",
            version="18.1.8",
            image_digest="sha256:" + "a" * 64,
            language="c17",
            target="linux-x86_64",
            flags=["-std=c17", *BASE_FLAGS],
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        server = grpc.server(executor)
        rpc.add_CoordinatorServiceServicer_to_server(CoordinatorService(repo), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                client = rpc.CoordinatorServiceStub(channel)
                receipt = client.SubmitSubmission(request, timeout=3)
                validate_message(receipt)
                assert receipt.accepted_message_id == request.meta.message_id
                assert Repository(tmp_path).get_task(request.meta.task_id)["submission"] == request
                assert (
                    client.SubmitSubmission(request, timeout=3).accepted_message_id
                    == request.meta.message_id
                )
                assert len(repo.list_tasks()) == 1
                request.meta.schema_version = 2
                with pytest.raises(grpc.RpcError) as error:
                    client.SubmitSubmission(request, timeout=3)
                assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        finally:
            server.stop(0).wait(timeout=2)
