variable "project" {
  type    = string
  default = "cmpe273-optimizer"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.project))
    error_message = "project must be 3-31 lowercase letters, digits or hyphens."
  }
}

variable "region" {
  type        = string
  description = "Region of the image repositories; it must match the environments."
}

variable "cost_owner" {
  type        = string
  description = "Team or person accountable for spend. Set it in the git-ignored local tfvars."
  validation {
    condition     = length(trimspace(var.cost_owner)) > 0
    error_message = "cost_owner is required; set it in infra/aws/config/local.tfvars.json."
  }
}

variable "github_repository" {
  type        = string
  description = "owner/name of the only repository whose workflows may assume the release roles."
  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "github_repository must be owner/name with no wildcards."
  }
}

variable "deploy_environments" {
  type        = list(string)
  description = "Environments that GitHub Actions may promote releases into."
  validation {
    condition     = alltrue([for e in var.deploy_environments : contains(["dev", "staging", "production"], e)])
    error_message = "deploy_environments may contain only dev, staging and production."
  }
}

variable "create_github_oidc_provider" {
  type        = bool
  default     = true
  description = "false when the account already has the token.actions.githubusercontent.com provider."
}

variable "retained_release_images" {
  type        = number
  default     = 90
  description = "Release-tagged images kept per repository (an index plus one image per platform)."
  validation {
    condition     = var.retained_release_images >= 9
    error_message = "keep at least three releases (nine images) so rollback has targets."
  }
}
