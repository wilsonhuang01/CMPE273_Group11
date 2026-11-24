CREATE TABLE debate_runs (
    task_id TEXT PRIMARY KEY REFERENCES selection_sessions(task_id),
    policy_key TEXT NOT NULL REFERENCES artifacts(key),
    started_ms INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    final_proposal_id TEXT,
    final_state TEXT,
    final_report_key TEXT REFERENCES artifacts(key)
);
CREATE TABLE debate_slots (
    task_id TEXT NOT NULL REFERENCES debate_runs(task_id),
    proposal_id TEXT PRIMARY KEY REFERENCES requests(proposal_id),
    round_number INTEGER NOT NULL,
    parent_id TEXT,
    context_key TEXT REFERENCES artifacts(key),
    reason TEXT
);
CREATE TABLE debate_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES debate_runs(task_id),
    proposal_id TEXT,
    event TEXT NOT NULL,
    record_key TEXT NOT NULL REFERENCES artifacts(key),
    UNIQUE(task_id, proposal_id, event)
);
