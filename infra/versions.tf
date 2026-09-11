terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State holds DATABASE_URL and the Grafana password. Keep it local and
  # gitignored (default), or move it to an encrypted S3 backend before this
  # leaves a single operator's laptop.
  #
  # backend "s3" {
  #   bucket       = "<STATE_BUCKET>"
  #   key          = "test-rca-app/terraform.tfstate"
  #   region       = "<REGION>"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
