"""Action R phase 2: PostgreSQL repository backend.

Dialect translation, rows and configuration are checked offline. Tests that need a
server run with ``--postgres-url`` (or ``TEST_POSTGRES_URL``); passing the same option
also reruns the whole Repository suite on PostgreSQL (see conftest.py).
"""

import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.connections import repository_database
from compiler_optimizer.shared.contracts import BASE_FLAGS
from compiler_optimizer.shared.manifest import parse_manifest
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import (
    ConflictError,
    NotFoundError,
    StateError,
    UnavailableError,
)
from compiler_optimizer.storage.postgres import (
    PostgresDatabase,
    Row,
    translate,
    validate_database_url,
)

LOCAL = "postgresql://optimizer@127.0.0.1:5432/optimizer"
RDS = "postgresql://optimizer@db.abc.us-west-2.rds.amazonaws.com:5432/optimizer"


# Offline --------------------------------------------------------------------------


IGNORE = " ON CONFLICT DO NOTHING"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t WHERE a=? AND b=?", "SELECT * FROM t WHERE a=%s AND b=%s"),
        ("SELECT '?' , \"q?\" FROM t WHERE a=?", "SELECT '?' , \"q?\" FROM t WHERE a=%s"),
        ("SELECT 1 WHERE x LIKE ? || '%'", "SELECT 1 WHERE x LIKE %s || '%%'"),
        ("INSERT OR IGNORE INTO a VALUES (?, ?)", "INSERT INTO a VALUES (%s, %s)" + IGNORE),
        ("  insert or ignore into a(x) VALUES(?);", "INSERT INTO a(x) VALUES(%s)" + IGNORE),
        (
            "INSERT INTO a VALUES (?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            "INSERT INTO a VALUES (%s) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        ),
    ],
)
def test_sqlite_dialect_translation(sql, expected):
    assert translate(sql) == expected


def test_statements_without_parameters_keep_literal_percent_signs():
    assert translate("SELECT '100%'", False) == "SELECT '100%'"


def test_rows_behave_like_sqlite_rows():
    row = Row(("task_id", "status"), ("t1", "running"), {"task_id": 0, "status": 1})
    assert (row[0], row["status"], len(row)) == ("t1", "running", 2)
    assert dict(row) == {"task_id": "t1", "status": "running"}
    assert list(row) == ["t1", "running"] and row.keys() == ["task_id", "status"]
    with pytest.raises(IndexError):
        row["missing"]


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (LOCAL, None),
        ("postgres://optimizer@localhost/optimizer", None),
        (RDS + "?sslmode=verify-full&sslrootcert=/etc/rds/global-bundle.pem", None),
        (RDS, "verify-full"),
        (RDS + "?sslmode=require", "verify-full"),
        ("postgresql://optimizer:secret@127.0.0.1/optimizer", "password"),
        ("postgresql://127.0.0.1/optimizer?password=secret", "password"),
        ("postgresql://127.0.0.1", "database"),
        ("mysql://127.0.0.1/optimizer", "postgresql"),
    ],
)
def test_database_urls_require_tls_remotely_and_never_embed_passwords(url, message):
    if message is None:
        validate_database_url(url)
    else:
        with pytest.raises(ValueError, match=message):
            validate_database_url(url)


def test_settings_configure_the_coordinator_database():
    env = {"DATABASE_URL": LOCAL, "DATABASE_PASSWORD": "db-secret", "DATABASE_SCHEMA": "staging"}
    settings = Settings.from_env("coordinator", env)
    assert (settings.database_url, settings.database_schema) == (LOCAL, "staging")
    assert "db-secret" not in repr(settings) and LOCAL not in repr(settings)
    database = repository_database(settings)
    assert database.identity == "postgresql://127.0.0.1:5432/optimizer?schema=staging"
    assert "db-secret" not in repr(database)
    assert repository_database(Settings.from_env("coordinator", {})) is None
    with pytest.raises(ValueError, match="coordinator"):
        Settings.from_env("worker", {"DATABASE_URL": LOCAL, "EXECUTION_ENABLED": "false"})
    for schema in ("Public", "pg_catalog", "a-b", "x" * 64):
        with pytest.raises(ValueError, match="DATABASE_SCHEMA"):
            Settings.from_env("coordinator", {"DATABASE_SCHEMA": schema})


def test_missing_driver_fails_clearly_at_first_use(monkeypatch, tmp_path):
    database = PostgresDatabase(LOCAL)  # Configuration does not need the driver.
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(RuntimeError, match=r"\[postgres\]"):
        Repository(tmp_path, database=database)


# Server-backed --------------------------------------------------------------------


@pytest.fixture
def url(pytestconfig):
    value = pytestconfig.getoption("postgres_url")
    if not value:
        pytest.skip("needs --postgres-url or TEST_POSTGRES_URL")
    return value


@pytest.fixture
def schema(url):
    import psycopg

    name = "phase2_" + uuid4().hex[:16]
    yield name
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def submission(repo):
    manifest = Path(__file__).resolve().parents[1] / "examples/sum-array/submission.json"
    task_id = str(uuid4())
    return pb.Submission(
        meta=pb.Metadata(
            schema_version=1,
            message_id=str(uuid4()),
            task_id=task_id,
            created_at_unix_ms=1_800_000_000_000,
        ),
        manifest=parse_manifest(manifest.read_bytes()),
        package=repo.artifacts.put(b"package " + task_id.encode()),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(b"source")),
        compiler=pb.CompilerSettings(
            executable="clang",
            version="18.1.8",
            image_digest="sha256:" + "a" * 64,
            language="c17",
            target="linux-x86_64",
            flags=["-std=c17", *BASE_FLAGS],
        ),
    )


