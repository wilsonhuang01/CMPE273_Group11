"""Durable metrics, bounded labels, structured events, and HTTP scrape failure semantics."""

import json
import logging
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from prometheus_client.parser import text_string_to_metric_families
from test_recovery import setup

from compiler_optimizer.monitoring.events import emit
from compiler_optimizer.monitoring.metrics import Metrics
from compiler_optimizer.runtime.health import Readiness, health_server
from compiler_optimizer.storage import Repository


def samples(metrics):
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(metrics.render().decode())
        for s in family.samples
    }


def test_metrics_survive_restart_and_do_not_count_duplicate_results(tmp_path):
    repo, _, _, task, _, _, coordinator, _ = setup(tmp_path, infra=1)
    coordinator.run(task, agents=["performance"])
    metrics = Metrics(repository=repo)
    before = samples(metrics)
    assert before[("optimizer_retries_total", (("reason", "infrastructure_failure"),))] == 1
    assert before[("optimizer_tasks", (("status", "completed"),))] == 1
    assert before[("optimizer_task_duration_seconds_count", (("status", "completed"),))] == 1
    assert (
        before[
            (
                "optimizer_optimization_gain_ratio_bucket",
                (("le", "+Inf"), ("metric", "performance_gain")),
            )
        ]
        == 1
    )
    assert (
        sum(v for (name, _), v in before.items() if name == "optimizer_verification_results_total")
        == 3
    )
    coordinator.run(task)
    for result in repo.list_results(task):
        assert not repo.save_result(result)
    after = samples(Metrics(repository=Repository(repo.root)))
    changing = {
        "optimizer_metrics_snapshot_timestamp_seconds",
        "optimizer_worker_heartbeat_max_age_seconds",
    }
    assert {k: v for k, v in before.items() if k[0] not in changing} == {
        k: v for k, v in after.items() if k[0] not in changing
    }
    payload = metrics.render().decode()
    assert task not in payload
    assert "task_id=" not in payload and "worker_id=" not in payload


def test_crashes_quarantine_and_stale_workers_are_visible(tmp_path):
    repo, _, _, task, _, _, coordinator, _ = setup(tmp_path, crash=True)
    coordinator.run(task, agents=["performance"])
    repo.recovery_clock_ms = lambda: 10**15
    values = samples(Metrics(repository=repo))
    assert (
        values[
            (
                "optimizer_crashes_total",
                (("phase", "verification"), ("reason", "SANITIZER_FAILURE")),
            )
        ]
        == 2
    )
    assert (
        values[("optimizer_recovery_terminal_total", (("reason", "confirmed_program_crash"),))] == 1
    )
    assert values[("optimizer_workers", (("state", "stale"),))] == 2
    assert values[("optimizer_stale_assigned_workers", ())] == 0


def test_structured_logs_correlate_retries_and_selection_without_source_or_secrets(
    tmp_path, caplog
):
    repo, _, _, task, _, _, coordinator, _ = setup(tmp_path, infra=1)
    with caplog.at_level(logging.INFO, logger="compiler_optimizer.events"):
        coordinator.run(task, agents=["performance"])
    events = [
        json.loads(r.message) for r in caplog.records if r.name == "compiler_optimizer.events"
    ]
    assert {e["event"] for e in events} >= {
        "attempt_started",
        "infrastructure_retry",
        "attempt_completed",
        "task_completed",
    }
    assert all(e["task_id"] == task and e["timestamp"] for e in events)
    assert "compiler service lost" not in caplog.text
    assert "#include" not in caplog.text
    with pytest.raises(ValueError):
        emit("unsafe", exception_message="password")


