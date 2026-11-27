# One non-secret document that later actions (T control plane, U workers)
# read instead of hard-coding IDs. It records capacity limits, cost ownership and
# retained-data behavior alongside the network and identity references.

locals {
  environment_config = {
    schema_version = 2
    project        = var.project
    environment    = var.environment
    region         = var.region
    account_id     = local.account_id
    network = {
      vpc_id              = aws_vpc.main.id
      vpc_cidr            = var.vpc_cidr
      availability_zones  = local.azs
      subnet_ids          = local.subnet_ids
      nat_gateway         = var.enable_nat_gateway
      interface_endpoints = sort(tolist(local.endpoint_services))
    }
    security_groups = merge(
      { for k, sg in aws_security_group.workload : k => sg.id },
      { data = aws_security_group.data.id, endpoints = aws_security_group.endpoints.id },
    )
    roles = {
      execution            = { for k, r in aws_iam_role.execution : k => r.arn }
      task                 = { for k, r in aws_iam_role.task : k => r.arn }
      worker               = aws_iam_role.worker.arn
      worker_profile       = aws_iam_instance_profile.worker.name
      permissions_boundary = aws_iam_policy.boundary.arn
    }
    kms_keys = {
      secrets = aws_kms_key.secrets.arn
      data    = aws_kms_key.data.arn
      logs    = aws_kms_key.logs.arn
    }
    secrets          = { for k, s in aws_secretsmanager_secret.service : k => s.arn }
    workload_secrets = local.workload_secrets
    capacity = {
      worker_architecture   = var.worker_architecture
      worker_instance_types = var.worker_instance_types
      worker                = var.worker_capacity
      max_worker_ceiling    = var.max_worker_ceiling
      services              = var.service_capacity
    }
    retention = {
      data_protection         = var.data_protection
      log_retention_days      = var.log_retention_days
      artifact_retention_days = var.artifact_retention_days
      evidence_retention_days = var.evidence_retention_days
      kms_deletion_window     = local.kms_deletion_window
      secret_recovery_days    = local.secret_recovery_days
    }
    cost = {
      owner              = var.cost_owner
      center             = var.cost_center
      scope_tag          = local.tags.CostScope
      monthly_budget_usd = var.monthly_budget_usd
      expires_on         = var.expires_on
    }
    model = {
      bedrock_enabled = var.enable_bedrock
    }
    # Managed services from action R. `coordinator_environment` is the non-secret part
    # of the coordinator's configuration; credentials are read through the secret IDs.
    data_services = {
      artifacts = {
        bucket      = aws_s3_bucket.artifacts.bucket
        prefix      = local.artifact_prefix
        object_lock = local.retain
      }
      database = {
        endpoint       = aws_db_instance.state.address
        port           = aws_db_instance.state.port
        name           = local.database_name
        engine_version = aws_db_instance.state.engine_version_actual
        secret_arn     = aws_db_instance.state.master_user_secret[0].secret_arn
        ca_bundle_url  = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
        ca_bundle_path = local.database_ca_path
        rotation_days  = var.database.rotation_days
      }
      broker = {
        endpoint       = aws_mq_broker.main.instances[0].endpoints[0]
        engine_version = local.rabbitmq_version
        rotation_days  = var.rotation_days
      }
      cache = {
        endpoint       = aws_elasticache_replication_group.cache.primary_endpoint_address
        engine_version = "valkey ${local.valkey_version}"
        rotation_days  = var.rotation_days
      }
      coordinator_environment = local.coordinator_environment
    }
  }
}

resource "aws_ssm_parameter" "environment" {
  name        = "/${var.project}/${var.environment}/environment"
  description = "Infrastructure references and limits for ${local.name} (non-secret)"
  type        = "String"
  tier        = "Intelligent-Tiering" # Advanced only if the document outgrows 4 KB.
  value       = jsonencode(local.environment_config)
}

output "environment_config" {
  value = local.environment_config
}

output "secret_names" {
  description = "Secrets that `./project infra up` seeds when they have no value."
  value       = [for s in aws_secretsmanager_secret.service : s.name]
}

# Read by `./project infra up` to seed and rotate credentials after apply.
output "data_services_admin" {
  value = {
    broker_secret        = aws_secretsmanager_secret.service["rabbitmq-password"].name
    broker_user          = local.rabbitmq_user
    cache_secret         = aws_secretsmanager_secret.service["redis-auth-token"].name
    cache_group_id       = aws_elasticache_replication_group.cache.id
    database_secret      = aws_db_instance.state.master_user_secret[0].secret_arn
    service_check_lambda = aws_lambda_function.function["service-check"].function_name
  }
}

output "broker_bootstrap_password" {
  description = "Initial broker password; `infra up` rotates it out of use immediately."
  value       = random_password.broker_bootstrap.result
  sensitive   = true
}
