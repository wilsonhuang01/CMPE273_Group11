# AWS infrastructure (action Q)

Terraform in [`infra/aws`](../infra/aws) defines the AWS foundation that the cloud
deployment actions build on: network, security groups, IAM roles, encryption keys,
Secrets Manager entries, budgets and a published environment configuration. It
does **not** run the optimizer in AWS yet. Storage and messaging (R), images (S),
the ECS control plane (T) and EC2 workers (U) attach to what this creates.

No cloud resources are deployed by this repository. `./project infra check` runs
offline. A `plan` reads the account but changes nothing. Only `up` and `down`
modify AWS, and both stop for confirmation after showing a saved plan.

## What an environment contains

| Area | Resources |
|---|---|
| Network | VPC with three private tiers per availability zone: **service** (ECS control plane, agents, endpoints), **worker** (EC2 sandbox hosts) and **data** (broker, cache, database). The data tier has no default route and a network ACL that only allows VPC traffic. The default security group has no rules. VPC flow logs go to KMS-encrypted CloudWatch Logs. |
| Egress | An S3 gateway endpoint is always present. Interface endpoints (ECR, Logs, CloudWatch, Secrets Manager, KMS, STS, SSM) let private subnets reach AWS without internet access. An optional single NAT gateway provides general egress instead. Bedrock Runtime gets an endpoint when model calls are enabled without NAT. |
| Security groups | `coordinator`, `scheduler`, `agents`, `worker`, `monitoring`, `data` and `endpoints`, mirroring [compose.yaml](../compose.yaml): agents accept gRPC only from the coordinator; the coordinator accepts gRPC only from the scheduler and workers; the data tier accepts AMQPS/Redis/PostgreSQL only from the coordinator, scheduler and workers; health ports accept only monitoring. **Nothing accepts traffic from outside the VPC.** Client access is added with the control plane (T). |
| Identity | For each ECS workload (coordinator, scheduler, agents, monitoring), an execution role that injects only that workload's secrets and an empty task role for later actions. An EC2 worker role and instance profile have SSM access (no SSH), read-only ECR access and the CloudWatch agent policy. Agents get `bedrock:InvokeModel` only when `enable_bedrock` is true. Every workload role has a **permissions boundary** that denies IAM changes, opening the network, and deleting keys or secrets. |
| Encryption | Customer-managed KMS keys for secrets, logs and data (data is used by action R), with rotation enabled. |
| Secrets | `rabbitmq-password`, `redis-auth-token`, `agent-api-token`, `optimizer-api-token` and `grafana-admin-password` under `cmpe273-optimizer/<env>/`. |
| Cost | A monthly AWS Budget filtered on the environment's `CostScope` tag, alerting at 50/80/100% of actual spend and 100% of forecast spend. |
| Configuration | The SSM parameter `/cmpe273-optimizer/<env>/environment` holds the VPC, subnet, security group, role, key and secret references, plus capacity limits, cost ownership and retention. Later actions read it instead of hard-coding IDs. |

Every resource is tagged with `Project`, `Environment`, `CostOwner`, `CostCenter`,
`CostScope`, `DataProtection`, `Teardown` and, for non-production, `ExpiresOn`.

## Environments

Committed, reviewable settings live in [`infra/aws/config`](../infra/aws/config):

| | dev | staging | production |
|---|---|---|---|
| Egress | NAT gateway | Interface endpoints, 1 AZ | Interface endpoints, 2 AZs |
| Workers (min/desired/max, ceiling) | 0/0/1, 2 | 0/1/2, 4 | 1/2/4, 8 |
| Data protection | destroy | destroy | retain |
| Log / artifact / evidence retention (days) | 7 / 7 / 30 | 14 / 14 / 90 | 90 / 90 / 731 |
| Monthly budget | $50 | $120 | $400 |
| Expires | 2026-12-31 | 2026-12-31 | never |

