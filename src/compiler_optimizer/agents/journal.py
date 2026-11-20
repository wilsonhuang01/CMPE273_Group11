"""Bounded, durable agent calls; completed work is reused after an owner restart."""

import json


class AgentJournal:
    def __init__(self, repository, role):
        self.repository, self.role = repository, role

    def run(self, task_id, operation, inputs, action):
        repository = self.repository
        input_ref = repository.artifacts.put(
            json.dumps(inputs, sort_keys=True, allow_nan=False).encode()
        )
        iteration, cached = repository.begin_agent_iteration(
            task_id, self.role, operation, input_ref
        )
        if cached is not None:
            return json.loads(repository.artifacts.get(cached))
        try:
            output = action()
            ref = repository.artifacts.put(
                json.dumps(output, sort_keys=True, allow_nan=False).encode()
            )
            repository.end_agent_iteration(task_id, self.role, iteration, ref)
            return output
        except BaseException:
            repository.fail_agent_iteration(task_id, self.role, iteration)
            raise
