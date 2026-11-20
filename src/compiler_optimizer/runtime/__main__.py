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
from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.dependencies import check_coordinator, check_rabbitmq, check_redis
from compiler_optimizer.runtime.health import Readiness, health_server
from compiler_optimizer.runtime.worker_health import WorkerHealthService
from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository


def run(settings):
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
    if settings.role == "coordinator":
        repository = Repository(settings.state_directory)
        probes["storage"] = repository.check_health
        executor = ThreadPoolExecutor(max_workers=4)
        grpc_server = grpc.server(
            executor,
            options=[
                ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ],
        )
        rpc.add_CoordinatorServiceServicer_to_server(CoordinatorService(repository), grpc_server)
        rpc.add_WorkerHealthServiceServicer_to_server(WorkerHealthService(repository), grpc_server)
        if not grpc_server.add_insecure_port(f"0.0.0.0:{settings.grpc_port}"):
            raise RuntimeError("cannot bind coordinator gRPC port")
        grpc_server.start()
    else:
        probes["coordinator"] = lambda: check_coordinator(settings)

    server = None
    threads = []
    try:
        state = Readiness(settings.role, probes, interval=settings.probe_interval_seconds)
        server = health_server(
            (settings.health_host, settings.health_port),
            state,
            metrics=Metrics(repository=repository, readiness=state),
        )
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True),
            threading.Thread(target=state.run, args=(stop,), daemon=True),
        ]
        for thread in threads:
            thread.start()
        logging.info(
            json.dumps({"event": "started", "service": settings.role, "execution_enabled": False})
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
    parser.add_argument("--role", required=True, choices=("coordinator", "scheduler", "worker"))
    args = parser.parse_args()
    configure_logging()
    run(Settings.from_env(args.role))


if __name__ == "__main__":
    main()
