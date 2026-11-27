# Workload identities. Every role carries a permissions boundary that forbids IAM
# changes and opening the network, whatever policies later actions attach.

data "aws_iam_policy_document" "boundary" {
  statement {
    sid       = "AllowWithinGrantedPolicies"
    actions   = ["*"]
    resources = ["*"]
  }
  statement {
    sid    = "DenyPrivilegeAndNetworkChanges"
    effect = "Deny"
    actions = [
      "iam:*",
      "organizations:*",
      "account:*",
      "ec2:CreateVpc*",
      "ec2:CreateInternetGateway",
      "ec2:AttachInternetGateway",
      "ec2:CreateNatGateway",
      "ec2:AllocateAddress",
      "ec2:AssociateAddress",
      "ec2:AuthorizeSecurityGroup*",
      "ec2:CreateRoute*",
      "ec2:ReplaceRoute*",
      "ec2:ModifyVpcEndpoint*",
      "kms:ScheduleKeyDeletion",
      "kms:PutKeyPolicy",
      "secretsmanager:DeleteSecret",
      "secretsmanager:PutResourcePolicy",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "boundary" {
  name        = "${local.name}-workload-boundary"
  description = "Maximum permissions for ${local.name} workloads"
  policy      = data.aws_iam_policy_document.boundary.json
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# Reads exactly the secrets a workload consumes, decrypting only through Secrets Manager.
data "aws_iam_policy_document" "read_secrets" {
  for_each = local.workload_secrets
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for name in each.value : aws_secretsmanager_secret.service[name].arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.secrets.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}

# --- ECS: execution roles inject secrets; task roles are the runtime identity ---

resource "aws_iam_role" "execution" {
  for_each             = local.ecs_workloads
  name                 = "${local.name}-${each.key}-execution"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_assume.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

resource "aws_iam_role_policy_attachment" "execution_base" {
  for_each   = local.ecs_workloads
  role       = aws_iam_role.execution[each.key].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  for_each = local.ecs_workloads
  name     = "inject-secrets"
  role     = aws_iam_role.execution[each.key].id
  policy   = data.aws_iam_policy_document.read_secrets[each.key].json
}

resource "aws_iam_role" "task" {
  for_each             = local.ecs_workloads
  name                 = "${local.name}-${each.key}-task"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_assume.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

data "aws_iam_policy_document" "bedrock" {
  statement {
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = var.bedrock_model_patterns
  }
}

resource "aws_iam_role_policy" "agents_bedrock" {
  count  = var.enable_bedrock ? 1 : 0
  name   = "invoke-bedrock-models"
  role   = aws_iam_role.task["agents"].id
  policy = data.aws_iam_policy_document.bedrock.json
}

# --- EC2 sandbox workers -------------------------------------------------------

resource "aws_iam_role" "worker" {
  name                 = "${local.name}-worker"
  assume_role_policy   = data.aws_iam_policy_document.ec2_assume.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

resource "aws_iam_role_policy_attachment" "worker" {
  for_each = toset([
    "AmazonSSMManagedInstanceCore",       # shell access without SSH or public IPs
    "AmazonEC2ContainerRegistryReadOnly", # pull toolchain images
    "CloudWatchAgentServerPolicy",        # logs and metrics
  ])
  role       = aws_iam_role.worker.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.value}"
}

resource "aws_iam_role_policy" "worker_secrets" {
  name   = "read-worker-secrets"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.read_secrets["worker"].json
}

resource "aws_iam_instance_profile" "worker" {
  name = "${local.name}-worker"
  role = aws_iam_role.worker.name
}

# --- Runtime secret reads (action R) ---------------------------------------------------
# Rotating credentials are read at connect time by the running task (RABBITMQ_SECRET_ID,
# REDIS_SECRET_ID, DATABASE_SECRET_ID), so the task role needs them too, not only the
# execution role that injects start-up values. The EC2 worker role already has them.

locals {
  runtime_secrets = {
    coordinator = [
      aws_secretsmanager_secret.service["rabbitmq-password"].arn,
      aws_secretsmanager_secret.service["redis-auth-token"].arn,
      aws_db_instance.state.master_user_secret[0].secret_arn,
    ]
    scheduler = [
      aws_secretsmanager_secret.service["rabbitmq-password"].arn,
      aws_secretsmanager_secret.service["redis-auth-token"].arn,
    ]
  }
}

data "aws_iam_policy_document" "runtime_secrets" {
  for_each = local.runtime_secrets
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = each.value
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.secrets.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "task_runtime_secrets" {
  for_each = local.runtime_secrets
  name     = "read-rotating-secrets"
  role     = aws_iam_role.task[each.key].id
  policy   = data.aws_iam_policy_document.runtime_secrets[each.key].json
}
