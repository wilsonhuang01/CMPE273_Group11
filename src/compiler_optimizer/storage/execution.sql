CREATE TABLE submission_executions (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    configuration_json TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at_ms INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    finished INTEGER NOT NULL DEFAULT 0 CHECK(finished IN (0, 1))
);
