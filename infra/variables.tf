# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Short name used to prefix every resource. Lowercase, no spaces."
  type        = string
  default     = "test-rca-app"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric with hyphens, 3-30 chars."
  }
}

variable "environment" {
  description = "Environment name (test, dev, staging, prod)."
  type        = string
  default     = "test"
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR block for the VPC created by this stack."
  type        = string
  default     = "10.20.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "Two public subnet CIDRs, one per AZ. An ALB requires at least two AZs."
  type        = list(string)
  default     = ["10.20.1.0/24", "10.20.2.0/24"]

  validation {
    condition     = length(var.public_subnet_cidrs) >= 2
    error_message = "Provide at least two subnet CIDRs so the ALB can span two AZs."
  }
}

# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

variable "instance_type" {
  description = <<-EOT
    EC2 instance type. The Compose stack runs seven containers (app, nginx,
    Prometheus, Loki, Alloy, Grafana, node-exporter), so 4 GB RAM is the
    practical floor. t3.small will OOM under load.
  EOT
  type        = string
  default     = "t3.medium"
}

variable "root_volume_size" {
  description = "Root EBS volume size in GiB. Prometheus (15d) + Loki + Docker images need room."
  type        = number
  default     = 30
}

variable "enable_detailed_monitoring" {
  description = "1-minute EC2 CloudWatch metrics instead of the free 5-minute resolution."
  type        = bool
  default     = true
}

variable "key_pair_name" {
  description = <<-EOT
    Optional EC2 key pair for SSH. Leave null and use SSM Session Manager
    (`aws ssm start-session --target <instance-id>`) — no key, no open port 22.
  EOT
  type        = string
  default     = null
}

variable "ssh_allowed_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach port 22 on the instance. Empty (default) means no
    inbound SSH rule at all. Never set this to 0.0.0.0/0.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.ssh_allowed_cidrs, "0.0.0.0/0")
    error_message = "Refusing to open SSH to the world. Use SSM Session Manager, or list specific CIDRs."
  }
}

variable "docker_compose_version" {
  description = "docker compose CLI plugin version installed on the instance."
  type        = string
  default     = "v2.32.4"
}

# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------

variable "alb_allowed_cidrs" {
  description = "CIDRs allowed to reach the ALB listeners."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "acm_certificate_arn" {
  description = <<-EOT
    Optional ACM certificate ARN in `aws_region`. When set, the ALB gets an
    HTTPS:443 listener and HTTP:80 redirects to it. When null, HTTP:80 serves
    traffic directly.
  EOT
  type        = string
  default     = null
}

variable "health_check_path" {
  description = "Target group health check path. The app exposes /health via nginx."
  type        = string
  default     = "/health"
}

variable "enable_deletion_protection" {
  description = "ALB deletion protection. Must stay false for single-command destroy."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------

# database_url is no longer a variable — it is assembled by Terraform from
# the RDS endpoint and the random_password resource, then stored as an SSM
# SecureString. See rds.tf and ssm.tf.

# ---------------------------------------------------------------------------
# RDS configuration
# ---------------------------------------------------------------------------

variable "rds_instance_class" {
  description = "RDS instance class. db.t3.micro is the cheapest option for a test env."
  type        = string
  default     = "db.t3.micro"
}

variable "rds_allocated_storage" {
  description = "Initial allocated storage for the RDS instance in GiB."
  type        = number
  default     = 20
}

variable "rds_engine_version" {
  description = "PostgreSQL major.minor version. Pin the major version; minor upgrades are automatic."
  type        = string
  default     = "16"
}

variable "rds_db_name" {
  description = "Name of the PostgreSQL database created inside the RDS instance."
  type        = string
  default     = "appdb"
}

variable "rds_username" {
  description = "Master username for the RDS instance."
  type        = string
  default     = "appuser"
}

variable "grafana_admin_user" {
  description = "Grafana admin username."
  type        = string
  default     = "admin"
}

variable "grafana_admin_password" {
  description = "REQUIRED. Grafana admin password. Stored as an SSM SecureString."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.grafana_admin_password) >= 12
    error_message = "grafana_admin_password must be at least 12 characters."
  }
}

variable "app_settings" {
  description = <<-EOT
    Non-secret entries written to /opt/app/.env on the instance. Merged over
    the defaults in locals.tf, so you only need to override what changes.
    Secrets (DATABASE_URL, GRAFANA_ADMIN_PASSWORD) do not belong here.
  EOT
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the system and container log groups."
  type        = number
  default     = 14
}

variable "alarm_email" {
  description = <<-EOT
    Optional email address subscribed to the alarm SNS topic. AWS sends a
    confirmation link that must be clicked before alarms deliver.
  EOT
  type        = string
  default     = null
}

variable "cpu_alarm_threshold" {
  description = "Average EC2 CPU percent that triggers the high-CPU alarm."
  type        = number
  default     = 80
}

variable "memory_alarm_threshold" {
  description = "Memory used percent (CloudWatch agent) that triggers the high-memory alarm."
  type        = number
  default     = 85
}

variable "disk_alarm_threshold" {
  description = "Root disk used percent (CloudWatch agent) that triggers the low-disk alarm."
  type        = number
  default     = 85
}
