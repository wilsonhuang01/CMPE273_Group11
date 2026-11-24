"""Verify the running scrape targets, queue metrics and provisioned Grafana dashboard."""

import argparse
import base64
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request_json(url, *, auth=None, body=None, timeout=10):
    request = Request(url, data=None if body is None else json.dumps(body).encode())
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if auth:
        request.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def check_grafana_datasource(grafana, auth, *, ready_timeout=30):
    """Exercise the same backend as dashboard panels, not optional plugin health support."""
    deadline = time.monotonic() + ready_timeout
    last_error = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "Grafana Prometheus datasource query did not become ready"
            ) from last_error
        try:
            datasource = request_json(
                grafana + "/api/datasources/uid/optimizer-prometheus",
                auth=auth,
                timeout=min(10, remaining),
            )
            assert datasource["uid"] == "optimizer-prometheus", "wrong provisioned datasource UID"
            assert datasource["type"] in ("prometheus", "grafana-prometheus-datasource"), (
                "wrong provisioned datasource type"
            )
            assert datasource["url"].rstrip("/") == "http://prometheus:9090", (
                "wrong provisioned Prometheus URL"
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Grafana datasource readiness deadline expired")
            result = request_json(
                grafana + "/api/ds/query",
                auth=auth,
                timeout=min(10, remaining),
                body={
                    "from": "now-1m",
                    "to": "now",
                    "queries": [
                        {
                            "refId": "A",
                            "datasource": {"type": datasource["type"], "uid": datasource["uid"]},
                            "expr": "vector(1)",
                            "instant": True,
                            "range": False,
                            "format": "time_series",
                            "intervalMs": 1000,
                            "maxDataPoints": 1,
                        }
                    ],
                },
            )
            query = result["results"]["A"]
            assert query.get("status") == 200 and not query.get("error"), (
                "Grafana Prometheus query failed"
            )
            samples = [
                frame["data"]["values"][i]
                for frame in query.get("frames", [])
                for i, field in enumerate(frame["schema"]["fields"])
                if field["type"] == "number"
            ]
            assert samples == [[1]], "Grafana Prometheus query returned no valid sentinel sample"
            return
        except HTTPError as error:
            # Provisioning/plugin startup can lag /api/health; auth failures are permanent.
            if error.code not in (404, 429, 500, 502, 503, 504):
                raise
            error.close()
            last_error = error
        except (URLError, TimeoutError) as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "Grafana Prometheus datasource query did not become ready"
            ) from last_error
        time.sleep(min(1, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-host", action="store_true")
    args = parser.parse_args()
    prometheus = "http://127.0.0.1:" + os.environ.get("PROMETHEUS_PORT", "9090")
    grafana = "http://127.0.0.1:" + os.environ.get("GRAFANA_PORT", "3000")
    targets = request_json(prometheus + "/api/v1/targets")["data"]["activeTargets"]
    required = [
        t
        for t in targets
        if t["labels"]["job"]
        in (
            ("optimizer-services", "rabbitmq", "optimizer-host")
            if args.require_host
            else ("optimizer-services", "rabbitmq")
        )
    ]
    assert len(required) == (5 if args.require_host else 4), "missing monitoring targets"
    assert all(t["health"] == "up" for t in required), "scrape target unavailable"
    query = request_json(
        prometheus + "/api/v1/query?" + urlencode({"query": "rabbitmq_queue_messages_ready"})
    )
    assert query["status"] == "success" and query["data"]["result"], "queue depth unavailable"
    auth = (
        os.environ.get("GRAFANA_ADMIN_USER", "admin")
        + ":"
        + os.environ.get("GRAFANA_ADMIN_PASSWORD", "grafana-dev-password")
    )
    dashboard = request_json(grafana + "/api/dashboards/uid/compiler-optimizer", auth=auth)[
        "dashboard"
    ]
    assert len(dashboard["panels"]) >= 14, "dashboard panels missing"
    workload = "optimizer-host" if args.require_host else "optimizer-services"
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expression = target["expr"].replace("$workload", workload)
            result = request_json(prometheus + "/api/v1/query?" + urlencode({"query": expression}))
            assert result["status"] == "success", "dashboard query failed"
    check_grafana_datasource(grafana, auth)
    print("PASS: live service/RabbitMQ scrapes, queue depth and provisioned Grafana dashboard")


if __name__ == "__main__":
    main()
