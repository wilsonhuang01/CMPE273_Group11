"""Run coordinator, scheduler, or worker infrastructure services."""

import argparse
import json
import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor

import grpc

from compiler_optimizer.monitoring.events import configure_logging
from compiler_optimizer.monitoring.metrics import Metrics
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.connections import artifact_store, repository_database
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.dependencies import check_coordinator, check_rabbitmq, check_redis
from compiler_optimizer.runtime.dispatch import SubmissionDispatcher
from compiler_optimizer.runtime.fleet import FleetService
from compiler_optimizer.runtime.fleet_client import check_fleet
from compiler_optimizer.runtime.health import Readiness, health_server
from compiler_optimizer.runtime.security import BearerAuth, validate_rpc_security
from compiler_optimizer.runtime.transfer import ArtifactTransferService
from compiler_optimizer.runtime.worker_health import WorkerHealthService
from compiler_optimizer.scheduler.fleet import run_scheduler
from compiler_optimizer.shared.agents_rpc import AGENT_NAMES
from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.shared.workflow import default_workflow
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.fleet import run_worker
from compiler_optimizer.workers.remote import fleet_compiler


def run(settings):
    validate_rpc_security(settings)
    if settings.role in AGENT_NAMES:
        from compiler_optimizer.runtime.agent import run_agent

        return run_agent(settings)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    probes = {
        "rabbitmq": lambda: check_rabbitmq(settings, setup=settings.role == "scheduler"),
        "redis": lambda: check_redis(settings),
    }
    grpc_server = None
    executor = None
    repository = None
    dispatcher = None
    if settings.role == "coordinator":
        repository = Repository(
            settings.state_directory,
            artifacts=artifact_store(settings),
            database=repository_database(settings),
        )
        probes["storage"] = repository.check_health
        if settings.agent_backend == "rpc":
            from compiler_optimizer.agents.rpc_client import AgentClient

            probes["agents"] = AgentClient(repository, settings).check_health
        executor = ThreadPoolExecutor(max_workers=4)
        grpc_server = grpc.server(
            executor,
            interceptors=(BearerAuth(settings.api_token),),
            maximum_concurrent_rpcs=8,
            options=[
                ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ],
        )
        coordinator = CoordinatorService(
            repository,
            execution_enabled=settings.execution_enabled,
            workflow_configuration={
                **default_workflow(),
                "execution_backend": settings.execution_backend,
                "agent_backend": settings.agent_backend,
                **(
                    {"debate_policy": json.loads(settings.debate_policy_json)}
                    if settings.debate_policy_json
                    else {}
                ),
            },
        )
        rpc.add_CoordinatorServiceServicer_to_server(coordinator, grpc_server)
        if settings.transfer_enabled:
            rpc.add_ArtifactTransferServiceServicer_to_server(
                ArtifactTransferService(
                    repository,
                    coordinator,
                    compiler_resolver=(
                        lambda target, language: fleet_compiler(repository, target, language)
                    )
                    if settings.execution_backend == "remote"
                    else None,
                ),
                grpc_server,
            )
        if settings.fleet_enabled:
            rpc.add_FleetServiceServicer_to_server(FleetService(repository), grpc_server)
        if settings.execution_enabled:
            dispatcher = SubmissionDispatcher(repository, settings)
        rpc.add_WorkerHealthServiceServicer_to_server(WorkerHealthService(repository), grpc_server)
        host = settings.grpc_host.strip("[]")
        address = (
            f"[{host}]:{settings.grpc_port}" if ":" in host else f"{host}:{settings.grpc_port}"
        )
        if settings.rpc_tls_cert:
            credentials = grpc.ssl_server_credentials(
                ((settings.rpc_tls_key.read_bytes(), settings.rpc_tls_cert.read_bytes()),)
            )
            port = grpc_server.add_secure_port(address, credentials)
        else:
            port = grpc_server.add_insecure_port(address)
        if not port:
            raise RuntimeError("cannot bind coordinator gRPC port")
        grpc_server.start()
    else:
        if settings.service_enabled:
            probes.pop("redis")
            probes["coordinator"] = lambda: check_fleet(settings)
        else:
            probes["coordinator"] = lambda: check_coordinator(settings)

    server = None
    threads = []
    try:
        component = None
        capabilities = None
        worker_enabled = settings.role == "worker" and settings.service_enabled
        if settings.role != "coordinator" and settings.service_enabled:
            ready = threading.Event()

            def check_component():
                if not ready.is_set():
                    raise RuntimeError("service loop is not ready")

            probes["service_loop"] = check_component
            action = run_worker if worker_enabled else run_scheduler
            component = threading.Thread(target=action, args=(settings, stop, ready), daemon=True)
            capabilities = (
                ["sandbox_worker_execution"] if worker_enabled else ["durable_worker_dispatch"]
            )
        state = Readiness(
            settings.role,
            probes,
            interval=settings.probe_interval_seconds,
            execution_enabled=dispatcher is not None or worker_enabled,
            capabilities=capabilities,
        )
        server = health_server(
            (settings.health_host, settings.health_port),
            state,
            metrics=Metrics(repository=repository, readiness=state),
        )
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True),
            threading.Thread(target=state.run, args=(stop,), daemon=True),
        ]
        if dispatcher is not None:
            threads.append(threading.Thread(target=dispatcher.run, args=(stop,), daemon=True))
        if component is not None:
            threads.append(component)
        for thread in threads:
            thread.start()
        logging.info(
            json.dumps(
                {
                    "event": "started",
                    "service": settings.role,
                    "execution_enabled": dispatcher is not None or worker_enabled,
                }
            )
        )
        stop.wait()
    finally:
        stop.set()
        if server:
            server.shutdown()
            server.server_close()
        if grpc_server:
            grpc_server.stop(grace=3).wait(timeout=4)
        if executor:
            executor.shutdown(wait=True)
        for thread in threads:
            thread.join(timeout=5)
        logging.info(json.dumps({"event": "stopped", "service": settings.role}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role", required=True, choices=("coordinator", "scheduler", "worker", *AGENT_NAMES)
    )
    args = parser.parse_args()
    configure_logging()
    run(Settings.from_env(args.role))


if __name__ == "__main__":
    main()
