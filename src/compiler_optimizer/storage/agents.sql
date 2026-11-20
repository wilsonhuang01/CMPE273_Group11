CREATE TABLE agent_iterations (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    role INTEGER NOT NULL,
    iteration INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    input_key TEXT NOT NULL REFERENCES artifacts(key),
    output_key TEXT REFERENCES artifacts(key),
    status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
    PRIMARY KEY(task_id, role, iteration)
);
CREATE INDEX agent_operation ON agent_iterations(task_id, role, operation_id);
