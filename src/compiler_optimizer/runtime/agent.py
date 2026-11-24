"""Native/Docker role processes without coordinator state or sandbox privileges."""

import os
import shutil
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import grpc

from compiler_optimizer.agents.model import ModelConfig
from compiler_optimizer.agents.rpc_service import AgentService
from compiler_optimizer.runtime.health import Readiness, health_server
from compiler_optimizer.runtime.security import BearerAuth, validate_rpc_security
from compiler_optimizer.shared.agent_roles import ROLES
from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc


def development_tls(settings):
    """Generate a persistent per-role development certificate inside its private volume."""
    if not settings.agent_dev_tls:
        return
    directory = settings.agent_tls_directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert, key = settings.rpc_tls_cert, settings.rpc_tls_key
    if cert.exists() and key.exists():
        if settings.agent_ca_directory:
            settings.agent_ca_directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cert, settings.agent_ca_directory / cert.name)
        return
    # argv is fixed operator configuration. Source/model output can never reach this helper.
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "365",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN=agent-{settings.role}",
            "-addext",
            f"subjectAltName=DNS:agent-{settings.role},DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    if settings.agent_ca_directory:
        settings.agent_ca_directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cert, settings.agent_ca_directory / cert.name)


def run_agent(settings):
    validate_rpc_security(settings)
    development_tls(settings)
    role = next(role for role, spec in ROLES.items() if spec.name == settings.role)
    service = AgentService(
        settings.state_directory,
        role,
        release=settings.agent_release,
        model=ModelConfig.from_env(os.environ),
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-rpc")
    server = grpc.server(
        executor,
        maximum_concurrent_rpcs=4,
        interceptors=(BearerAuth(settings.api_token),),
        options=[
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ],
    )
    rpc.add_AgentServiceServicer_to_server(service, server)
    address = f"{settings.grpc_host}:{settings.grpc_port}"
    if settings.rpc_tls_cert:
        server.add_secure_port(
            address,
            grpc.ssl_server_credentials(
                ((settings.rpc_tls_key.read_bytes(), settings.rpc_tls_cert.read_bytes()),)
            ),
        )
    else:
        server.add_insecure_port(address)
    probes = {"agent_storage": service.store.check_health}
    if service.model_check is not None:
        probes["model_provider"] = service.model_check  # No credentials: report not ready.
    state = Readiness(
        settings.role,
        probes,
        interval=settings.probe_interval_seconds,
        capabilities=["agent_rpc_v1"],
    )
    health = health_server((settings.health_host, settings.health_port), state)
    threads = [
        threading.Thread(target=health.serve_forever, daemon=True),
        threading.Thread(target=state.run, args=(stop,), daemon=True),
    ]
    try:
        server.start()
        for thread in threads:
            thread.start()
        stop.wait()
    finally:
        stop.set()
        server.stop(grace=3).wait(timeout=4)
        health.shutdown()
        health.server_close()
        executor.shutdown(wait=True)
        for thread in threads:
            thread.join(timeout=5)
        service.close()
