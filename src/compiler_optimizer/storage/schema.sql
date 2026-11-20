CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    submission BLOB NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted', 'running', 'completed', 'failed')),
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE task_history (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    reason TEXT NOT NULL,
    recorded_at_unix_ms INTEGER NOT NULL,
    UNIQUE(task_id, revision)
);
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    kind TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE requests (
    proposal_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    payload BLOB NOT NULL
);
CREATE TABLE proposals (
    proposal_id TEXT PRIMARY KEY REFERENCES requests(proposal_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    payload BLOB NOT NULL
);
CREATE TABLE jobs (
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    payload BLOB NOT NULL,
    PRIMARY KEY(job_id, attempt)
);
CREATE TABLE results (
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    payload BLOB NOT NULL,
    PRIMARY KEY(job_id, attempt),
    FOREIGN KEY(job_id, attempt) REFERENCES jobs(job_id, attempt)
);
CREATE TABLE artifacts (
    key TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL
);
CREATE TABLE task_artifacts (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    key TEXT NOT NULL REFERENCES artifacts(key),
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    PRIMARY KEY(task_id, key, kind, owner_id)
);
CREATE TABLE decisions (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    reason TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    recorded_at_unix_ms INTEGER NOT NULL,
    FOREIGN KEY(job_id, attempt) REFERENCES results(job_id, attempt)
);
CREATE INDEX requests_task ON requests(task_id);
CREATE INDEX proposals_task ON proposals(task_id);
CREATE INDEX jobs_task ON jobs(task_id);
CREATE INDEX results_task ON results(task_id);
