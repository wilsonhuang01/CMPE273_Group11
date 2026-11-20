"""Verify the running scrape targets, queue metrics and provisioned Grafana dashboard."""

import argparse
import base64
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def get(url, *, auth=None):
    request = Request(url)
    if auth:
        request.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-host", action="store_true")
    args = parser.parse_args()
    prometheus = "http://127.0.0.1:" + os.environ.get("PROMETHEUS_PORT", "9090")
    grafana = "http://127.0.0.1:" + os.environ.get("GRAFANA_PORT", "3000")
    targets = get(prometheus + "/api/v1/targets")["data"]["activeTargets"]
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
    query = get(
        prometheus + "/api/v1/query?" + urlencode({"query": "rabbitmq_queue_messages_ready"})
    )
    assert query["status"] == "success" and query["data"]["result"], "queue depth unavailable"
    auth = (
        os.environ.get("GRAFANA_ADMIN_USER", "admin")
        + ":"
        + os.environ.get("GRAFANA_ADMIN_PASSWORD", "grafana-dev-password")
    )
    dashboard = get(grafana + "/api/dashboards/uid/compiler-optimizer", auth=auth)["dashboard"]
    assert len(dashboard["panels"]) >= 14, "dashboard panels missing"
    workload = "optimizer-host" if args.require_host else "optimizer-services"
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expression = target["expr"].replace("$workload", workload)
            result = get(prometheus + "/api/v1/query?" + urlencode({"query": expression}))
            assert result["status"] == "success", "dashboard query failed"
    datasource = get(grafana + "/api/datasources/uid/optimizer-prometheus/health", auth=auth)
    assert datasource["status"] == "OK", "Grafana cannot query Prometheus"
    print("PASS: live service/RabbitMQ scrapes, queue depth and provisioned Grafana dashboard")


if __name__ == "__main__":
    main()
