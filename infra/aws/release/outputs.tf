output "repositories" {
  description = "Image repository URLs by image name."
  value       = { for name, repository in aws_ecr_repository.image : name => repository.repository_url }
}

output "publisher_role_arn" {
  description = "Set as the AWS_RELEASE_ROLE_ARN repository variable."
  value       = aws_iam_role.publisher.arn
}

output "deployer_role_arns" {
  description = "Set each as AWS_DEPLOY_ROLE_ARN in the GitHub environment of the same name."
  value       = { for name, role in aws_iam_role.deployer : name => role.arn }
}

output "github_variables" {
  description = "Repository-level GitHub Actions variables (not secrets)."
  value = {
    AWS_REGION           = var.region
    AWS_RELEASE_ROLE_ARN = aws_iam_role.publisher.arn
    ECR_REGISTRY         = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  }
}
