"""Cached readiness probes; process liveness does not depend on infrastructure."""

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Readiness:
    def __init__(self, role, probes, *, interval=5, clock=time.monotonic):
        self.role = role
        self.probes = probes
        self.interval = interval
        self.clock = clock
        self._lock = threading.Lock()
        self._checks = {}
        self._checked = None

    def probe_once(self):
        checks = {}
        for name, probe in self.probes.items():
            try:
                probe()
                checks[name] = "ok"
            except Exception as error:
                # Do not include exception text: it may contain connection secrets.
                checks[name] = "error:" + type(error).__name__
        with self._lock:
            changed = checks != self._checks
            self._checks = checks
            self._checked = self.clock()
        if changed:
            logging.info(
                json.dumps({"event": "dependency_status", "service": self.role, "checks": checks})
            )

    def run(self, stop):
        while not stop.is_set():
            self.probe_once()
            stop.wait(self.interval)

    def snapshot(self):
        with self._lock:
            checks = dict(self._checks)
            fresh = self._checked is not None and self.clock() - self._checked <= max(
                15, self.interval * 3
            )
        return {
            "service": self.role,
            "infrastructure_ready": bool(checks)
            and fresh
            and all(value == "ok" for value in checks.values()),
            "execution_enabled": False,
            "capabilities": ["durable_intake"]
            if self.role == "coordinator"
            else ["queue_topology"]
            if self.role == "scheduler"
            else ["infrastructure_only"],
            "checks": checks,
        }


def health_server(address, state, *, metrics=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics" and metrics is not None:
                try:
                    body = metrics.render()
                except Exception as error:
                    logging.warning(
                        json.dumps({"event": "metrics_failed", "error_class": type(error).__name__})
                    )
                    self.send_error(503, "metrics snapshot unavailable")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path not in ("/healthz", "/readyz"):
                self.send_error(404)
                return
            payload = state.snapshot()
            code = 200 if self.path == "/healthz" or payload["infrastructure_ready"] else 503
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(address, Handler)
