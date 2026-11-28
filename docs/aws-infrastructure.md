# AWS infrastructure (actions Q and R)

Terraform in [`infra/aws`](../infra/aws) defines the AWS foundation that the cloud
deployment actions build on: network, security groups, IAM roles, encryption keys,
Secrets Manager entries, budgets and a published environment configuration. Action R
adds the managed data services: the S3 artifact bucket, RDS for PostgreSQL, Amazon MQ
for RabbitMQ and ElastiCache for Valkey, plus credential rotation and an in-VPC service
check. It does **not** run the optimizer in AWS yet. Images (S), the ECS control plane
(T) and EC2 workers (U) attach to what this creates.

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
| Encryption | Customer-managed KMS keys for secrets, logs and data (artifacts, database, broker and cache), with rotation enabled. |
| Secrets | `rabbitmq-password`, `redis-auth-token`, `agent-api-token`, `optimizer-api-token` and `grafana-admin-password` under `cmpe273-optimizer/<env>/`, plus the RDS-managed database secret. Broker, cache and database credentials rotate every 30 days. |
| Data services (R) | S3 artifact bucket (no-overwrite policy, versioning, Object Lock in production), RDS for PostgreSQL 16, Amazon MQ for RabbitMQ 3.13 and ElastiCache for Valkey 8.0, all TLS-only in the data tier. Details are in [cloud storage](cloud-storage.md#managed-services-rotation-and-validation-phase-4). |
| Functions (R) | `rotate-rabbitmq` (in the VPC), `rotate-redis` and `service-check` (in the VPC, coordinator security group), each with its own role under the permissions boundary and a KMS-encrypted log group. |
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
| Database | db.t4g.micro, single-AZ, 1-day backups | db.t4g.micro, single-AZ, 3-day backups | db.t4g.small, Multi-AZ, 14-day backups |
| Broker | mq.t3.micro, single instance | mq.t3.micro, single instance | mq.m5.large, single instance |
| Cache | 1 × cache.t4g.micro | 1 × cache.t4g.micro | 2 × cache.t4g.small, Multi-AZ |
| Monthly budget | $100 | $160 | $650 |
| Expires | 2026-12-31 | 2026-12-31 | never |

These are the approximate baseline costs of this foundation alone in us-west-2, at
list prices. Check them with the AWS Pricing Calculator before relying on them:
about $40/month for dev (mostly the NAT gateway), about $80/month for staging
(10 interface endpoints in one AZ) and about $150/month for production (endpoints
in two AZs). Each environment also pays about $3/month for KMS keys and $2/month
for secrets.

The data services add roughly $50/month to dev and to staging (broker about $26,
database about $14, cache about $10). Production adds about $300/month: the mq.m5.large
broker is about $210, the Multi-AZ database about $52 and the two cache nodes about $38.
S3, Lambda and the extra secret cost little at this scale. The budgets were raised to
match. For a highly available broker, set `broker.deployment_mode` to `CLUSTER_MULTI_AZ`
(three brokers at roughly three times the cost). The mq.t3 family can't be clustered.

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
./project infra validate --env staging # broker, cache, database and S3 checks from inside the VPC
./project infra rotate --env staging --secret broker   # rotate now (broker|cache|database|all)
./project infra down --env staging     # saved destroy plan, confirmation, teardown
```

Before every plan, the launcher builds the two Lambda packages under `var/infra/lambda/`
(`pip` downloads linux/arm64 wheels pinned by `constraints.txt`, plus the RDS CA bundle).
Equal inputs give byte-identical archives, so an unchanged tree never shows a Lambda diff.

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
existing values are left alone. RDS creates and rotates the database login itself.

The one exception is the broker. Amazon MQ needs a password when it is created, so
its random bootstrap password is in the encrypted state. `up` copies it into
`rabbitmq-password` and rotates the broker credential twice, which replaces that
password before any service connects. `up` then requires the cache AUTH token
(`ROTATE`, then `SET`). Both steps are skipped when they have already been done.
Rotation itself is described in [cloud storage](cloud-storage.md#credentials-never-pass-through-terraform). At deploy time, each ECS execution role (or the
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

`artifact_retention_days` is how long S3 keeps superseded or deleted artifact versions.
In production, `evidence_retention_days` is also the Object Lock retention for every
artifact version. Live artifacts are never expired automatically.

The data services follow `data_protection` too. In `destroy` environments, the bucket
is emptied on teardown, and the database is deleted without a final snapshot or retained
backups. In production, the database has deletion protection and takes a final snapshot,
and locked artifact versions can't be deleted before their retention ends. Tearing
production down therefore needs deletion protection turned off first, and a bucket that
still holds locked objects is left in place.

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
- With the action R data services, read-only plans produce 150 (dev), 147 (staging)
  and 148 (production) resources. Nothing has been applied yet, so the managed services,
  rotation functions and `infra validate` have not run in AWS.
- `tests/test_infra.py` also checks that no service credential is assigned in Terraform
  except the broker bootstrap password, the post-apply broker and cache bootstrap,
  waiting for rotations, and reproducible Lambda packages.
