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
