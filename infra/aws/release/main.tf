# Account-level release resources (action S): image repositories shared by every
# environment, so a release is built once and promoted by digest, and the GitHub
# OIDC roles that let workflows publish and deploy without stored AWS access keys.
# Applied with `./project infra release-up`; state lives beside the environments.

terraform {
  required_version = ">= 1.5.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.project
      Component = "release"
      CostOwner = var.cost_owner
      CostScope = "${var.project}-release"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  # app: coordinator, scheduler, worker and agent services (multi-arch Python image).
  # compiler: the trusted sandbox toolchain; each platform's image labels its target.
  repositories = toset(["app", "compiler"])
  oidc_host    = "token.actions.githubusercontent.com"
  oidc_arn = (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.oidc_host}"
  )
  release_parameter_arns = {
    for environment in var.deploy_environments : environment =>
    "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${var.project}/${environment}/release"
  }
}

# --- Image repositories ------------------------------------------------------------

resource "aws_ecr_repository" "image" {
  for_each = local.repositories
  name     = "${var.project}/${each.key}"
  # A published tag always names the same digest; releases are never rewritten.
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "image" {
  for_each   = aws_ecr_repository.image
  repository = each.value.name
  # Platform images of a release carry their own tags (v1.2.3-amd64), so expiring
  # untagged images never removes a platform still referenced by a release index.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Remove abandoned untagged uploads"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the newest release images (index plus one per platform)"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = var.retained_release_images
        }
        action = { type = "expire" }
      },
    ]
  })
}

# --- GitHub Actions identity -------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  count           = var.create_github_oidc_provider ? 1 : 0
  url             = "https://${local.oidc_host}"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

# Publishing is allowed only from release tags of this one repository.
data "aws_iam_policy_document" "publisher_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "${local.oidc_host}:sub"
      values   = ["repo:${var.github_repository}:ref:refs/tags/v*"]
    }
  }
}

resource "aws_iam_role" "publisher" {
  name                 = "${var.project}-release-publisher"
  description          = "GitHub Actions: build, push and scan release images"
  assume_role_policy   = data.aws_iam_policy_document.publisher_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "publisher" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImageScanFindings",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for repository in aws_ecr_repository.image : repository.arn]
  }
}

resource "aws_iam_role_policy" "publisher" {
  name   = "publish-release-images"
  role   = aws_iam_role.publisher.id
  policy = data.aws_iam_policy_document.publisher.json
}

# Deploying to an environment is allowed only from jobs bound to the GitHub
# environment of the same name, so its protection rules and reviewers apply.
data "aws_iam_policy_document" "deployer_trust" {
  for_each = toset(var.deploy_environments)
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["repo:${var.github_repository}:environment:${each.key}"]
    }
  }
}

resource "aws_iam_role" "deployer" {
  for_each             = toset(var.deploy_environments)
  name                 = "${var.project}-${each.key}-deployer"
  description          = "GitHub Actions: promote and roll back releases in ${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.deployer_trust[each.key].json
  max_session_duration = 3600
}

# The release pointer (/project/env/release) is the single deployed-release record;
# its parameter history is the rollback list. Action T adds the ECS service updates.
data "aws_iam_policy_document" "deployer" {
  for_each = toset(var.deploy_environments)
  statement {
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameterHistory",
      "ssm:PutParameter",
    ]
    resources = [local.release_parameter_arns[each.key]]
  }
  statement {
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${var.project}/${each.key}/environment",
    ]
  }
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:DescribeImageScanFindings",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for repository in aws_ecr_repository.image : repository.arn]
  }
}

resource "aws_iam_role_policy" "deployer" {
  for_each = toset(var.deploy_environments)
  name     = "promote-releases"
  role     = aws_iam_role.deployer[each.key].id
  policy   = data.aws_iam_policy_document.deployer[each.key].json
}
