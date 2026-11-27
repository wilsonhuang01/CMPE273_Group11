-- Schema version 13: database-backed dispatch claims (action R, phase 3).
CREATE TABLE dispatch_claims (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    owner_id TEXT NOT NULL,
    token TEXT NOT NULL,
    fence BIGINT NOT NULL,
    claimed_ms BIGINT NOT NULL,
    expires_ms BIGINT NOT NULL,
    rowid BIGINT GENERATED ALWAYS AS IDENTITY
);
CREATE INDEX dispatch_claims_live ON dispatch_claims(expires_ms)
