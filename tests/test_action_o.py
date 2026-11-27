"""Restart-safe monitoring, safe usage export and release reproduction inputs."""

import importlib.util
import json
import sqlite3
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.agents.model import ModelReply
from compiler_optimizer.agents.rpc_store import AgentStore
from compiler_optimizer.monitoring.metrics import Metrics
from compiler_optimizer.storage import Repository

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import reproduction  # noqa: E402


def test_agent_metrics_and_redacted_usage_survive_restart(tmp_path):
    task = str(uuid4())
    store = AgentStore(tmp_path)
    store.begin(task, "generate:private-operation", "digest", 2)
    call = store.reserve_model_call(
        task,
        "performance",
        "generate",
        "model:identity",
        b"SECRET prompt",
        max_calls=3,
        max_tokens=100,
    )
    store.finish_model_call(
        call,
        status="completed",
        reply=ModelReply("SECRET reply", 11, 7, "end_turn"),
        latency_ms=250,
    )
    store.finish(task, "generate:private-operation", response=b"SECRET response")
    payload = Metrics(agent_store=store).render()
    assert b'optimizer_model_tokens_total{direction="input"} 11.0' in payload
    assert b"optimizer_model_calls_total 1.0" in payload
    assert (
        b'optimizer_agent_operation_duration_seconds_count{method="generate",state="completed"} 1.0'
        in payload
    )
    assert b"SECRET" not in payload and task.encode() not in payload
    exported = reproduction.usage(tmp_path)
    assert exported[0]["backend"] == "model:identity"
    assert "SECRET" not in json.dumps(exported)
    store.close()
    reopened = AgentStore(tmp_path)
    try:
        assert reopened.begin(task, "generate:private-operation", "digest", 2) == b"SECRET response"
        assert Metrics(agent_store=reopened).render() == payload
    finally:
        reopened.close()


def test_old_agent_database_migrates_without_losing_cached_operations(tmp_path):
    with sqlite3.connect(tmp_path / "agent.sqlite3") as db:
        db.execute(
            "CREATE TABLE operations(task_id TEXT,operation TEXT,request_sha256 TEXT,"
            "state TEXT,attempts INTEGER,response BLOB,error TEXT,"
            "PRIMARY KEY(task_id,operation))"
        )
        db.execute(
            "INSERT INTO operations VALUES('old','generate:x','hash','completed',1,?,NULL)",
            (b"cached",),
        )
    store = AgentStore(tmp_path)
    try:
        assert b"optimizer_model_calls_total 0.0" in Metrics(agent_store=store).render()
        with store.transaction() as db:
            assert db.execute("SELECT response FROM operations").fetchone()[0] == b"cached"
    finally:
        store.close()


def test_empty_repository_exports_fleet_capacity(tmp_path):
    payload = Metrics(repository=Repository(tmp_path)).render()
    assert b'optimizer_fleet_workers{state="idle"} 0.0' in payload
    assert b'optimizer_fleet_workers{state="stale"} 0.0' in payload


def test_release_has_provenance_constraints_and_no_local_setup(tmp_path):
    spec = importlib.util.spec_from_file_location("project_tool", ROOT / "tools/project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = module.package(tmp_path / "release.tar.gz", [])
    with tarfile.open(destination) as archive:
        names = archive.getnames()
        assert "project/constraints.txt" in names
        assert "project/.env.example" in names
        assert not any(
            part in {".env", ".venv", "__pycache__", ".git"}
            for name in names
            for part in Path(name).parts
        )
        record = json.load(archive.extractfile("provenance.json"))
        assert record["configuration_sha256"]["constraints.txt"]
        assert record["source_sha256"]["src/compiler_optimizer/monitoring/stages.py"]
        assert record["model_usage"] == []
