CREATE TABLE benchmark_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL,
    report_key TEXT NOT NULL REFERENCES artifacts(key),
    qualified INTEGER NOT NULL CHECK(qualified IN (0, 1)),
    candidate_invalid INTEGER NOT NULL CHECK(candidate_invalid IN (0, 1)),
    recorded_at_unix_ms INTEGER NOT NULL,
    FOREIGN KEY(job_id, attempt) REFERENCES results(job_id, attempt)
);
CREATE INDEX benchmark_task ON benchmark_runs(task_id, job_id, attempt);
