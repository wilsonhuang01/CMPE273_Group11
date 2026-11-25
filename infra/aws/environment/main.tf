data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  azs        = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  retain     = var.data_protection == "retain"

  tags = merge(
    {
      Project        = var.project
      Environment    = var.environment
      CostOwner      = var.cost_owner
      CostCenter     = var.cost_center
      CostScope      = "${var.project}-${var.environment}"
      ManagedBy      = "terraform"
      Repository     = "CMPE273_Group11"
      DataProtection = var.data_protection
      Teardown       = local.retain ? "protected" : "allowed"
    },
    var.expires_on == "" ? {} : { ExpiresOn = var.expires_on },
  )

  # Private subnets must reach these AWS APIs to pull images, ship logs and read secrets.
  required_endpoints = ["ecr.api", "ecr.dkr", "logs", "secretsmanager"]
  missing_endpoints  = setsubtract(local.required_endpoints, var.interface_endpoints)
  endpoint_services = toset(concat(
    var.interface_endpoints,
    var.enable_bedrock && !var.enable_nat_gateway ? ["bedrock-runtime"] : [],
  ))

  # Every secret is created empty; `./project infra up` seeds random values in memory.
  secrets = {
    rabbitmq-password      = "Amazon MQ (RabbitMQ) application user password"
    redis-auth-token       = "ElastiCache quarantine cache AUTH token"
    agent-api-token        = "Bearer token between the coordinator and agent RPC services"
    optimizer-api-token    = "Operator bearer token for submission, transfer and worker RPCs"
    grafana-admin-password = "Grafana administrator password"
  }

  # Least privilege: each workload's execution role reads only the secrets it injects.
  workload_secrets = {
    coordinator = ["rabbitmq-password", "redis-auth-token", "agent-api-token", "optimizer-api-token"]
    scheduler   = ["rabbitmq-password", "redis-auth-token", "optimizer-api-token"]
    agents      = ["agent-api-token"]
    monitoring  = ["grafana-admin-password"]
    worker      = ["rabbitmq-password", "redis-auth-token", "optimizer-api-token"]
  }
  ecs_workloads = toset(["coordinator", "scheduler", "agents", "monitoring"])

  kms_deletion_window  = local.retain ? 30 : 7
  secret_recovery_days = local.retain ? 30 : 0
}

# Plan-time guards: fail before any change instead of after a partial apply.
resource "terraform_data" "guards" {
  lifecycle {
    precondition {
      condition     = var.enable_nat_gateway || length(local.missing_endpoints) == 0
      error_message = "Without a NAT gateway, interface_endpoints must include ecr.api, ecr.dkr, logs and secretsmanager."
    }
    precondition {
      condition     = var.worker_capacity.max <= var.max_worker_ceiling
      error_message = "worker_capacity.max exceeds max_worker_ceiling."
    }
    precondition {
      condition     = var.endpoint_az_count >= 1 && var.endpoint_az_count <= var.az_count
      error_message = "endpoint_az_count must be between 1 and az_count."
    }
    precondition {
      condition     = var.environment == "production" || var.expires_on != ""
      error_message = "Non-production environments need expires_on so they are torn down."
    }
    precondition {
      condition     = var.environment != "production" || local.retain
      error_message = "production must use data_protection = \"retain\"."
    }
    precondition {
      condition = alltrue([
        for t in var.worker_instance_types :
        (var.worker_architecture == "arm64") == can(regex("^[a-z]+[0-9]+[a-z]*g[a-z]*\\.", t))
      ])
      error_message = "worker_instance_types must match worker_architecture (Graviton 'g' families for arm64 only)."
    }
    precondition {
      condition = alltrue([
        for c in values(var.service_capacity) : c >= 1 && c <= 4
      ])
      error_message = "Each service_capacity value must be between 1 and 4."
    }
  }
}
