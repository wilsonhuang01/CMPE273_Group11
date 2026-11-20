CREATE TABLE selection_sessions (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    policy_key TEXT NOT NULL REFERENCES artifacts(key),
    cohort_json TEXT NOT NULL,
    outcome_key TEXT REFERENCES artifacts(key)
);
CREATE TABLE selection_progress (
    task_id TEXT NOT NULL REFERENCES selection_sessions(task_id),
    proposal_id TEXT NOT NULL REFERENCES requests(proposal_id),
    status TEXT NOT NULL CHECK(status IN ('evaluated', 'generation_failed', 'assessment_failed')),
    evidence_key TEXT NOT NULL REFERENCES artifacts(key),
    PRIMARY KEY(task_id, proposal_id)
);
