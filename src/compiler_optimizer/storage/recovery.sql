CREATE TABLE recovery_workers (
    worker_id TEXT PRIMARY KEY,
    incarnation_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    sequence INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK(state IN ('idle', 'busy', 'quarantined')),
    last_seen_ms INTEGER NOT NULL,
    quarantine_until_ms INTEGER NOT NULL DEFAULT 0,
    current_job_id TEXT,
    heartbeat BLOB
);
CREATE TABLE recovery_jobs (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    job_id TEXT NOT NULL UNIQUE,
    policy_key TEXT NOT NULL REFERENCES artifacts(key),
    base_context_json TEXT NOT NULL,
    execution_context_json TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending', 'leased', 'backoff', 'completed', 'quarantined', 'exhausted')),
    attempt INTEGER NOT NULL DEFAULT 0,
    fence INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT REFERENCES recovery_workers(worker_id),
    incarnation_id TEXT,
    lease_token TEXT,
    lease_expires_ms INTEGER,
    deadline_ms INTEGER,
    available_at_ms INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'evaluate',
    crash_json TEXT,
    cleanup_token TEXT,
    outcome_key TEXT REFERENCES artifacts(key),
    PRIMARY KEY(task_id, proposal_id)
);
CREATE TABLE recovery_attempts (
    job_id TEXT NOT NULL REFERENCES recovery_jobs(job_id),
    attempt INTEGER NOT NULL,
    worker_id TEXT,
    lease_token TEXT,
    fence INTEGER NOT NULL,
    started_ms INTEGER NOT NULL,
    finished_ms INTEGER,
    status TEXT NOT NULL,
    evidence_key TEXT REFERENCES artifacts(key),
    PRIMARY KEY(job_id, attempt)
);
CREATE TABLE recovery_blacklist (
    key TEXT PRIMARY KEY,
    record_key TEXT NOT NULL REFERENCES artifacts(key),
    expires_ms INTEGER NOT NULL
);
CREATE TABLE recovery_outbox (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES recovery_jobs(job_id),
    kind TEXT NOT NULL CHECK(kind IN ('retry', 'deadletter', 'blacklist')),
    payload_json TEXT NOT NULL,
    sent_ms INTEGER
);
