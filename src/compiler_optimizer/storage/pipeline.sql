CREATE TABLE pipeline_runs (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(proposal_id),
    configuration_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('queued', 'published', 'verifying', 'benchmarking', 'completed')),
    published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0, 1)),
    report_key TEXT REFERENCES artifacts(key)
);
