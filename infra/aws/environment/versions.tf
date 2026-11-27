terraform {
  required_version = ">= 1.5.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # Partial configuration: `./project infra` passes the bucket, key, lock table and
  # region created by infra/aws/bootstrap, so one module serves every environment.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  # Tags drive cost allocation, ownership and teardown rules for every resource.
  default_tags {
    tags = local.tags
  }
}
