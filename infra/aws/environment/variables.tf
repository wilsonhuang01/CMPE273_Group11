variable "environment" {
  type        = string
  description = "Deployment environment; each has its own state, network and secrets."
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be dev, staging or production."
  }
}

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
  description = "AWS region for every regional resource in this environment."
}

variable "cost_owner" {
  type        = string
  description = "Team or person accountable for spend. Set it in the git-ignored local tfvars."
  validation {
    condition     = length(trimspace(var.cost_owner)) > 0
    error_message = "cost_owner is required; set it in infra/aws/config/<env>.local.tfvars.json."
  }
}

variable "cost_center" {
  type    = string
  default = "cmpe273"
}

variable "expires_on" {
  type        = string
  default     = ""
  description = "ISO date after which a non-production environment should be torn down."
  validation {
    condition     = var.expires_on == "" || can(regex("^\\d{4}-\\d{2}-\\d{2}$", var.expires_on))
    error_message = "expires_on must be empty or YYYY-MM-DD."
  }
}

# --- Network ---------------------------------------------------------------

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && tonumber(split("/", var.vpc_cidr)[1]) <= 20
    error_message = "vpc_cidr must be a valid IPv4 CIDR of /20 or larger."
  }
}

variable "az_count" {
  type    = number
  default = 2
  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3 so managed data services can be multi-AZ."
  }
}

variable "enable_nat_gateway" {
  type        = bool
  default     = false
  description = "One NAT gateway for general egress. Without it, private subnets reach AWS only via endpoints."
}

variable "interface_endpoints" {
  type        = list(string)
  description = "AWS interface endpoint service suffixes (for example ecr.api) placed in service subnets."
  default = [
    "ecr.api", "ecr.dkr", "logs", "monitoring", "secretsmanager", "kms", "sts",
    "ssm", "ssmmessages", "ec2messages",
  ]
}

variable "endpoint_az_count" {
  type        = number
  default     = 1
  description = "Availability zones that host each interface endpoint (cost scales linearly)."
}

variable "flow_log_traffic" {
  type    = string
  default = "REJECT"
  validation {
    condition     = contains(["ACCEPT", "REJECT", "ALL"], var.flow_log_traffic)
    error_message = "flow_log_traffic must be ACCEPT, REJECT or ALL."
  }
}

# --- Workers and capacity --------------------------------------------------

variable "worker_architecture" {
  type    = string
  default = "arm64"
  validation {
    condition     = contains(["arm64", "x86_64"], var.worker_architecture)
    error_message = "worker_architecture must be arm64 or x86_64."
  }
}

variable "worker_instance_types" {
  type    = list(string)
  default = ["c7g.large"]
}

variable "worker_capacity" {
  type = object({
    min     = number
    desired = number
    max     = number
  })
  default = { min = 0, desired = 1, max = 2 }
  validation {
    condition = (
      var.worker_capacity.min >= 0 &&
      var.worker_capacity.min <= var.worker_capacity.desired &&
      var.worker_capacity.desired <= var.worker_capacity.max
    )
    error_message = "worker_capacity must satisfy 0 <= min <= desired <= max."
  }
}

variable "max_worker_ceiling" {
  type        = number
  default     = 8
  description = "Hard upper bound on worker instances in any environment; guards against runaway scaling."
  validation {
    condition     = var.max_worker_ceiling >= 1 && var.max_worker_ceiling <= 32
    error_message = "max_worker_ceiling must be between 1 and 32."
  }
}

variable "service_capacity" {
  type = object({
    coordinator_tasks = number
    scheduler_tasks   = number
    agent_tasks       = number
  })
  default     = { coordinator_tasks = 1, scheduler_tasks = 1, agent_tasks = 1 }
  description = "Maximum ECS tasks per control-plane service (agent_tasks applies to each agent role)."
}

# --- Model access ----------------------------------------------------------

variable "enable_bedrock" {
  type    = bool
  default = false
}

variable "bedrock_model_patterns" {
  type        = list(string)
  description = "Foundation-model and inference-profile ARN patterns agents may invoke."
  default = [
    "arn:aws:bedrock:*::foundation-model/anthropic.*",
    "arn:aws:bedrock:*:*:inference-profile/*anthropic.*",
  ]
}

# --- Retention, teardown and cost -----------------------------------------

variable "data_protection" {
  type        = string
  default     = "destroy"
  description = "retain: long recovery windows and guarded teardown. destroy: disposable environment."
  validation {
    condition     = contains(["retain", "destroy"], var.data_protection)
    error_message = "data_protection must be retain or destroy."
  }
}

variable "log_retention_days" {
  type    = number
  default = 14
  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.log_retention_days,
    )
    error_message = "log_retention_days must be a CloudWatch Logs retention value."
  }
}

variable "artifact_retention_days" {
  type        = number
  default     = 30
  description = "Retention for task artifacts; recorded now, enforced by the storage stack (action R)."
}

variable "evidence_retention_days" {
  type        = number
  default     = 365
  description = "Retention for quarantine and final-decision evidence (action R)."
}

variable "monthly_budget_usd" {
  type = number
  validation {
    condition     = var.monthly_budget_usd > 0 && var.monthly_budget_usd <= 2000
    error_message = "monthly_budget_usd must be between 0 and 2000."
  }
}

variable "budget_alert_emails" {
  type        = list(string)
  description = "Budget recipients. Set them in the git-ignored local tfvars, not committed files."
  validation {
    condition     = length(var.budget_alert_emails) > 0
    error_message = "Set at least one budget_alert_emails entry in infra/aws/config/<env>.local.tfvars.json."
  }
}
