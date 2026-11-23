CREATE TABLE agent_rpc_calls (
    call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    role INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    receipt_key TEXT NOT NULL REFERENCES artifacts(key)
);
