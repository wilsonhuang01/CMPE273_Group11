"""Private per-service artifact catalog and durable agent operation deduplication."""

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.shared.agents_rpc import MAX_AGENT_ARTIFACT_BYTES
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.transfer import validate_reference, validate_task_id
from compiler_optimizer.storage.artifacts import ArtifactStore
from compiler_optimizer.storage.errors import ConflictError, NotFoundError, StateError


class AgentStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.root / "agent.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise
        self.artifacts = ArtifactStore(self.root / "artifacts", max_bytes=MAX_AGENT_ARTIFACT_BYTES)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS identity(id TEXT PRIMARY KEY)")
            if not db.execute("SELECT 1 FROM identity").fetchone():
                db.execute("INSERT INTO identity VALUES(?)", (str(uuid4()),))
            self.service_id = db.execute("SELECT id FROM identity").fetchone()[0]
            db.execute(
                "CREATE TABLE IF NOT EXISTS artifacts(task_id TEXT,key TEXT,sha256 TEXT,"
                "size_bytes INTEGER, PRIMARY KEY(task_id,key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations(task_id TEXT,operation TEXT,"
                "request_sha256 TEXT,state TEXT,attempts INTEGER,response BLOB,error TEXT,"
                "PRIMARY KEY(task_id,operation))"
            )
            db.execute(
                "UPDATE operations SET state='failed',error='owner_restart' WHERE state='running'"
            )

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.root / "agent.sqlite3", isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def check_health(self):
        with self.transaction() as db:
            require(
                db.execute("SELECT id FROM identity").fetchone()[0] == self.service_id,
                "agent identity changed",
            )

    def attach(self, task_id, ref):
        validate_task_id(task_id)
        validate_reference(ref, MAX_AGENT_ARTIFACT_BYTES)
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)",
                (task_id, ref.key, ref.sha256, ref.size_bytes),
            )

    def get(self, task_id, ref):
        validate_task_id(task_id)
        validate_reference(ref, MAX_AGENT_ARTIFACT_BYTES)
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM artifacts WHERE task_id=? AND key=?", (task_id, ref.key)
            ).fetchone()
        if row is None:
            raise NotFoundError("artifact is not registered to this agent task")
        require(
            (row["sha256"], row["size_bytes"]) == (ref.sha256, ref.size_bytes),
            "agent artifact metadata mismatch",
        )
        return self.artifacts.get(ref)

    def begin(self, task_id, operation, digest, maximum):
        validate_task_id(task_id)
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE task_id=? AND operation=?", (task_id, operation)
            ).fetchone()
            if row is not None:
                if row["request_sha256"] != digest:
                    raise ConflictError("agent operation inputs are frozen")
                if row["state"] == "completed":
                    return row["response"]
                if row["state"] == "running":
                    raise StateError("agent operation is already running")
                require(row["attempts"] < maximum, "agent operation attempt budget exhausted")
            db.execute(
                "INSERT INTO operations VALUES(?,?,?,'running',1,NULL,NULL) "
                "ON CONFLICT(task_id,operation) DO UPDATE SET state='running', "
                "attempts=operations.attempts+1,error=NULL",
                (task_id, operation, digest),
            )
        return None

    def finish(self, task_id, operation, *, response=None, error=None):
        with self.transaction() as db:
            db.execute(
                "UPDATE operations SET state=?,response=?,error=? "
                "WHERE task_id=? AND operation=? AND state='running'",
                ("completed" if error is None else "failed", response, error, task_id, operation),
            )

    def task_artifacts(self, task_id):
        store = self

        class TaskArtifacts:
            def get(self, ref):
                return store.get(task_id, ref)

            def put(self, data):
                ref = store.artifacts.put(data)
                store.attach(task_id, ref)
                return ref

        return TaskArtifacts()