def test_coordinators_on_separate_hosts_share_committed_state(url, schema, tmp_path):
    # Separate roots and pools stand in for coordinator replacement on another host.
    first = Repository(tmp_path / "host-a", database=PostgresDatabase(url, schema=schema))
    message = submission(first)
    assert first.save_submission(message)
    second = Repository(tmp_path / "host-b", database=PostgresDatabase(url, schema=schema))
    second.artifacts = first.artifacts  # Shared object storage (S3 in AWS).
    task_id = message.meta.task_id
    assert [t["task_id"] for t in second.list_tasks()] == [task_id]
    second.transition(task_id, expected_revision=0, status="running", reason="replacement")
    assert first.get_task(task_id)["status"] == "running"
    with pytest.raises(ConflictError):
        first.transition(task_id, expected_revision=0, status="failed", reason="stale")
    assert not second.save_submission(message)  # Redelivery still deduplicates.
    assert [h["status"] for h in first.history(task_id)] == ["accepted", "running"]


def test_committed_state_survives_abrupt_process_exit(url, schema, tmp_path):
    repo = Repository(tmp_path, database=PostgresDatabase(url, schema=schema))
    message = submission(repo)
    repo.save_submission(message)
    script = (
        "import os, sys\n"
        "from compiler_optimizer.storage import Repository\n"
        "from compiler_optimizer.storage.postgres import PostgresDatabase\n"
        "db = PostgresDatabase(sys.argv[1], schema=sys.argv[2])\n"
        "Repository(sys.argv[3], database=db).transition(\n"
        "    sys.argv[4], expected_revision=0, status='running', reason='child')\n"
        "os._exit(0)\n"
    )
    subprocess.run(
        [sys.executable, "-c", script, url, schema, str(tmp_path), message.meta.task_id],
        check=True,
        timeout=60,
    )
    assert repo.get_task(message.meta.task_id)["status"] == "running"


def test_failed_statement_rolls_back_the_whole_transaction(url, schema, tmp_path):
    database = PostgresDatabase(url, schema=schema)
    repo = Repository(tmp_path, database=database)
    with repo._transaction() as db:
        db.execute(
            "CREATE FUNCTION deny() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'disk test'; "
            "END $$"
        )
        # History is written last, after the task, message and artifact links.
        db.execute(
            "CREATE TRIGGER deny_history BEFORE INSERT ON task_history "
            "FOR EACH ROW EXECUTE FUNCTION deny()"
        )
    message = submission(repo)
    with pytest.raises(ConflictError, match="disk test"):
        repo.save_submission(message)
    assert repo.list_tasks() == []
    with pytest.raises(NotFoundError), repo._transaction(write=False) as db:
        repo._task(db, message.meta.task_id)
    with repo._transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM task_artifacts").fetchone()[0] == 0


def test_writers_are_serialized_like_sqlite_immediate_transactions(url, schema, tmp_path):
    repo = Repository(tmp_path, database=PostgresDatabase(url, schema=schema))
    holding, order = threading.Event(), []

    def first():
        with repo._transaction() as db:
            holding.set()
            time.sleep(0.3)
            db.execute("SELECT 1")
            order.append("first committed")

    thread = threading.Thread(target=first)
    thread.start()
    holding.wait(5)
    with repo._transaction():
        order.append("second started")
    thread.join()
    assert order == ["first committed", "second started"]


def test_lock_timeouts_and_outages_are_retryable_unavailable_errors(url, schema, tmp_path):
    database = PostgresDatabase(url, schema=schema, lock_timeout_ms=100)
    repo = Repository(tmp_path, database=database)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with repo._transaction():
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(5)
    try:
        with pytest.raises(UnavailableError) as raised, repo._transaction():
            pass
        assert isinstance(raised.value, OSError)
    finally:
        release.set()
        thread.join()
    closed = PostgresDatabase("postgresql://optimizer@127.0.0.1:1/optimizer", connect_timeout=1)
    with pytest.raises(UnavailableError):
        closed.check()


def test_pool_replaces_connections_closed_by_a_restart_or_failover(url, schema, tmp_path):
    import psycopg

    database = PostgresDatabase(url, schema=schema)
    repo = Repository(tmp_path, database=database)
    with repo._transaction(write=False) as db:
        pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.1)
    assert repo.list_tasks() == []  # The dead pooled connection is replaced transparently.


def test_read_transactions_are_read_only_snapshots(url, schema, tmp_path):
    repo = Repository(tmp_path, database=PostgresDatabase(url, schema=schema))
    with pytest.raises(UnavailableError, match="read-only"), repo._transaction(write=False) as db:
        db.execute("DELETE FROM tasks")


def test_unknown_schema_version_is_rejected_without_changes(url, schema, tmp_path):
    database = PostgresDatabase(url, schema=schema)
    repo = Repository(tmp_path, database=database)
    message = submission(repo)
    repo.save_submission(message)
    with repo._transaction() as db:
        db.execute("INSERT INTO schema_version VALUES (99, 0)")
    with pytest.raises(StateError, match="99"):
        Repository(tmp_path / "other", database=PostgresDatabase(url, schema=schema))
    with repo._transaction() as db:
        db.execute("DELETE FROM schema_version WHERE version=99")
    assert repo.get_task(message.meta.task_id)["status"] == "accepted"
