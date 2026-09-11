# Secrets live in Parameter Store, not in user data. EC2 user data is readable
# by anything that can reach IMDS and by anyone with ec2:DescribeInstanceAttribute,
# so a password pasted there is effectively public to the account.

resource "aws_ssm_parameter" "database_url" {
  name        = local.ssm_db_url
  description = "PostgreSQL connection string for ${local.name_prefix} (RDS managed)"
  type        = "SecureString"

  # Assembled from the RDS resource so the password is never in tfvars or user data.
  value = "postgresql://${var.rds_username}:${urlencode(random_password.rds.result)}@${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}/${var.rds_db_name}"

  tags = { Name = "${local.name_prefix}-database-url" }
}

resource "aws_ssm_parameter" "grafana_admin_password" {
  name        = local.ssm_grafana
  description = "Grafana admin password for ${local.name_prefix}"
  type        = "SecureString"
  value       = var.grafana_admin_password

  tags = { Name = "${local.name_prefix}-grafana-password" }
}

resource "aws_ssm_parameter" "cloudwatch_agent_config" {
  name        = local.ssm_cw_agent
  description = "CloudWatch agent configuration for ${local.name_prefix}"
  type        = "String"

  value = templatefile("${path.module}/templates/cloudwatch_agent.json.tftpl", {
    metrics_namespace = local.metrics_namespace
    log_group_system  = local.log_group_system
    log_group_docker  = local.log_group_docker
  })

  tags = { Name = "${local.name_prefix}-cwagent-config" }
}
