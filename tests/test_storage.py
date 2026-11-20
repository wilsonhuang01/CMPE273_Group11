"""Persistence, integrity, rollback, deduplication, and concurrent-writer checks."""

import hashlib
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from compiler_optimizer.shared.contracts import BASE_FLAGS
from compiler_optimizer.shared.manifest import ContractError, parse_manifest
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import ArtifactStore, Repository
from compiler_optimizer.storage.errors import (
    ArtifactError,
    ConflictError,
    NotFoundError,
    StateError,
)


def meta(task_id):
    return pb.Metadata(
        schema_version=1,
        message_id=str(uuid4()),
        task_id=task_id,
        created_at_unix_ms=1_800_000_000_000,
    )


def result_for(job, *, failure=False):
    result = pb.VerificationResult(
        meta=meta(job.meta.task_id),
        job_id=job.job_id,
        worker_id=str(uuid4()),
        attempt=job.attempt,
        source=job.source,
        compiler=job.compiler,
        hardware="test CPU",
    )
    if job.WhichOneof("subject") == "baseline":
        result.baseline = True
    else:
        result.proposal_id = job.proposal_id
    if failure:
        result.failure.CopyFrom(
            pb.FailureDetails(
                reason=pb.OUTPUT_MISMATCH,
                stage=pb.TEST,
                test_id="mixed",
                message="incorrect stdout",
            )
        )
    else:
        result.success.CopyFrom(
            pb.VerificationSuccess(
                passed_test_ids=["empty", "mixed"],
                benchmarks=[
                    pb.BenchmarkMeasurement(
                        test_id="mixed", wall_seconds=[0.01, 0.012], peak_memory_bytes=4096
                    )
                ],
            )
        )
    return result


@pytest.fixture
def scenario(tmp_path):
    repo = Repository(tmp_path / "state")
    task_id, proposal_id = str(uuid4()), str(uuid4())
    compiler = pb.CompilerSettings(
        executable="clang",
        version="18.1.8",
        image_digest="sha256:" + "a" * 64,
        language="c17",
        target="linux-x86_64",
        flags=["-std=c17", *BASE_FLAGS],
    )
    manifest_path = Path(__file__).resolve().parents[1] / "examples/sum-array/submission.json"
    submission = pb.Submission(
        meta=meta(task_id),
        manifest=parse_manifest(manifest_path.read_bytes()),
        package=repo.artifacts.put(b"test package bytes"),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(b"baseline source bytes")),
        compiler=compiler,
    )
    request = pb.OptimizationRequest(
        meta=meta(task_id),
        proposal_id=proposal_id,
        role=pb.PERFORMANCE,
        source=submission.source,
        manifest=submission.manifest,
        compiler=compiler,
    )
    proposal = pb.Proposal(
        meta=meta(task_id),
        proposal_id=proposal_id,
        role=pb.PERFORMANCE,
        source=pb.SourceVersion(
            version=2,
            bundle=repo.artifacts.put(b"candidate source bytes"),
            parent_source_sha256=submission.source.bundle.sha256,
        ),
        rationale="Reduce repeated loads",
        transformation="loop-unroll",
        compiler=compiler,
    )
    baseline_job = pb.VerificationJob(
        meta=meta(task_id),
        job_id=str(uuid4()),
        baseline=True,
        source=submission.source,
        manifest=submission.manifest,
        compiler=compiler,
        attempt=1,
        deadline_unix_ms=1_800_000_060_000,
    )
    candidate_job = pb.VerificationJob(
        meta=meta(task_id),
        job_id=str(uuid4()),
        proposal_id=proposal_id,
        source=proposal.source,
        manifest=submission.manifest,
        compiler=compiler,
        attempt=1,
        deadline_unix_ms=1_800_000_060_000,
    )
    return repo, submission, request, proposal, baseline_job, candidate_job


def start(scenario, *, proposals=True):
    repo, submission, request, proposal, _, _ = scenario
    repo.save_submission(submission)
    repo.transition(submission.meta.task_id, expected_revision=0, status="running", reason="Start")
    if proposals:
        repo.save_request(request)
        repo.save_proposal(proposal)


