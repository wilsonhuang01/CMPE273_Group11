CREATE TABLE fleet_workers (
    worker_id TEXT PRIMARY KEY,
    incarnation_id TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0,
    last_seen_ms INTEGER NOT NULL,
    active_job TEXT
);
CREATE TABLE fleet_jobs (
    job_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    worker_id TEXT NOT NULL,
    incarnation_id TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    parent_token TEXT,
    input_key TEXT NOT NULL REFERENCES artifacts(key),
    operation TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    fence INTEGER NOT NULL DEFAULT 1,
    token TEXT NOT NULL,
    expires_ms INTEGER,
    deadline_ms INTEGER NOT NULL,
    published_ms INTEGER NOT NULL DEFAULT 0,
    result_key TEXT REFERENCES artifacts(key),
    error TEXT
);
CREATE INDEX fleet_pending ON fleet_jobs(state, deadline_ms);
