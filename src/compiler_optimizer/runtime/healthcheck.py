"""Docker health check against the local cached infrastructure readiness state."""

import json
import os
from urllib.request import urlopen


def main():
    try:
        port = int(os.environ.get("HEALTH_PORT", "8080"))
        with urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as response:
            payload = json.loads(response.read(16384))
        return 0 if payload.get("infrastructure_ready") is True else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