These are the approximate baseline costs of this foundation alone in us-west-2, at
list prices. Check them with the AWS Pricing Calculator before relying on them:
about $40/month for dev (mostly the NAT gateway), about $80/month for staging
(10 interface endpoints in one AZ) and about $150/month for production (endpoints
in two AZs). Each environment also pays about $3/month for KMS keys and $2/month
for secrets.

Terraform checks these rules at plan time, before anything changes. Private
subnets without NAT must have the ECR, Logs and Secrets Manager endpoints. Worker
`max` cannot exceed `max_worker_ceiling`. Instance types must match
`worker_architecture` (Graviton `g` families for arm64). Non-production
environments must set `expires_on`, and production must use `retain`.

## Provision and tear down from a Mac

Prerequisites: Terraform 1.5 or later (`brew install terraform`), AWS credentials
for the target account (`aws configure sso`, then `export AWS_PROFILE=...`) and
Python 3.11 or later. The launcher installs `boto3` (the `aws` extra) the first
time it runs.

```sh
# Once per person: cost owner and budget recipients stay out of git.
cp infra/aws/config/local.tfvars.json.example infra/aws/config/local.tfvars.json
$EDITOR infra/aws/config/local.tfvars.json

# Once per AWS account: versioned, encrypted S3 state bucket plus DynamoDB lock table.
./project infra bootstrap --region us-west-2 --cost-owner cmpe273-group11

./project infra plan --env staging     # saves var/infra/staging-apply.tfplan and a .txt to review
./project infra up --env staging       # re-plans, asks for confirmation, applies, seeds secrets
./project infra output --env staging   # environment references and limits as JSON
./project infra down --env staging     # saved destroy plan, confirmation, teardown
```

`bootstrap` writes `infra/aws/config/backend.json`, which is git-ignored. Every
environment keeps its own state file (`cmpe273-optimizer/<env>/terraform.tfstate`)
in that bucket. Before init, the launcher checks that the current credentials
belong to the account in the bucket name, so a plan can't target the wrong
account. `--yes` skips the confirmation prompt but still applies only the saved,
reviewed plan.

Once per account, activate the `CostScope` cost allocation tag in the Billing
console. Budgets can't attribute spend until it is active.

## Secrets

Terraform creates only empty secret containers, so secret values never appear in
state, plans, tfvars, the repository or release archives. After `up`, the
launcher asks Secrets Manager to generate a 48-character alphanumeric value for
each secret that has no value. Values stay in memory and aren't printed, and
existing values are left alone. At deploy time, each ECS execution role (or the
worker instance role) injects only the secrets listed for that workload in
`workload_secrets`. The launcher also rejects any credential-like key (`password`,
`token`, `secret` and similar) in committed or local tfvars.

## Teardown and retained data

| | `destroy` (dev, staging) | `retain` (production) |
|---|---|---|
| `./project infra down` | Allowed after confirmation | Refused unless `--confirm-protected <env>` repeats the name |
| Secrets | Deleted immediately | 30-day recovery window |
| KMS keys | 7-day deletion window | 30-day deletion window |
| Tag | `Teardown=allowed`, `ExpiresOn` | `Teardown=protected` |

When an environment is past its `ExpiresOn` date, every launcher command prints a
warning with the matching `down` command. Terraform state history remains in the
versioned state bucket after teardown. `./project infra bootstrap --destroy`
removes the bucket only after it has been emptied deliberately.

The artifact and evidence retention values are recorded in the environment
configuration now. The storage stack (R) will enforce them.

## Validation

- `./project infra check` and the CI `infrastructure` job run `terraform fmt`,
  `terraform validate` for both stacks and the committed-settings checks.
  No credentials are needed.
- [tests/test_infra.py](../tests/test_infra.py) covers settings merging and
  credential rejection, the protected-teardown guard, per-environment state
  keys, the account check, expiry warnings, idempotent secret seeding and the
  match between declared and consumed secrets.
- During development, read-only plans against a real account produced 106 (dev),
  104 each for staging and production. Plans were also rejected when
  required endpoints were missing or the instance type didn't match the
  architecture. Nothing was applied.
