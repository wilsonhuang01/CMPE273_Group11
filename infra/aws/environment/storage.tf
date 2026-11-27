# Artifact bucket for the coordinator's S3ArtifactStore (action R, phase 4).
#
# Objects are content-addressed and never overwritten: the bucket policy refuses any
# object write without `If-None-Match: *`, so even a misconfigured client cannot replace
# an artifact. Versioning keeps anything deleted by mistake for artifact_retention_days.
# Retained environments add S3 Object Lock in governance mode, so every object version is
# kept for at least evidence_retention_days.

locals {
  artifact_bucket = "${local.name}-artifacts-${local.account_id}-${var.region}"
  artifact_prefix = "artifacts"
}

resource "aws_s3_bucket" "artifacts" {
  bucket              = local.artifact_bucket
  force_destroy       = !local.retain
  object_lock_enabled = local.retain
  tags                = { Name = local.artifact_bucket, DataClass = "artifacts" }
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "artifacts" {
  count  = local.retain ? 1 : 0
  bucket = aws_s3_bucket.artifacts.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.evidence_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.artifacts]
}

# Live artifacts are referenced by committed state and never expire here; deletion
# needs a reachability-based cleanup (see docs/storage.md). Only superseded versions,
# abandoned uploads and service-check objects age out.
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.artifact_retention_days
    }
    expiration {
      expired_object_delete_marker = true
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  rule {
    id     = "service-check-objects"
    status = "Enabled"
    filter {
      prefix = "${local.artifact_prefix}/service-check/"
    }
    expiration {
      days = 1
    }
  }
  depends_on = [aws_s3_bucket_versioning.artifacts]
}

data "aws_iam_policy_document" "artifact_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
  statement {
    sid       = "DenyOldTls"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
  # Content-addressed objects are created once. Multipart part uploads cannot carry the
  # header, so they are exempted by s3:ObjectCreationOperation as AWS documents.
  statement {
    sid       = "DenyOverwrites"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Null"
      variable = "s3:if-none-match"
      values   = ["true"]
    }
    condition {
      test     = "Bool"
      variable = "s3:ObjectCreationOperation"
      values   = ["true"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket     = aws_s3_bucket.artifacts.id
  policy     = data.aws_iam_policy_document.artifact_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

# Runtime access for the coordinator (and the in-VPC service check): read and create
# objects under the artifact prefix. Nothing can delete objects.
data "aws_iam_policy_document" "artifact_access" {
  statement {
    sid       = "Objects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/${local.artifact_prefix}/*"]
  }
  statement {
    # HeadBucket (readiness) and distinguishing a missing object (404) from a denied
    # one (403) both need ListBucket.
    sid       = "Bucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
  statement {
    sid       = "DataKeyViaS3"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.data.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "coordinator_artifacts" {
  name   = "artifact-store"
  role   = aws_iam_role.task["coordinator"].id
  policy = data.aws_iam_policy_document.artifact_access.json
}
