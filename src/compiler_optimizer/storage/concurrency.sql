CREATE TABLE cohort_runs (
    task_id TEXT PRIMARY KEY REFERENCES selection_sessions(task_id),
    workers INTEGER NOT NULL,
    reservation_mib INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'preparing'
        CHECK(state IN ('preparing','measuring','completed','cancelling','cancelled'))
);
CREATE TABLE cohort_work (
    task_id TEXT NOT NULL REFERENCES cohort_runs(task_id),
    proposal_id TEXT NOT NULL REFERENCES requests(proposal_id),
    state TEXT NOT NULL CHECK(state IN ('pending','preparing','ready','completed','cancelled')),
    PRIMARY KEY(task_id, proposal_id)
);
CREATE TABLE measurement_windows (
    token TEXT PRIMARY KEY,
    engine_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    parent_token TEXT,
    state TEXT NOT NULL CHECK(state IN ('waiting','active','draining','released'))
);
ALTER TABLE fleet_jobs ADD COLUMN window_token TEXT;
ALTER TABLE fleet_jobs ADD COLUMN reservation_mib INTEGER NOT NULL DEFAULT 128;
CREATE TABLE measurement_claims (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    plan_key TEXT NOT NULL REFERENCES artifacts(key),
    lease_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL CHECK(state IN ('running','retryable','completed')),
    PRIMARY KEY(task_id,proposal_id)
);
