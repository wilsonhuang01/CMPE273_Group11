# Customer-managed keys separate secrets, logs and data so each can be granted and
# retired independently. Rotation is on; the deletion window follows data_protection.

data "aws_iam_policy_document" "key_admin" {
  statement {
    sid       = "AccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_kms_key" "secrets" {
  description             = "${local.name} Secrets Manager entries"
  enable_key_rotation     = true
  deletion_window_in_days = local.kms_deletion_window
  policy                  = data.aws_iam_policy_document.key_admin.json
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${local.name}/secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

# Artifacts, database, broker and cache (consumed by action R).
resource "aws_kms_key" "data" {
  description             = "${local.name} artifacts, state and cache"
  enable_key_rotation     = true
  deletion_window_in_days = local.kms_deletion_window
  policy                  = data.aws_iam_policy_document.key_admin.json
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}/data"
  target_key_id = aws_kms_key.data.key_id
}

data "aws_iam_policy_document" "logs_key" {
  source_policy_documents = [data.aws_iam_policy_document.key_admin.json]
  statement {
    sid = "CloudWatchLogs"
    actions = [
      "kms:Encrypt*",
      "kms:Decrypt*",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:Describe*",
    ]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/${var.project}/${var.environment}/*"]
    }
  }
}

resource "aws_kms_key" "logs" {
  description             = "${local.name} CloudWatch Logs"
  enable_key_rotation     = true
  deletion_window_in_days = local.kms_deletion_window
  policy                  = data.aws_iam_policy_document.logs_key.json
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${local.name}/logs"
  target_key_id = aws_kms_key.logs.key_id
}
