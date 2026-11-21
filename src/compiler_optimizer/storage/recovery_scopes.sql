CREATE TEMP TABLE backup_recovery_jobs AS SELECT * FROM recovery_jobs;
CREATE TEMP TABLE backup_recovery_attempts AS SELECT * FROM recovery_attempts;
CREATE TEMP TABLE backup_recovery_outbox AS SELECT * FROM recovery_outbox;
DROP TABLE recovery_attempts;
DROP TABLE recovery_outbox;
DROP TABLE recovery_jobs;
CREATE TABLE recovery_jobs (
    scope TEXT NOT NULL DEFAULT 'evaluate',
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
    PRIMARY KEY(task_id, proposal_id, scope)
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

CREATE TABLE recovery_outbox (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES recovery_jobs(job_id),
    kind TEXT NOT NULL CHECK(kind IN ('retry', 'deadletter', 'blacklist')),
    payload_json TEXT NOT NULL,
    sent_ms INTEGER
);

INSERT INTO recovery_jobs (task_id,proposal_id,job_id,policy_key,base_context_json,execution_context_json,state,attempt,fence,worker_id,incarnation_id,lease_token,lease_expires_ms,deadline_ms,available_at_ms,mode,crash_json,cleanup_token,outcome_key) SELECT task_id,proposal_id,job_id,policy_key,base_context_json,execution_context_json,state,attempt,fence,worker_id,incarnation_id,lease_token,lease_expires_ms,deadline_ms,available_at_ms,mode,crash_json,cleanup_token,outcome_key FROM backup_recovery_jobs;
DROP TABLE backup_recovery_jobs;
INSERT INTO recovery_attempts (job_id,attempt,worker_id,lease_token,fence,started_ms,finished_ms,status,evidence_key) SELECT job_id,attempt,worker_id,lease_token,fence,started_ms,finished_ms,status,evidence_key FROM backup_recovery_attempts;
DROP TABLE backup_recovery_attempts;
INSERT INTO recovery_outbox (event_id,job_id,kind,payload_json,sent_ms) SELECT event_id,job_id,kind,payload_json,sent_ms FROM backup_recovery_outbox;
DROP TABLE backup_recovery_outbox;
