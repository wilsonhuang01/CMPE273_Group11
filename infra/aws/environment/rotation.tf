# Secret rotation and the in-VPC service check (action R, phase 4).
#
# - rotate-rabbitmq (in the VPC): alternating broker users through the RabbitMQ API.
# - rotate-redis (outside the VPC): ElastiCache AUTH token ROTATE through the AWS API.
# - service-check (in the VPC, coordinator security group): runs
#   compiler_optimizer.runtime.service_check against the managed engines for
#   `./project infra validate`.
#
# `./project infra` builds both packages reproducibly before every plan and passes
# their paths here. The RDS secret is rotated by RDS itself (database.tf).

variable "rotation_package" {
  type        = string
  description = "Zip of infra/aws/lambda/rotation.py, built by ./project infra."
}

variable "service_check_package" {
  type        = string
  description = "Zip of the optimizer package and drivers for linux/arm64, built by ./project infra."
}

variable "rotation_days" {
  type        = number
  default     = 30
  description = "Rotation interval for the broker and cache credentials."
  validation {
    condition     = var.rotation_days >= 1 && var.rotation_days <= 365
    error_message = "rotation_days must be between 1 and 365."
  }
}

locals {
  functions = {
    rotate-rabbitmq = { timeout = 120, memory = 256, package = var.rotation_package, in_vpc = true }
    rotate-redis    = { timeout = 900, memory = 256, package = var.rotation_package, in_vpc = false }
    service-check   = { timeout = 300, memory = 512, package = var.service_check_package, in_vpc = true }
  }
  rds_ca_bundle = "/var/task/rds-global-bundle.pem"
  database_url = format(
    "postgresql://%s@%s:%d/%s?sslmode=verify-full&sslrootcert=%%s",
    local.database_user, aws_db_instance.state.address, aws_db_instance.state.port, local.database_name,
  )
  # Non-secret variables for any process acting as the coordinator. The control plane (T)
  # reads these from the environment configuration; the image provides the CA bundle.
  coordinator_environment = {
    ARTIFACT_BACKEND       = "s3"
    ARTIFACT_S3_BUCKET     = aws_s3_bucket.artifacts.bucket
    ARTIFACT_S3_PREFIX     = local.artifact_prefix
    ARTIFACT_S3_KMS_KEY_ID = aws_kms_key.data.arn
    DATABASE_URL           = format(local.database_url, local.database_ca_path)
    DATABASE_SECRET_ID     = aws_db_instance.state.master_user_secret[0].secret_arn
    RABBITMQ_HOST          = local.broker_host
    RABBITMQ_TLS           = "true"
    RABBITMQ_USER          = local.rabbitmq_user
    RABBITMQ_SECRET_ID     = aws_secretsmanager_secret.service["rabbitmq-password"].arn
    REDIS_HOST             = aws_elasticache_replication_group.cache.primary_endpoint_address
    REDIS_TLS              = "true"
    REDIS_SECRET_ID        = aws_secretsmanager_secret.service["redis-auth-token"].arn
  }
  function_environment = {
    rotate-rabbitmq = {
      ROTATION_KIND    = "rabbitmq"
      RABBITMQ_API_URL = local.broker_api_url
      RABBITMQ_USER    = local.rabbitmq_user
    }
    rotate-redis = {
      ROTATION_KIND        = "redis"
      REPLICATION_GROUP_ID = aws_elasticache_replication_group.cache.id
    }
    service-check = merge(local.coordinator_environment, {
      DATABASE_URL      = format(local.database_url, local.rds_ca_bundle)
      STATE_DIRECTORY   = "/tmp/var"
      EXECUTION_ENABLED = "false"
      TRANSFER_ENABLED  = "false"
    })
  }
  function_handlers = {
    rotate-rabbitmq = "rotation.lambda_handler"
    rotate-redis    = "rotation.lambda_handler"
    service-check   = "compiler_optimizer.runtime.service_check.lambda_handler"
  }
}

# --- Network -----------------------------------------------------------------------

resource "aws_security_group" "rotation" {
  name        = "${local.name}-rotation"
  description = "Broker credential rotation (${var.environment})"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-rotation" }
}

resource "aws_vpc_security_group_egress_rule" "rotation" {
  security_group_id = aws_security_group.rotation.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Broker management API and Secrets Manager endpoint"
}

resource "aws_vpc_security_group_egress_rule" "rotation_https" {
  count             = var.enable_nat_gateway ? 1 : 0
  security_group_id = aws_security_group.rotation.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Secrets Manager through the NAT gateway"
}

