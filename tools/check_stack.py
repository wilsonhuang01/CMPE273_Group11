"""Check the Compose application's live HTTP readiness endpoints (stdlib only)."""

import json
import os
from urllib.request import urlopen


def main():
    for role, variable, default in (
        ("coordinator", "COORDINATOR_HEALTH_PORT", "18080"),
        ("scheduler", "SCHEDULER_HEALTH_PORT", "18081"),
        ("worker", "WORKER_HEALTH_PORT", "18082"),
    ):
        port = int(os.environ.get(variable, default))
        with urlopen(f"http://127.0.0.1:{port}/readyz", timeout=3) as response:
            payload = json.loads(response.read(16384))
        assert payload["service"] == role, payload
        assert payload["infrastructure_ready"] is True, payload
        assert payload["execution_enabled"] is False, payload
        assert payload["checks"]["rabbitmq"] == "ok", payload
        assert payload["checks"]["redis"] == "ok", payload
        print(f"PASS: {role} infrastructure ready; execution remains disabled")


if __name__ == "__main__":
    main()
