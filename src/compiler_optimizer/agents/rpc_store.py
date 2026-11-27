"""Private per-service artifact catalog and durable agent operation deduplication."""

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.agents.model import ModelBudgetExceeded
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
            columns = {r[1] for r in db.execute("PRAGMA table_info(operations)")}
            for column in ("started_ms", "finished_ms"):
                if column not in columns:
                    db.execute(f"ALTER TABLE operations ADD COLUMN {column} INTEGER")
            db.execute(
                "UPDATE operations SET state='failed',error='owner_restart' WHERE state='running'"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_calls(call_id INTEGER PRIMARY KEY,"
                "task_id TEXT NOT NULL,role TEXT NOT NULL,purpose TEXT NOT NULL,"
                "backend TEXT NOT NULL,prompt_sha256 TEXT NOT NULL,prompt_key TEXT NOT NULL,"
                "prompt_size_bytes INTEGER NOT NULL,state TEXT NOT NULL,response_sha256 TEXT,"
                "response_key TEXT,response_size_bytes INTEGER,"
                "input_tokens INTEGER NOT NULL DEFAULT 0,output_tokens INTEGER NOT NULL DEFAULT 0,"
                "stop_reason TEXT,error TEXT,latency_ms INTEGER,created_unix_ms INTEGER NOT NULL)"
            )
            db.execute(
                "UPDATE model_calls SET state='failed',error='owner_restart' WHERE state='running'"
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
                "INSERT INTO operations(task_id,operation,request_sha256,state,attempts,"
                "response,error) "
                "VALUES(?,?,?,'running',1,NULL,NULL) "
                "ON CONFLICT(task_id,operation) DO UPDATE SET state='running', "
                "attempts=operations.attempts+1,error=NULL",
                (task_id, operation, digest),
            )
            db.execute(
                "UPDATE operations SET started_ms=?,finished_ms=NULL "
                "WHERE task_id=? AND operation=?",
                (time.time_ns() // 1_000_000, task_id, operation),
            )
        return None

    def finish(self, task_id, operation, *, response=None, error=None):
        with self.transaction() as db:
            db.execute(
                "UPDATE operations SET state=?,response=?,error=?,finished_ms=? "
                "WHERE task_id=? AND operation=? AND state='running'",
                (
                    "completed" if error is None else "failed",
                    response,
                    error,
                    time.time_ns() // 1_000_000,
                    task_id,
                    operation,
                ),
            )

    def reserve_model_call(self, task_id, role, purpose, backend, prompt, *, max_calls, max_tokens):
        """Atomically admit one model call under the task's call/token budget."""
        validate_task_id(task_id)
        ref = self.artifacts.put(prompt)
        self.attach(task_id, ref)
        with self.transaction() as db:
            calls, tokens = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(input_tokens+output_tokens),0) "
                "FROM model_calls WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if calls >= max_calls:
                raise ModelBudgetExceeded("task model call budget exhausted")
            if tokens >= max_tokens:
                raise ModelBudgetExceeded("task model token budget exhausted")
            cursor = db.execute(
                "INSERT INTO model_calls(task_id,role,purpose,backend,prompt_sha256,prompt_key,"
                "prompt_size_bytes,state,created_unix_ms) VALUES(?,?,?,?,?,?,?,'running',?)",
                (
                    task_id,
                    role,
                    purpose[:256],
                    backend,
                    ref.sha256,
                    ref.key,
                    ref.size_bytes,
                    int(time.time() * 1000),
                ),
            )
            return task_id, cursor.lastrowid

    def finish_model_call(self, call, *, status, reply=None, error=None, latency_ms=0):
        task_id, call_id = call
        response_ref = None
        if reply is not None:
            response_ref = self.artifacts.put(
                json.dumps(
                    {
                        "schema_version": 1,
                        "text": reply.text,
                        "input_tokens": reply.input_tokens,
                        "output_tokens": reply.output_tokens,
                        "stop_reason": reply.stop_reason,
                    },
                    sort_keys=True,
                ).encode()
            )
            self.attach(task_id, response_ref)
        with self.transaction() as db:
            db.execute(
                "UPDATE model_calls SET state=?,response_sha256=?,response_key=?,"
                "response_size_bytes=?,input_tokens=?,"
                "output_tokens=?,stop_reason=?,error=?,latency_ms=? "
                "WHERE call_id=? AND state='running'",
                (
                    status,
                    response_ref.sha256 if response_ref else None,
                    response_ref.key if response_ref else None,
                    response_ref.size_bytes if response_ref else None,
                    reply.input_tokens if reply else 0,
                    reply.output_tokens if reply else 0,
                    reply.stop_reason if reply else None,
                    (error or "")[:256] or None,
                    latency_ms,
                    call_id,
                ),
            )

    def list_model_calls(self, task_id):
        validate_task_id(task_id)
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM model_calls WHERE task_id=? ORDER BY call_id", (task_id,)
                )
            ]

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
