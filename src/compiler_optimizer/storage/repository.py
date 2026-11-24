"""Transactional, coordinator-owned SQLite state using the v1 wire contracts."""

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from compiler_optimizer.shared.agent_roles import role_spec
from compiler_optimizer.shared.benchmarking import BenchmarkPlan, analyze, invalidated_role
from compiler_optimizer.shared.contracts import (
    validate_proposal_for_request,
    validate_result_for_job,
)
from compiler_optimizer.shared.correctness import validate_evidence
from compiler_optimizer.shared.lease_context import EXECUTION_LEASE
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.messaging import decode_message, encode_message
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.workflow import validate_workflow
from compiler_optimizer.storage.artifacts import ArtifactStore
from compiler_optimizer.storage.concurrency import ConcurrencyStorage
from compiler_optimizer.storage.debate import DebateStorage
from compiler_optimizer.storage.errors import ConflictError, NotFoundError, StateError
from compiler_optimizer.storage.execution import ExecutionStorage
from compiler_optimizer.storage.fleet import FleetStorage
from compiler_optimizer.storage.pipeline import PipelineStorage
from compiler_optimizer.storage.recovery import RecoveryStorage
from compiler_optimizer.storage.selection import SelectionStorage


def _now() -> int:
    return time.time_ns() // 1_000_000


