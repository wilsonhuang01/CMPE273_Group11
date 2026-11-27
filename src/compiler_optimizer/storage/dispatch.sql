CREATE TABLE dispatch_claims (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    owner_id TEXT NOT NULL,
    token TEXT NOT NULL,
    fence INTEGER NOT NULL,
    claimed_ms INTEGER NOT NULL,
    expires_ms INTEGER NOT NULL
);
CREATE INDEX dispatch_claims_live ON dispatch_claims(expires_ms)
