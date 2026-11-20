"""Serve the local coordinator intake/result API against a milestone state directory."""

import argparse
from concurrent.futures import ThreadPoolExecutor

import grpc

from compiler_optimizer.runtime.coordinator import CoordinatorService
from compiler_optimizer.runtime.worker_health import WorkerHealthService
from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="var/pipeline")
    parser.add_argument("--port", type=int, default=50052)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    repository = Repository(args.state)
    with ThreadPoolExecutor(max_workers=4) as executor:
        server = grpc.server(
            executor,
            options=[
                ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ],
        )
        rpc.add_CoordinatorServiceServicer_to_server(CoordinatorService(repository), server)
        rpc.add_WorkerHealthServiceServicer_to_server(WorkerHealthService(repository), server)
        if not server.add_insecure_port(f"127.0.0.1:{args.port}"):
            raise RuntimeError("cannot bind coordinator result API")
        server.start()
        print(f"Coordinator result API listening on 127.0.0.1:{args.port}", flush=True)
        try:
            server.wait_for_termination()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop(3).wait(timeout=4)


if __name__ == "__main__":
    main()