class Repository(
    PipelineStorage,
    SelectionStorage,
    RecoveryStorage,
    ExecutionStorage,
    FleetStorage,
    ConcurrencyStorage,
    DebateStorage,
):
    """Use one instance per service; operations open their own SQLite connection.

    All processes accessing this directory must be on the same host. This class
    does not implement broker acknowledgement, worker assignment, or selection policy.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "state.sqlite3"
        self.artifacts = ArtifactStore(self.root / "artifacts")
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                schema = files("compiler_optimizer.storage").joinpath("schema.sql").read_text()
                for statement in schema.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("PRAGMA user_version = 1")
                version = 1
            for target, name in (
                (2, "benchmark.sql"),
                (3, "pipeline.sql"),
                (4, "agents.sql"),
                (5, "selection.sql"),
                (6, "recovery.sql"),
                (7, "execution.sql"),
                (8, "fleet.sql"),
                (9, "recovery_scopes.sql"),
                (10, "concurrency.sql"),
                (11, "agent_rpc.sql"),
                (12, "debate.sql"),
            ):
                if version == target - 1:
                    schema = files("compiler_optimizer.storage").joinpath(name).read_text()
                    for statement in schema.split(";"):
                        if statement.strip():
                            db.execute(statement)
                    db.execute(f"PRAGMA user_version = {target}")
                    version = target
            if version != 12:
                raise StateError(f"unsupported database schema version: {version}")

    @contextmanager
    def _transaction(self, *, write=True):
        db = sqlite3.connect(self.database, isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            lease = EXECUTION_LEASE.get()
            if write and lease is not None and lease["database"] == str(self.database):
                self._assert_recovery_fence(db, lease)
            yield db
            if write and lease is not None and lease["database"] == str(self.database):
                self._assert_recovery_fence(db, lease)
            db.commit()
        except sqlite3.IntegrityError as error:
            db.rollback()
            raise ConflictError(str(error)) from error
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _task(db, task_id, *, running=False):
        row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"task: {task_id}")
        if running and row["status"] != "running":
            raise StateError("task must be running")
        return row

    @staticmethod
    def _seen(db, message, payload):
        row = db.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message.meta.message_id,)
        ).fetchone()
        if row is None:
            return False
        if row["kind"] != message.DESCRIPTOR.full_name or row["payload"] != payload:
            raise ConflictError("message_id reused with different contents")
        return True

    @staticmethod
    def _record(db, message, payload):
        db.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            (message.meta.message_id, message.meta.task_id, message.DESCRIPTOR.full_name, payload),
        )

    def _artifact(self, db, task_id, ref, kind, *, owner="task"):
        self.artifacts.get(ref)
        db.execute(
            "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?)",
            (ref.key, ref.sha256, ref.size_bytes),
        )
        row = db.execute("SELECT * FROM artifacts WHERE key = ?", (ref.key,)).fetchone()
        if row["sha256"] != ref.sha256 or row["size_bytes"] != ref.size_bytes:
            raise ConflictError("artifact metadata conflict")
        db.execute(
            "INSERT OR IGNORE INTO task_artifacts VALUES (?, ?, ?, ?)",
            (task_id, ref.key, kind, owner),
        )

    @staticmethod
    def _history(db, task_id, status, revision, reason):
        db.execute(
            "INSERT INTO task_history(task_id, status, revision, reason, recorded_at_unix_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, status, revision, reason, _now()),
        )

    def save_submission(self, message: pb.Submission, *, execution=None) -> bool:
        payload = encode_message(message)
        configuration = None if execution is None else validate_workflow(execution)
        if execution is not None:
            from compiler_optimizer.workers.package import package_from_files, unbundle

            package = package_from_files(unbundle(self.artifacts.get(message.package)))
            require(package.manifest == message.manifest, "submission package manifest mismatch")
            require(
                package.source_bundle == self.artifacts.get(message.source.bundle),
                "submission package/source mismatch",
            )
        with self._transaction() as db:
            if self._seen(db, message, payload):
                return False
            db.execute(
                "INSERT INTO tasks VALUES (?, ?, 'accepted', 0)", (message.meta.task_id, payload)
            )
            self._artifact(db, message.meta.task_id, message.package, "package")
            self._artifact(db, message.meta.task_id, message.source.bundle, "source")
            self._record(db, message, payload)
            self._history(db, message.meta.task_id, "accepted", 0, "Submission accepted")
            if configuration is not None:
                db.execute(
                    "INSERT INTO submission_executions(task_id, configuration_json) VALUES (?, ?)",
                    (message.meta.task_id, configuration),
                )
        return True

    def get_task(self, task_id: str) -> dict:
        with self._transaction(write=False) as db:
            row = dict(self._task(db, task_id))
        row["submission"] = decode_message(pb.Submission, row["submission"])
        return row

    def check_health(self) -> None:
        """Check the existing database without initializing or scanning task payloads."""
        with self._transaction(write=False) as db:
            db.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()

    def history(self, task_id: str) -> list[dict]:
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM task_history WHERE task_id = ? ORDER BY revision", (task_id,)
                )
            ]

    def list_tasks(self, *, status: str | None = None) -> list[dict]:
        if status is not None and status not in {"accepted", "running", "completed", "failed"}:
            raise ValueError("unknown task status")
        with self._transaction(write=False) as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE (? IS NULL OR status = ?) ORDER BY rowid",
                (status, status),
            ).fetchall()
        tasks = [dict(row) for row in rows]
        for task in tasks:
            task["submission"] = decode_message(pb.Submission, task["submission"])
        return tasks

    def transition(self, task_id: str, *, expected_revision: int, status: str, reason: str) -> None:
        if not reason.strip() or len(reason.encode()) > 4096:
            raise ValueError("transition reason must be 1–4096 UTF-8 bytes")
        with self._transaction() as db:
            row = self._task(db, task_id)
            if row["revision"] != expected_revision:
                raise ConflictError("task revision changed")
            allowed = {"accepted": {"running", "failed"}, "running": {"failed"}}
            if status not in allowed.get(row["status"], set()):
                raise StateError("invalid task transition; completion requires select_result")
            revision = row["revision"] + 1
            db.execute(
                "UPDATE tasks SET status = ?, revision = ? WHERE task_id = ?",
                (status, revision, task_id),
            )
            self._history(db, task_id, status, revision, reason)

    @staticmethod
    def _submission(row):
        return decode_message(pb.Submission, row["submission"])

    @staticmethod
    def _load(db, table, cls, key, value):
        # table/key are internal constants at all call sites, never user inputs.
        row = db.execute(f"SELECT payload FROM {table} WHERE {key} = ?", (value,)).fetchone()
        if row is None:
            raise NotFoundError(f"{table}: {value}")
        return decode_message(cls, row["payload"])

    def save_request(self, message: pb.OptimizationRequest) -> bool:
        payload = encode_message(message)
        with self._transaction() as db:
            if self._seen(db, message, payload):
                return False
            submission = self._submission(self._task(db, message.meta.task_id, running=True))
            parents = [submission.source]
            parents.extend(
                decode_message(pb.Proposal, row[0]).source
                for row in db.execute(
                    "SELECT payload FROM proposals WHERE task_id = ?", (message.meta.task_id,)
                )
            )
            if message.source not in parents:
                raise ConflictError("unknown parent source for task")
            if message.compiler != submission.compiler or message.manifest != submission.manifest:
                raise ConflictError("request changes task build profile or tests")
            db.execute(
                "INSERT INTO requests VALUES (?, ?, ?)",
                (message.proposal_id, message.meta.task_id, payload),
            )
            self._record(db, message, payload)
        return True

    def save_proposal(self, message: pb.Proposal) -> bool:
        payload = encode_message(message)
        with self._transaction() as db:
            if self._seen(db, message, payload):
                return False
            self._task(db, message.meta.task_id, running=True)
            request = self._load(
                db, "requests", pb.OptimizationRequest, "proposal_id", message.proposal_id
            )
            validate_proposal_for_request(message, request)
            db.execute(
                "INSERT INTO proposals VALUES (?, ?, ?)",
                (message.proposal_id, message.meta.task_id, payload),
            )
            self._artifact(
                db, message.meta.task_id, message.source.bundle, "source", owner=message.proposal_id
            )
            self._record(db, message, payload)
        return True

    def list_proposals(self, task_id: str) -> list[pb.Proposal]:
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                decode_message(pb.Proposal, row[0])
                for row in db.execute(
                    "SELECT payload FROM proposals WHERE task_id = ? ORDER BY rowid", (task_id,)
                )
            ]

    def save_job(self, message: pb.VerificationJob) -> bool:
        payload = encode_message(message)
        with self._transaction() as db:
            if self._seen(db, message, payload):
                return False
            submission = self._submission(self._task(db, message.meta.task_id, running=True))
            source = submission.source
            if message.WhichOneof("subject") == "proposal_id":
                self._selection_event_guard(db, message.meta.task_id, message.proposal_id)
                proposal = self._load(
                    db, "proposals", pb.Proposal, "proposal_id", message.proposal_id
                )
                if proposal.meta.task_id != message.meta.task_id:
                    raise ConflictError("proposal belongs to another task")
                source = proposal.source
            if (
                message.source != source
                or message.compiler != submission.compiler
                or message.manifest != submission.manifest
            ):
                raise ConflictError("job changes source, build profile, or tests")
            previous = db.execute(
                "SELECT payload FROM jobs WHERE job_id = ? ORDER BY attempt DESC LIMIT 1",
                (message.job_id,),
            ).fetchone()
            if previous is None:
                if message.attempt != 1:
                    raise ConflictError("first job attempt must be 1")
            else:
                old = decode_message(pb.VerificationJob, previous[0])
                if old.meta.task_id != message.meta.task_id:
                    raise ConflictError("job ID belongs to another task")
                if message.attempt != old.attempt + 1:
                    raise ConflictError("job attempts must increase by one")
                completed = db.execute(
                    "SELECT payload FROM results WHERE job_id = ? AND attempt = ?",
                    (old.job_id, old.attempt),
                ).fetchone()
                if (
                    completed
                    and decode_message(pb.VerificationResult, completed[0]).WhichOneof("outcome")
                    == "success"
                ):
                    raise StateError("cannot retry a successful job")
                comparable = pb.VerificationJob()
                comparable.CopyFrom(message)
                comparable.meta.CopyFrom(old.meta)
                comparable.attempt = old.attempt
                comparable.deadline_unix_ms = old.deadline_unix_ms
                if comparable != old:
                    raise ConflictError("retry changes the original job")
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?)",
                (message.job_id, message.attempt, message.meta.task_id, payload),
            )
            for name in ("verification_plan", "verification_inputs"):
                if message.HasField(name):
                    self._artifact(
                        db,
                        message.meta.task_id,
                        getattr(message, name),
                        "correctness",
                        owner=f"{message.job_id}/{message.attempt}",
                    )
            self._record(db, message, payload)
        return True

    def _job(self, db, job_id, attempt):
        row = db.execute(
            "SELECT payload FROM jobs WHERE job_id = ? AND attempt = ?", (job_id, attempt)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"job: {job_id}/{attempt}")
        return decode_message(pb.VerificationJob, row[0])

    def list_jobs(self, task_id: str) -> list[pb.VerificationJob]:
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                decode_message(pb.VerificationJob, row[0])
                for row in db.execute(
                    "SELECT payload FROM jobs WHERE task_id = ? ORDER BY rowid", (task_id,)
                )
            ]

    def save_result(self, message: pb.VerificationResult) -> bool:
        payload = encode_message(message)
        with self._transaction() as db:
            if self._seen(db, message, payload):
                return False
            task = self._task(db, message.meta.task_id, running=True)
            if message.WhichOneof("subject") == "proposal_id":
                self._selection_event_guard(db, message.meta.task_id, message.proposal_id)
            evidence = None
            job = self._job(db, message.job_id, message.attempt)
            latest = db.execute(
                "SELECT MAX(attempt) FROM jobs WHERE job_id = ?", (message.job_id,)
            ).fetchone()[0]
            if message.attempt != latest:
                raise ConflictError("stale result attempt")
            validate_result_for_job(message, job)
            if job.HasField("verification_plan"):
                submission = self._submission(task)
                evidence = json.loads(self.artifacts.get(message.correctness_report))
                validate_evidence(
                    evidence,
                    message,
                    job,
                    submission,
                    json.loads(self.artifacts.get(job.verification_plan)),
                )
            if message.HasField("log"):
                self._artifact(
                    db,
                    message.meta.task_id,
                    message.log,
                    "log",
                    owner=f"{message.job_id}/{message.attempt}",
                )
            if message.HasField("correctness_report"):
                self._artifact(
                    db,
                    message.meta.task_id,
                    message.correctness_report,
                    "correctness",
                    owner=f"{message.job_id}/{message.attempt}",
                )
            if message.WhichOneof("outcome") == "success":
                metrics = self.artifacts.put(message.success.SerializeToString(deterministic=True))
                self._artifact(
                    db,
                    message.meta.task_id,
                    metrics,
                    "benchmark",
                    owner=f"{message.job_id}/{message.attempt}",
                )
            db.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?)",
                (message.job_id, message.attempt, message.meta.task_id, payload),
            )
            self._record(db, message, payload)
            if evidence is not None and evidence["baseline_invalid"]:
                db.execute(
                    "UPDATE tasks SET status = 'failed', revision = revision + 1 WHERE task_id = ?",
                    (message.meta.task_id,),
                )
                self._history(
                    db,
                    message.meta.task_id,
                    "failed",
                    task["revision"] + 1,
                    "Baseline invalidated by candidate verification",
                )
        return True

    def list_results(self, task_id: str) -> list[pb.VerificationResult]:
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                decode_message(pb.VerificationResult, row[0])
                for row in db.execute(
                    "SELECT payload FROM results WHERE task_id = ? ORDER BY rowid", (task_id,)
                )
            ]

    def attach_artifact(
        self, task_id: str, ref: pb.ArtifactRef, *, kind: str, job_id: str, attempt: int
    ) -> None:
        if kind not in {"binary", "log", "benchmark", "correctness"}:
            raise ValueError("artifact kind must be binary, log, benchmark, or correctness")
        with self._transaction() as db:
            self._task(db, task_id, running=True)
            if self._job(db, job_id, attempt).meta.task_id != task_id:
                raise ConflictError("artifact job belongs to another task")
            self._artifact(db, task_id, ref, kind, owner=f"{job_id}/{attempt}")

    def list_artifacts(self, task_id: str) -> list[dict]:
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT a.*, t.kind, t.owner_id FROM artifacts a "
                    "JOIN task_artifacts t ON t.key = a.key "
                    "WHERE t.task_id = ? ORDER BY t.kind, a.key, t.owner_id",
                    (task_id,),
                )
            ]

    def _verified_candidate(self, db, task_id, job_id, attempt):
        task = self._task(db, task_id, running=True)
        job = self._job(db, job_id, attempt)
        row = db.execute(
            "SELECT payload FROM results WHERE job_id = ? AND attempt = ? AND task_id = ?",
            (job_id, attempt, task_id),
        ).fetchone()
        require(row is not None, "missing candidate result")
        result = decode_message(pb.VerificationResult, row[0])
        require(
            job.meta.task_id == task_id
            and job.WhichOneof("subject") == "proposal_id"
            and result.WhichOneof("outcome") == "success"
            and job.HasField("verification_plan")
            and result.HasField("correctness_report"),
            "benchmark requires passed correctness evidence",
        )
        require(
            attempt
            == db.execute("SELECT MAX(attempt) FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[
                0
            ],
            "superseded verification attempt",
        )
        validate_result_for_job(result, job)
        report = json.loads(self.artifacts.get(result.correctness_report))
        self.artifacts.get(job.verification_inputs)
        validate_evidence(
            report,
            result,
            job,
            self._submission(task),
            json.loads(self.artifacts.get(job.verification_plan)),
        )
        proposal = self._load(db, "proposals", pb.Proposal, "proposal_id", result.proposal_id)
        reviews = self._pipeline_reviews(db, task_id, proposal, result)
        if any(review["verdict"] != "approve" for review in reviews.values()):
            raise StateError("candidate rejected by required agent review")
        if db.execute(
            "SELECT 1 FROM benchmark_runs WHERE task_id = ? AND source_sha256 = ? "
            "AND candidate_invalid = 1",
            (task_id, job.source.bundle.sha256),
        ).fetchone():
            raise StateError("candidate invalidated by benchmark execution")
        return job, result, report

    def get_verified_candidate(self, task_id, job_id, attempt=1):
        with self._transaction(write=False) as db:
            return self._verified_candidate(db, task_id, job_id, attempt)

    def _benchmark_evidence(self, db, report_ref):
        report = json.loads(self.artifacts.get(report_ref))
        require(report.get("schema_version") == 1, "invalid benchmark report")
        job, result, correctness = self._verified_candidate(
            db, report["task_id"], report["job_id"], report["attempt"]
        )
        require(
            report["correctness_sha256"] == result.correctness_report.sha256
            and report["compiler_image"] == job.compiler.image_digest
            and report["candidate_source_sha256"] == job.source.bundle.sha256,
            "benchmark report/job mismatch",
        )
        require(
            report.get("compiler")
            == {
                "executable": job.compiler.executable,
                "version": job.compiler.version,
                "flags": list(job.compiler.flags),
                "target": job.compiler.target,
                "language": job.compiler.language,
            },
            "benchmark compiler metadata mismatch",
        )
        expected = {
            role: next(
                check["binary_sha256"]
                for check in correctness["checks"]
                if check["phase"] == role + "_compile"
            )
            for role in ("baseline", "candidate")
        }
        require(report["binaries"] == expected, "benchmark must use verified binaries")
        for digest in expected.values():
            row = db.execute(
                "SELECT a.* FROM artifacts a JOIN task_artifacts t ON t.key=a.key "
                "WHERE t.task_id=? AND t.owner_id=? AND t.kind='binary' "
                "AND a.sha256=?",
                (job.meta.task_id, f"{job.job_id}/{job.attempt}", digest),
            ).fetchone()
            require(row is not None, "missing verified executable")
            self.artifacts.get(pb.ArtifactRef(**dict(row)))
        plan_ref, raw_ref = (pb.ArtifactRef(**report[key]) for key in ("plan", "raw"))
        require(raw_ref.size_bytes <= 64 * 1024**2, "benchmark evidence exceeds 64 MiB")
        plan = BenchmarkPlan.parse(self.artifacts.get(plan_ref))
        proposal = self._load(db, "proposals", pb.Proposal, "proposal_id", job.proposal_id)
        require(
            plan.objective == role_spec(proposal.role).objective,
            "benchmark objective does not match generator role",
        )
        self._pipeline_measurement_work(db, job.meta.task_id, proposal, job, result, plan_ref)
        raw = json.loads(self.artifacts.get(raw_ref))
        for environment in (raw.get("environment_before"), raw.get("environment_after")):
            if environment is not None:
                require(
                    environment.get("execution_target", "linux-x86_64") == job.compiler.target,
                    "benchmark environment target differs from verified compiler",
                )
        require(
            report["analysis"] == analyze(raw, plan, list(job.manifest.benchmark_test_ids)),
            "benchmark analysis differs from raw measurements",
        )
        require(
            isinstance(report["run_id"], str) and 0 < len(report["run_id"]) <= 64,
            "invalid benchmark run ID",
        )
        return report, raw, plan_ref, raw_ref

    def record_benchmark(self, report_ref):
        with self._transaction() as db:
            report = json.loads(self.artifacts.get(report_ref))
            existing = db.execute(
                "SELECT report_key FROM benchmark_runs WHERE run_id = ?", (report["run_id"],)
            ).fetchone()
            if existing:
                if existing[0] != report_ref.key:
                    raise ConflictError("benchmark run ID reused")
                return False
            job = self._job(db, report["job_id"], report["attempt"])
            require(
                job.compiler.target in ("linux-x86_64", "linux-aarch64"),
                "RISC-V emulator measurements cannot enter optimization selection",
            )
            self._assert_managed_execution(db, report["task_id"], job.proposal_id)
            report, raw, plan_ref, raw_ref = self._benchmark_evidence(db, report_ref)
            member = self._selection_member(
                db,
                report["task_id"],
                self._job(db, report["job_id"], report["attempt"]).proposal_id,
            )
            if (
                member
                and db.execute(
                    "SELECT 1 FROM benchmark_runs WHERE task_id=? AND job_id=?",
                    (report["task_id"], report["job_id"]),
                ).fetchone()
                and not self.managed_benchmark_retry_allowed(
                    db,
                    report["task_id"],
                    self._job(db, report["job_id"], report["attempt"]).proposal_id,
                    report["job_id"],
                )
            ):
                raise StateError("frozen selection permits one committed benchmark per candidate")
            self._commit_measurement_claim(db, job, report, raw)
            task_id = report["task_id"]
            for ref in (report_ref, plan_ref, raw_ref):
                self._artifact(db, task_id, ref, "benchmark", owner=report["run_id"])
            role = invalidated_role(raw)
            db.execute(
                "INSERT INTO benchmark_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report["run_id"],
                    task_id,
                    report["job_id"],
                    report["attempt"],
                    report["candidate_source_sha256"],
                    report_ref.key,
                    int(report["analysis"]["qualified"]),
                    int(role == "candidate"),
                    _now(),
                ),
            )
            if role == "baseline":
                task = self._task(db, task_id)
                db.execute(
                    "UPDATE tasks SET status='failed', revision=revision+1 WHERE task_id=?",
                    (task_id,),
                )
                self._history(
                    db,
                    task_id,
                    "failed",
                    task["revision"] + 1,
                    "Baseline invalidated by benchmark execution",
                )
        return True

    def list_benchmarks(self, task_id):
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM benchmark_runs WHERE task_id=? ORDER BY rowid", (task_id,)
                )
            ]

    def select_result(
        self,
        task_id: str,
        job_id: str,
        attempt: int,
        *,
        expected_revision: int,
        reason: str,
        scores: dict[str, float] | None = None,
    ) -> dict:
        if not reason.strip() or len(reason.encode()) > 4096:
            raise ValueError("decision reason must be 1–4096 UTF-8 bytes")
        scores = {} if scores is None else scores
        if (
            not isinstance(scores, dict)
            or len(scores) > 100
            or not all(
                isinstance(key, str)
                and 0 < len(key) <= 64
                and type(value) in (int, float)
                and math.isfinite(value)
                for key, value in scores.items()
            )
        ):
            raise ValueError("scores must map up to 100 short names to finite numbers")
        scores_json = json.dumps(scores, sort_keys=True, allow_nan=False)
        with self._transaction() as db:
            task = self._task(db, task_id)
            existing = db.execute(
                "SELECT * FROM decisions WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["job_id"],
                    existing["attempt"],
                    existing["reason"],
                    existing["scores_json"],
                ) != (job_id, attempt, reason, scores_json):
                    raise ConflictError("task already has a different final decision")
                return self._decision(existing)
            if task["status"] != "running":
                raise StateError("only running tasks can be completed")
            if task["revision"] != expected_revision:
                raise ConflictError("task revision changed")
            result_row = db.execute(
                "SELECT payload FROM results WHERE job_id = ? AND attempt = ? AND task_id = ?",
                (job_id, attempt, task_id),
            ).fetchone()
            if result_row is None:
                raise NotFoundError("selected result is missing")
            result = decode_message(pb.VerificationResult, result_row[0])
            if result.WhichOneof("outcome") != "success":
                raise StateError("cannot select an unverified result")
            latest = db.execute(
                "SELECT MAX(attempt) FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
            if attempt != latest:
                raise ConflictError("cannot select a superseded attempt")
            baseline_verified = any(
                value.WhichOneof("subject") == "baseline"
                and value.WhichOneof("outcome") == "success"
                for value in (
                    decode_message(pb.VerificationResult, row[0])
                    for row in db.execute(
                        "SELECT payload FROM results WHERE task_id = ?", (task_id,)
                    )
                )
            )
            if not baseline_verified:
                raise StateError("baseline must be verified before final selection")
            if result.WhichOneof("subject") == "proposal_id":
                job = self._job(db, job_id, attempt)
                proposal = self._load(
                    db, "proposals", pb.Proposal, "proposal_id", result.proposal_id
                )
                reviews = self._pipeline_reviews(db, task_id, proposal, result)
                if any(review["verdict"] != "approve" for review in reviews.values()):
                    raise StateError("candidate rejected by required agent review")
                if not job.HasField("verification_plan") or not result.HasField(
                    "correctness_report"
                ):
                    raise StateError("candidate needs bound correctness evidence before selection")
                validate_result_for_job(result, job)
                self.artifacts.get(job.verification_inputs)
                validate_evidence(
                    json.loads(self.artifacts.get(result.correctness_report)),
                    result,
                    job,
                    self._submission(task),
                    json.loads(self.artifacts.get(job.verification_plan)),
                )
                benchmark = db.execute(
                    "SELECT b.*, a.sha256, a.size_bytes FROM benchmark_runs b "
                    "JOIN artifacts a ON a.key=b.report_key WHERE b.task_id=? "
                    "AND b.job_id=? AND b.attempt=? ORDER BY b.rowid DESC LIMIT 1",
                    (task_id, job_id, attempt),
                ).fetchone()
                if benchmark is None or not benchmark["qualified"]:
                    raise StateError("candidate needs a reliable benchmark improvement")
                self._benchmark_evidence(
                    db,
                    pb.ArtifactRef(
                        key=benchmark["report_key"],
                        sha256=benchmark["sha256"],
                        size_bytes=benchmark["size_bytes"],
                    ),
                )
            self.artifacts.get(result.source.bundle)
            cancellation = db.execute(
                "SELECT 1 FROM cohort_runs WHERE task_id=? AND state IN ('cancelling','cancelled')",
                (task_id,),
            ).fetchone()
            if cancellation is not None:
                raise StateError("cohort cancellation prevents selection")
            self._guard_weighted_decision(db, task_id, job_id, attempt, reason, scores)
            db.execute(
                "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, job_id, attempt, reason, scores_json, _now()),
            )
            db.execute(
                "UPDATE tasks SET status = 'completed', revision = revision + 1 WHERE task_id = ?",
                (task_id,),
            )
            self._history(db, task_id, "completed", task["revision"] + 1, reason)
            return self._decision(
                db.execute("SELECT * FROM decisions WHERE task_id = ?", (task_id,)).fetchone()
            )

    @staticmethod
    def _decision(row):
        value = dict(row)
        value["scores"] = json.loads(value.pop("scores_json"))
        return value

    def get_decision(self, task_id: str) -> dict | None:
        with self._transaction(write=False) as db:
            self._task(db, task_id)
            row = db.execute("SELECT * FROM decisions WHERE task_id = ?", (task_id,)).fetchone()
            return None if row is None else self._decision(row)
