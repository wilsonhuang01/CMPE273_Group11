"""Serve durable host-workflow metrics independently of a running optimization command."""

import argparse
import signal
import threading

from compiler_optimizer.monitoring.events import configure_logging, emit
from compiler_optimizer.monitoring.metrics import Metrics
from compiler_optimizer.runtime.health import Readiness, health_server
from compiler_optimizer.storage import Repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="var/cohort")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18083)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    configure_logging()
    repo = Repository(args.state)
    state = Readiness("exporter", {"storage": repo.check_health})
    state.probe_once()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    server = health_server((args.host, args.port), state, metrics=Metrics(repository=repo))
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True),
        threading.Thread(target=state.run, args=(stop,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    emit("metrics_exporter_started", role="exporter")
    try:
        stop.wait()
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        for thread in threads:
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
