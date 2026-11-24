# Model-backed agents (Action G)

Performance, Memory and Architecture generators and the Correctness and Critic
reviewers can call a hosted model instead of the deterministic starter rules.
The Benchmark agent always stays deterministic: measurement work is frozen policy.
The default is still `MODEL_PROVIDER=none`, so tests, CI and the demo never need
credentials or network access.

The model only supplies **data**: exact-text edits in the
[structured patch format](performance-pipeline.md) or a review. Nothing it returns
is executed, and it cannot bypass any gate:

- The service binds the patch to the assigned parent hash and applies it with the
  same `shared.patches` rules (existing files only, unique context, ≤32 edits).
  The coordinator then revalidates the patch, provenance and hashes as before.
- Correctness never consults the model when automated verification failed; the
  candidate is rejected. A model reply that tries to approve with blocking findings
  is rejected by `validate_review`.
- The Critic's deterministic source-risk scan runs first. New `system()`, `asm`,
  file/network calls and similar are rejected without a model call; its warnings
  (e.g. unsupported "guaranteed" claims) are kept beside the model's findings.
- Submitted source and evidence are wrapped in `BEGIN/END FILE`/`EVIDENCE` markers
  and the system prompt treats them as untrusted data.

## Providers

| `MODEL_PROVIDER` | Behavior |
| --- | --- |
| `none` (default) | Deterministic rules and replayed patches, exactly as in Action F. |
| `bedrock` | Amazon Bedrock Converse API through the standard AWS credential chain. |
| `replay` | Recorded replies from `MODEL_REPLAY_DIRECTORY`, keyed by prompt hash; no network. |

Providers implement one small interface in
[`agents/model.py`](../src/compiler_optimizer/agents/model.py)
(`complete(model_id, system, messages, ...)` and `check()`), so another hosted or
local model can be added without touching the agents or the RPC services.

## Set up Amazon Bedrock

1. Install the extra on the host (the Docker image includes it by default):

   ```sh
   python -m pip install -e '.[bedrock]'
   ```

2. Give each teammate their own AWS identity. Do not share or commit access keys.
   The minimum IAM policy is:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["bedrock:InvokeModel", "bedrock:Converse"],
       "Resource": "*"
     }]
   }
   ```

   Set an AWS Budgets alert on the account. First use of some model families
   (e.g. Anthropic) requires the use-case form under *Bedrock → Model access*.

3. Choose a model and region. Many models need an inference-profile ID with a
   region prefix (`us.`), not the bare model ID:

   ```sh
   export MODEL_PROVIDER=bedrock
   export MODEL_ID=us.amazon.nova-micro-v1:0
   export MODEL_REGION=us-east-1
   export AWS_PROFILE=your-profile
   python -m compiler_optimizer.agents.model check
   ```

   `check` makes one tiny call and prints the backend identity, latency and
   token usage. It exits with status 2 and the reason when the model is unavailable.

## Run with Docker Compose

Agent containers do not read `~/.aws`. Export short-lived credentials from your
profile into the shell (this works for SSO and IAM users) and Compose passes them
to the five model-capable agents only:

```sh
eval "$(aws configure export-credentials --profile your-profile --format env)"
export MODEL_PROVIDER=bedrock MODEL_ID=us.amazon.nova-micro-v1:0 MODEL_REGION=us-east-1
export AGENT_TIMEOUT_SECONDS=90   # model calls need more than the 15 s default
./project up
```

A model-enabled agent reports **not ready** on its health endpoint when it cannot
resolve credentials or `boto3` is missing, so the stack does not start silently in
a mode it cannot serve. When credentials expire mid-run, calls fail with
`UNAVAILABLE` and consume one bounded agent attempt; nothing is committed.

On ECS (action T) the task role supplies credentials instead; no keys are needed.

## Configuration

| Variable | Default | Range / meaning |
| --- | --- | --- |
| `MODEL_PROVIDER` | `none` | `none`, `bedrock`, `replay` |
| `MODEL_ID` | — | Required when a provider is set |
| `MODEL_REGION` | `AWS_REGION` | Required for Bedrock |
| `MODEL_TIMEOUT_SECONDS` | 60 | 1–115; also capped by the RPC deadline |
| `MODEL_MAX_RETRIES` | 2 | 0–5 retries for throttling/transient faults only |
| `MODEL_MAX_REPAIRS` | 1 | 0–3 follow-ups that explain a rejected reply to the model |
| `MODEL_MAX_OUTPUT_TOKENS` | 4096 | 256–16384; truncated replies are rejected |
| `MODEL_TEMPERATURE` | 0 | 0–1 |
| `MODEL_MAX_CALLS_PER_TASK` | 12 | 1–100 calls per task, per agent service |
| `MODEL_MAX_TOKENS_PER_TASK` | 200000 | 1000–5,000,000 input+output tokens |
| `MODEL_REPLAY_DIRECTORY` | — | Required for `replay` |
| `MODEL_RECORD_DIRECTORY` | — | Write each successful reply as a replay fixture |

The coordinator's `AGENT_TIMEOUT_SECONDS` (≤120) bounds the whole RPC, including
artifact upload. Each model call gets the smaller of `MODEL_TIMEOUT_SECONDS` and
the time left on that deadline. Retries and repairs count against the task budget.
Exceeding it returns `RESOURCE_EXHAUSTED`.

## Records and provenance

Every model call is recorded in the agent's private `agent.sqlite3`
(`model_calls` table) before its reply is used: task, role, purpose, backend,
prompt/response SHA-256 and artifact keys, token usage, stop reason, latency and
failure reason. Prompts and replies are stored as task-scoped artifacts in the
agent volume. Calls still running when a service restarts are marked
`owner_restart`.

Proposal and review provenance carries the model identity as
`model-v1:<provider>:<model-id>` (for example
`model-v1:bedrock:us.amazon.nova-micro-v1:0`), and the coordinator's agent RPC
receipts keep it. Completed operations are reused through the existing idempotent
journals, so a coordinator rerun does not call the model again.

## Reproducible tests

[`tests/test_model_adapter.py`](../tests/test_model_adapter.py) covers configuration,
the Bedrock request/response mapping with a stub client, retries, budgets,
deadlines, replay/recording, prompt-injection framing, the review gates, and
end-to-end runs through real gRPC agent services on a program the deterministic
rules do not recognize. It never contacts a network service.

To capture replay fixtures from a live run, set `MODEL_RECORD_DIRECTORY`; to replay
them, use `MODEL_PROVIDER=replay` with `MODEL_REPLAY_DIRECTORY` pointing to the same
directory and the same `MODEL_ID`. Generation prompts contain no task or proposal IDs, and
review evidence drops task, proposal and job IDs, so identical source and evidence
usually replay across tasks.