resource "aws_vpc_security_group_ingress_rule" "broker_api_from_rotation" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_security_group.rotation.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "broker-api-from-rotation"
}

# --- Identity ----------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "function" {
  for_each             = local.functions
  name                 = "${local.name}-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

resource "aws_iam_role_policy_attachment" "function_vpc" {
  for_each   = { for k, f in local.functions : k => f if f.in_vpc }
  role       = aws_iam_role.function[each.key].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "function_logs" {
  for_each = local.functions
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function[each.key].arn}:*"]
  }
}

resource "aws_iam_role_policy" "function_logs" {
  for_each = local.functions
  name     = "write-logs"
  role     = aws_iam_role.function[each.key].id
  policy   = data.aws_iam_policy_document.function_logs[each.key].json
}

# Each rotation function may change only its own secret.
data "aws_iam_policy_document" "rotate" {
  for_each = {
    rotate-rabbitmq = aws_secretsmanager_secret.service["rabbitmq-password"].arn
    rotate-redis    = aws_secretsmanager_secret.service["redis-auth-token"].arn
  }
  statement {
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
      "secretsmanager:PutSecretValue",
      "secretsmanager:UpdateSecretVersionStage",
    ]
    resources = [each.value]
  }
  statement {
    actions   = ["secretsmanager:GetRandomPassword"]
    resources = ["*"]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.secrets.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
  dynamic "statement" {
    for_each = each.key == "rotate-redis" ? [1] : []
    content {
      actions = [
        "elasticache:DescribeReplicationGroups",
        "elasticache:ModifyReplicationGroup",
      ]
      resources = [aws_elasticache_replication_group.cache.arn]
    }
  }
}

resource "aws_iam_role_policy" "rotate" {
  for_each = data.aws_iam_policy_document.rotate
  name     = "rotate-secret"
  role     = aws_iam_role.function[each.key].id
  policy   = each.value.json
}

resource "aws_iam_role_policy" "service_check_secrets" {
  name   = "read-runtime-secrets"
  role   = aws_iam_role.function["service-check"].id
  policy = data.aws_iam_policy_document.runtime_secrets["coordinator"].json
}

resource "aws_iam_role_policy" "service_check_artifacts" {
  name   = "artifact-store"
  role   = aws_iam_role.function["service-check"].id
  policy = data.aws_iam_policy_document.artifact_access.json
}

# --- Functions ---------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "function" {
  for_each          = local.functions
  name              = "/${var.project}/${var.environment}/lambda/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
}

resource "aws_lambda_function" "function" {
  for_each         = local.functions
  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.function[each.key].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = local.function_handlers[each.key]
  filename         = each.value.package
  source_code_hash = filebase64sha256(each.value.package)
  timeout          = each.value.timeout
  memory_size      = each.value.memory

  environment {
    variables = local.function_environment[each.key]
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.function[each.key].name
  }

  dynamic "vpc_config" {
    for_each = each.value.in_vpc ? [1] : []
    content {
      subnet_ids = local.subnet_ids.service
      security_group_ids = [
        each.key == "service-check" ? aws_security_group.workload["coordinator"].id : aws_security_group.rotation.id
      ]
    }
  }

  depends_on = [
    aws_iam_role_policy.function_logs,
    aws_iam_role_policy_attachment.function_vpc,
  ]
}

resource "aws_lambda_permission" "rotation" {
  for_each = {
    rotate-rabbitmq = aws_secretsmanager_secret.service["rabbitmq-password"].arn
    rotate-redis    = aws_secretsmanager_secret.service["redis-auth-token"].arn
  }
  statement_id  = "AllowSecretsManagerRotation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.function[each.key].function_name
  principal     = "secretsmanager.amazonaws.com"
  source_arn    = each.value
}

resource "aws_secretsmanager_secret_rotation" "service" {
  for_each = {
    rabbitmq-password = "rotate-rabbitmq"
    redis-auth-token  = "rotate-redis"
  }
  secret_id           = aws_secretsmanager_secret.service[each.key].id
  rotation_lambda_arn = aws_lambda_function.function[each.value].arn
  # `./project infra up` seeds and applies the first value, then rotates; a rotation
  # here would run before the broker or cache has a credential to rotate from.
  rotate_immediately = false
  rotation_rules {
    automatically_after_days = var.rotation_days
  }
  depends_on = [aws_lambda_permission.rotation]
}
