output "application_url" {
  description = "Public entry point for the application."
  value       = var.acm_certificate_arn == null ? "http://${aws_lb.this.dns_name}" : "https://${aws_lb.this.dns_name}"
}

output "alb_dns_name" {
  description = "ALB DNS name. Point a CNAME here (including the Grafana subdomain)."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone ID, for a Route 53 alias record."
  value       = aws_lb.this.zone_id
}

output "target_group_arn" {
  description = "ARN of the target group fronting nginx on the instance."
  value       = aws_lb_target_group.app.arn
}

output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.app.id
}

output "instance_private_ip" {
  description = "Private IP of the instance."
  value       = aws_instance.app.private_ip
}

output "ssm_session_command" {
  description = "Open a root shell on the instance without SSH or an open port 22."
  value       = "aws ssm start-session --target ${aws_instance.app.id} --region ${var.aws_region}"
}

output "cloudwatch_dashboard_url" {
  description = "CloudWatch dashboard for this stack."
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.this.dashboard_name}"
}

output "region" {
  description = "Region this stack is deployed in."
  value       = var.aws_region
}

output "system_log_group" {
  description = "CloudWatch log group for bootstrap, cloud-init and system logs."
  value       = aws_cloudwatch_log_group.system.name
}

output "container_log_group" {
  description = "CloudWatch log group for Docker container stdout/stderr."
  value       = aws_cloudwatch_log_group.docker.name
}

output "alarm_topic_arn" {
  description = "SNS topic every alarm publishes to."
  value       = aws_sns_topic.alarms.arn
}

output "alarm_names" {
  description = "Every CloudWatch alarm created for this stack."
  value = [
    aws_cloudwatch_metric_alarm.unhealthy_hosts.alarm_name,
    aws_cloudwatch_metric_alarm.app_error_rate.alarm_name,
    aws_cloudwatch_metric_alarm.db_connection_timeout_rate.alarm_name,
    aws_cloudwatch_metric_alarm.rds_cpu.alarm_name,
    # Lambda → RDS connection exhaustion scenario
    aws_cloudwatch_metric_alarm.lambda_errors.alarm_name,
    aws_cloudwatch_metric_alarm.lambda_duration_p95.alarm_name,
    aws_cloudwatch_metric_alarm.rds_connections_high.alarm_name,
    aws_cloudwatch_metric_alarm.rds_cpu_conn_exhaust.alarm_name,
  ]
}

output "artifact_bucket" {
  description = "S3 bucket holding the deployed application bundle."
  value       = aws_s3_bucket.artifacts.id
}

output "deployed_bundle_key" {
  description = "S3 key of the bundle this instance booted from - changes when the app code changes."
  value       = aws_s3_object.app.key
}

# ---------------------------------------------------------------------------
# RDS outputs
# ---------------------------------------------------------------------------

output "rds_endpoint" {
  description = "RDS instance endpoint (host:port). Use this to connect to the database directly."
  value       = "${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}"
}

output "rds_db_name" {
  description = "Name of the PostgreSQL database inside the RDS instance."
  value       = aws_db_instance.postgres.db_name
}

output "rds_username" {
  description = "Master username for the RDS instance."
  value       = aws_db_instance.postgres.username
}

output "rds_connection_string_ssm_param" {
  description = "SSM Parameter Store path that holds the DATABASE_URL SecureString (fetch with --with-decryption)."
  value       = aws_ssm_parameter.database_url.name
}

# ---------------------------------------------------------------------------
# Lambda → RDS Connection Exhaustion Scenario outputs
# ---------------------------------------------------------------------------

output "lambda_function_url" {
  description = "HTTPS Function URL for the conn-exhaust Lambda. Hit this with ab/curl to trigger the scenario."
  value       = aws_lambda_function_url.conn_exhaust.function_url
}

output "lambda_function_name" {
  description = "Lambda function name (use in CloudWatch console filter)."
  value       = aws_lambda_function.conn_exhaust.function_name
}

output "lambda_trigger_commands" {
  description = "Copy-paste commands to trigger the connection-exhaustion scenario."
  value = {
    single_request  = "curl -sS \"$${aws_lambda_function_url_conn_exhaust_function_url}\""
    load_test_linux = "ab -n 500 -c 60 \"$${aws_lambda_function_url_conn_exhaust_function_url}\""
    load_test_ps    = "1..60 | ForEach-Object -Parallel { Invoke-WebRequest -Uri '$${aws_lambda_function_url_conn_exhaust_function_url}' -UseBasicParsing } -ThrottleLimit 60"
    note            = "Replace the placeholder above with the actual lambda_function_url output value."
  }
}

resource "terraform_data" "debug_policy" {
  triggers_replace = [ timestamp() ]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOF
      aws lambda get-policy --function-name "${aws_lambda_function.conn_exhaust.function_name}" > lambda_policy.json || true
    EOF
  }
}