def test_metrics_endpoint_and_scrape_failure_are_independent_of_liveness(tmp_path):
    repo = Repository(tmp_path / "state")
    state = Readiness("coordinator", {"storage": repo.check_health})
    state.probe_once()
    metrics = Metrics(repository=repo, readiness=state)
    server = health_server(("127.0.0.1", 0), state, metrics=metrics)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(url + "/metrics", timeout=3) as response:
            assert response.status == 200
            assert b'optimizer_service_ready{service="coordinator"} 1.0' in response.read()
        metrics.render = lambda: (_ for _ in ()).throw(OSError("secret"))
        with pytest.raises(HTTPError) as failed:
            urlopen(url + "/metrics", timeout=3)
        assert failed.value.code == 503 and b"secret" not in failed.value.read()
        with urlopen(url + "/healthz", timeout=3) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def checker(monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "check_observability", Path(__file__).parents[1] / "tools/check_observability.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = [0.0]
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    return module, clock


DATASOURCE = {"uid": "optimizer-prometheus", "type": "prometheus", "url": "http://prometheus:9090"}
QUERY_RESULT = {
    "results": {
        "A": {
            "status": 200,
            "frames": [
                {
                    "schema": {"fields": [{"type": "time"}, {"type": "number"}]},
                    "data": {"values": [[1234567890], [1]]},
                }
            ],
        }
    }
}


def test_grafana_check_runs_real_backend_query_without_optional_health_route(checker, monkeypatch):
    from unittest.mock import Mock

    module, _ = checker
    request = Mock(side_effect=[DATASOURCE, QUERY_RESULT])
    monkeypatch.setattr(module, "request_json", request)
    module.check_grafana_datasource("http://grafana", "test:password")
    calls = request.call_args_list
    assert [call.args[0] for call in calls] == [
        "http://grafana/api/datasources/uid/optimizer-prometheus",
        "http://grafana/api/ds/query",
    ]
    body = calls[1].kwargs["body"]
    assert body["queries"][0]["datasource"]["uid"] == "optimizer-prometheus"
    assert body["queries"][0]["expr"] == "vector(1)"
    assert all(call.kwargs["auth"] == "test:password" for call in calls)


@pytest.mark.parametrize("status", [404, 503])
def test_grafana_query_waits_for_provisioning_or_plugin_startup(checker, monkeypatch, status):
    from unittest.mock import Mock

    module, clock = checker
    pending = HTTPError("http://grafana/api/ds/query", status, "not ready", {}, None)
    request = Mock(side_effect=[DATASOURCE, pending, DATASOURCE, QUERY_RESULT])
    monkeypatch.setattr(module, "request_json", request)
    module.check_grafana_datasource("http://grafana", "test:password", ready_timeout=3)
    assert clock[0] == 1 and request.call_count == 4


def test_missing_grafana_datasource_remains_a_failure_after_bounded_wait(checker, monkeypatch):
    from unittest.mock import Mock

    module, clock = checker
    missing = HTTPError(
        "http://grafana/api/datasources/uid/optimizer-prometheus", 404, "not found", {}, None
    )
    request = Mock(side_effect=missing)
    monkeypatch.setattr(module, "request_json", request)
    with pytest.raises(RuntimeError, match="did not become ready") as error:
        module.check_grafana_datasource("http://grafana", "test:password", ready_timeout=2)
    assert error.value.__cause__ is missing
    assert request.call_count == 2 and clock[0] == 2


@pytest.mark.parametrize("status", [401, 403])
def test_grafana_auth_errors_are_not_retried(checker, monkeypatch, status):
    from unittest.mock import Mock

    module, clock = checker
    request = Mock(side_effect=HTTPError("http://grafana", status, "denied", {}, None))
    monkeypatch.setattr(module, "request_json", request)
    with pytest.raises(HTTPError):
        module.check_grafana_datasource("http://grafana", "test:password")
    assert request.call_count == 1 and clock[0] == 0


@pytest.mark.parametrize(
    "query",
    [
        {"status": 200, "frames": []},
        {"status": 200, "error": "backend failure"},
        {"status": 500, "frames": []},
    ],
)
def test_grafana_query_error_or_missing_data_cannot_pass(checker, monkeypatch, query):
    from unittest.mock import Mock

    module, _ = checker
    monkeypatch.setattr(
        module, "request_json", Mock(side_effect=[DATASOURCE, {"results": {"A": query}}])
    )
    with pytest.raises(AssertionError, match="Grafana Prometheus query"):
        module.check_grafana_datasource("http://grafana", "test:password")


def test_grafana_query_http_request_uses_json_post_and_basic_auth(checker, monkeypatch):
    import base64
    import io
    from unittest.mock import Mock

    module, _ = checker
    opener = Mock(return_value=io.BytesIO(b'{"results": {}}'))
    monkeypatch.setattr(module, "urlopen", opener)
    body = {"queries": [{"expr": "vector(1)"}]}
    assert module.request_json("http://grafana/api/ds/query", auth="test:password", body=body) == {
        "results": {}
    }
    request = opener.call_args.args[0]
    assert request.get_method() == "POST" and json.loads(request.data) == body
    assert request.get_header("Content-type") == "application/json"
    assert (
        request.get_header("Authorization")
        == "Basic " + base64.b64encode(b"test:password").decode()
    )