def test_artifact_round_trip_deduplicates_and_accepts_empty_logs(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put(b"hello")
    assert ref.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert store.put(b"hello") == ref
    assert store.get(ref) == b"hello"
    assert store.get(store.put(b"")) == b""
    assert not list(tmp_path.glob(".upload-*"))


@pytest.mark.parametrize("corruption", [b"HELLO", b"shorter or longer"])
def test_artifact_corruption_is_detected_and_never_overwritten(tmp_path, corruption):
    store = ArtifactStore(tmp_path)
    ref = store.put(b"hello")
    (tmp_path / ref.key).write_bytes(corruption)
    with pytest.raises(ArtifactError):
        store.get(ref)
    with pytest.raises(ArtifactError):
        store.put(b"hello")
    assert (tmp_path / ref.key).read_bytes() == corruption


def test_artifact_size_limit_and_cleanup(tmp_path):
    store = ArtifactStore(tmp_path, max_bytes=4)
    with pytest.raises(ArtifactError, match="size limit"):
        store.put(b"12345")
    assert list(tmp_path.iterdir()) == []
    assert store.get(store.put(b"1234")) == b"1234"


def test_artifact_missing_traversal_wrong_size_and_symlink(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    ref = store.put(b"hello")
    bad = pb.ArtifactRef()
    bad.CopyFrom(ref)
    bad.key = "../outside"
    with pytest.raises(ArtifactError):
        store.get(bad)
    bad.CopyFrom(ref)
    bad.size_bytes += 1
    with pytest.raises(ArtifactError):
        store.get(bad)
    path = store.root / ref.key
    path.unlink()
    with pytest.raises(NotFoundError):
        store.get(ref)
    outside = tmp_path / "outside"
    outside.write_bytes(b"hello")
    path.symlink_to(outside)
    with pytest.raises(ArtifactError):
        store.get(ref)
    with pytest.raises(ArtifactError):
        store.put(b"hello")
    assert outside.read_bytes() == b"hello"


def test_artifact_symlink_directory_is_rejected(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    external = tmp_path / "external"
    external.mkdir()
    (store.root / "sha256").symlink_to(external, target_is_directory=True)
    with pytest.raises(ArtifactError):
        store.put(b"hello")
    assert list(external.iterdir()) == []


def test_concurrent_blob_publication(tmp_path):
    store = ArtifactStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(store.put, [b"same immutable blob"] * 12))
    assert all(ref == refs[0] for ref in refs)
    assert store.get(refs[0]) == b"same immutable blob"


def test_full_history_artifacts_decision_and_reopen(scenario):
    repo, submission, _, proposal, baseline, candidate = scenario
    start(scenario)
    for job in (baseline, candidate):
        assert repo.save_job(job)
        measured = result_for(job)
        measured.log.CopyFrom(repo.artifacts.put(b"compiler/test logs"))
        assert repo.save_result(measured)
    binary = repo.artifacts.put(b"test executable bytes")
    repo.attach_artifact(
        submission.meta.task_id,
        binary,
        kind="binary",
        job_id=candidate.job_id,
        attempt=candidate.attempt,
    )
    chosen = repo.select_result(
        submission.meta.task_id,
        baseline.job_id,
        1,
        expected_revision=1,
        reason="Retain baseline while candidate lacks correctness evidence",
        scores={"performance": 1.0},
    )
    reopened = Repository(repo.root)
    task = reopened.get_task(submission.meta.task_id)
    assert task["status"] == "completed" and task["revision"] == 2
    assert task["submission"] == submission
    assert reopened.list_proposals(submission.meta.task_id) == [proposal]
    assert len(reopened.list_results(submission.meta.task_id)) == 2
    assert reopened.get_decision(submission.meta.task_id) == chosen
    assert (
        reopened.select_result(
            submission.meta.task_id,
            baseline.job_id,
            1,
            expected_revision=1,
            reason=chosen["reason"],
            scores={"performance": 1.0},
        )
        == chosen
    )
    assert [row["status"] for row in reopened.history(submission.meta.task_id)] == [
        "accepted",
        "running",
        "completed",
    ]
    assert {row["kind"] for row in reopened.list_artifacts(submission.meta.task_id)} == {
        "package",
        "source",
        "log",
        "binary",
        "benchmark",
    }
    binary_rows = [
        row for row in reopened.list_artifacts(submission.meta.task_id) if row["kind"] == "binary"
    ]
    assert binary_rows[0]["owner_id"] == f"{candidate.job_id}/{candidate.attempt}"
    for row in reopened.list_artifacts(submission.meta.task_id):
        reopened.artifacts.get(
            pb.ArtifactRef(key=row["key"], sha256=row["sha256"], size_bytes=row["size_bytes"])
        )


def test_retain_original_after_failed_candidate(scenario):
    repo, submission, _, _, baseline, candidate = scenario
    start(scenario)
    repo.save_job(baseline)
    repo.save_result(result_for(baseline))
    repo.save_job(candidate)
    repo.save_result(result_for(candidate, failure=True))
    with pytest.raises(StateError, match="unverified"):
        repo.select_result(
            submission.meta.task_id,
            candidate.job_id,
            1,
            expected_revision=1,
            reason="Cannot select",
        )
    assert repo.get_decision(submission.meta.task_id) is None
    decision = repo.select_result(
        submission.meta.task_id, baseline.job_id, 1, expected_revision=1, reason="Retain original"
    )
    assert decision["job_id"] == baseline.job_id
    with pytest.raises(ConflictError):
        repo.select_result(
            submission.meta.task_id,
            candidate.job_id,
            1,
            expected_revision=2,
            reason="Change final choice",
        )


def test_selection_requires_baseline_and_matching_task(scenario):
    repo, submission, _, _, _, candidate = scenario
    start(scenario)
    repo.save_job(candidate)
    repo.save_result(result_for(candidate))
    with pytest.raises(StateError, match="baseline"):
        repo.select_result(
            submission.meta.task_id, candidate.job_id, 1, expected_revision=1, reason="Premature"
        )
    assert repo.get_task(submission.meta.task_id)["status"] == "running"


def test_message_redelivery_and_identity_conflicts(scenario):
    repo, submission, request, proposal, baseline, _ = scenario
    assert repo.save_submission(submission)
    assert not repo.save_submission(submission)
    repo.transition(submission.meta.task_id, expected_revision=0, status="running", reason="Start")
    for save, value in [
        (repo.save_request, request),
        (repo.save_proposal, proposal),
        (repo.save_job, baseline),
        (repo.save_result, result_for(baseline)),
    ]:
        assert save(value)
        assert not save(value)
    proposal.rationale = "Different payload under the same identity"
    with pytest.raises(ConflictError, match="message_id"):
        repo.save_proposal(proposal)
    submission.meta.message_id = str(uuid4())
    with pytest.raises(ConflictError):
        repo.save_submission(submission)


def test_failed_transaction_leaves_no_task_or_inbox_entry(scenario):
    repo, submission, *_ = scenario
    blob_path = repo.artifacts.root / submission.package.key
    contents = blob_path.read_bytes()
    blob_path.unlink()
    with pytest.raises(NotFoundError):
        repo.save_submission(submission)
    with pytest.raises(NotFoundError):
        repo.get_task(submission.meta.task_id)
    assert repo.artifacts.put(contents) == submission.package
    assert repo.save_submission(submission)
    assert len(repo.history(submission.meta.task_id)) == 1


def test_invalid_parent_and_changed_job_inputs(scenario):
    repo, _, request, _, baseline, _ = scenario
    start(scenario, proposals=False)
    request.source.bundle.CopyFrom(repo.artifacts.put(b"unknown source"))
    with pytest.raises(ConflictError, match="parent"):
        repo.save_request(request)
    baseline.manifest.tests[0].args.append("changed-test")
    with pytest.raises(ConflictError, match="tests"):
        repo.save_job(baseline)


def test_retry_history_rejects_stale_results_and_successful_retry(scenario):
    repo, submission, _, _, baseline, _ = scenario
    start(scenario)
    repo.save_job(baseline)
    stale = result_for(baseline)
    baseline.meta.message_id = str(uuid4())
    baseline.attempt = 2
    baseline.deadline_unix_ms += 60_000
    repo.save_job(baseline)
    with pytest.raises(ConflictError, match="stale"):
        repo.save_result(stale)
    current = result_for(baseline)
    repo.save_result(current)
    baseline.meta.message_id = str(uuid4())
    baseline.attempt = 3
    with pytest.raises(StateError, match="successful"):
        repo.save_job(baseline)
    assert len(repo.list_results(submission.meta.task_id)) == 1
    assert [job.attempt for job in repo.list_jobs(submission.meta.task_id)] == [1, 2]


def test_retry_cannot_change_subject_or_skip_attempts(scenario):
    repo, _, _, _, baseline, candidate = scenario
    start(scenario)
    repo.save_job(baseline)
    candidate.job_id = baseline.job_id
    candidate.attempt = 2
    with pytest.raises(ConflictError, match="original job"):
        repo.save_job(candidate)
    baseline.meta.message_id = str(uuid4())
    baseline.attempt = 3
    with pytest.raises(ConflictError, match="increase by one"):
        repo.save_job(baseline)


def test_concurrent_message_acceptance_and_revision_guard(scenario):
    repo, submission, *_ = scenario
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(repo.save_submission, [submission, submission])) == [False, True]

    def run(_):
        try:
            repo.transition(
                submission.meta.task_id,
                expected_revision=0,
                status="running",
                reason="Concurrent start",
            )
            return "ok"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == ["conflict", "ok"]
    assert len(repo.history(submission.meta.task_id)) == 2


def test_failed_task_is_terminal_and_results_are_defensive_copies(scenario):
    repo, submission, request, *_ = scenario
    repo.save_submission(submission)
    copy = repo.get_task(submission.meta.task_id)
    copy["submission"].manifest.name = "mutated"
    assert repo.get_task(submission.meta.task_id)["submission"] == submission
    repo.transition(
        submission.meta.task_id, expected_revision=0, status="failed", reason="Invalid input"
    )
    with pytest.raises(StateError):
        repo.save_request(request)
    with pytest.raises(StateError):
        repo.transition(
            submission.meta.task_id, expected_revision=1, status="running", reason="Restart"
        )


def test_process_restart_preserves_committed_state(scenario):
    repo, submission, *_ = scenario
    repo.save_submission(submission)
    code = """
import os, sys
from compiler_optimizer.storage import Repository
Repository(sys.argv[1]).transition(
    sys.argv[2], expected_revision=0, status="running", reason="Child process")
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", code, str(repo.root), submission.meta.task_id], check=True
    )
    assert Repository(repo.root).get_task(submission.meta.task_id)["status"] == "running"


def test_unknown_schema_is_not_overwritten(scenario):
    repo, submission, *_ = scenario
    repo.save_submission(submission)
    with sqlite3.connect(repo.database) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(StateError, match="schema version"):
        Repository(repo.root)
    with sqlite3.connect(repo.database) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_contract_validation_still_applies_before_storage(scenario):
    repo, submission, *_ = scenario
    submission.meta.schema_version = 2
    with pytest.raises(ContractError):
        repo.save_submission(submission)


def test_restart_discovery_and_attachment_requires_recorded_job(scenario):
    repo, submission, *_ = scenario
    assert repo.list_tasks() == []
    repo.save_submission(submission)
    assert repo.list_tasks(status="accepted")[0]["submission"] == submission
    assert repo.list_tasks(status="running") == []
    repo.transition(submission.meta.task_id, expected_revision=0, status="running", reason="Start")
    with pytest.raises(NotFoundError):
        repo.attach_artifact(
            submission.meta.task_id,
            repo.artifacts.put(b"binary"),
            kind="binary",
            job_id=str(uuid4()),
            attempt=1,
        )
    assert not any(row["kind"] == "binary" for row in repo.list_artifacts(submission.meta.task_id))
    assert Repository(repo.root).list_tasks(status="running")[0]["revision"] == 1


def test_job_identity_cannot_cross_tasks(scenario):
    repo, submission, _, _, baseline, _ = scenario
    start(scenario)
    repo.save_job(baseline)
    other = pb.Submission()
    other.CopyFrom(submission)
    other.meta.CopyFrom(meta(str(uuid4())))
    repo.save_submission(other)
    repo.transition(other.meta.task_id, expected_revision=0, status="running", reason="Start other")
    baseline.meta.CopyFrom(meta(other.meta.task_id))
    baseline.attempt = 2
    with pytest.raises(ConflictError, match="another task"):
        repo.save_job(baseline)


def test_legacy_candidate_result_cannot_skip_correctness_selection_gate(scenario):
    repo, submission, _, _, baseline, candidate = scenario
    start(scenario)
    for job in (baseline, candidate):
        repo.save_job(job)
        repo.save_result(result_for(job))
    with pytest.raises(StateError, match="correctness evidence"):
        repo.select_result(
            submission.meta.task_id,
            candidate.job_id,
            1,
            expected_revision=1,
            reason="Attempt to skip verification",
        )
    assert repo.get_decision(submission.meta.task_id) is None
