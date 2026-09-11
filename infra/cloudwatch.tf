# ---------------------------------------------------------------------------
# Log groups - created here (not implicitly by the agent) so retention is set
# and `terraform destroy` removes them.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "system" {
  name              = local.log_group_system
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name_prefix}-system-logs" }
}

resource "aws_cloudwatch_log_group" "docker" {
  name              = local.log_group_docker
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name_prefix}-container-logs" }
}

# ---------------------------------------------------------------------------
# Metric filter — count ERROR-level log lines from the application.
#
# The app emits structured JSON logs. The filter matches any log event where
# the top-level "level" field equals "ERROR" (case-sensitive, matching the
# app’s exact output). Each matching log line increments ApplicationErrorCount
# by 1 in the project’s custom metrics namespace.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "app_errors" {
  name           = "${local.name_prefix}-app-error-count"
  log_group_name = aws_cloudwatch_log_group.docker.name

  # Pattern matches structured JSON logs where level == "ERROR".
  # The CloudWatch filter syntax for JSON fields uses { $.field = "value" }.
  pattern = "{ $.level = \"ERROR\" }"

  metric_transformation {
    name          = "ApplicationErrorCount"
    namespace     = local.metrics_namespace
    value         = "1"     # each matching log line counts as 1
    default_value = "0"     # emit 0 when no errors so the alarm can recover
    unit          = "Count"
  }
}

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alarms" {
  name = "${local.name_prefix}-alarms"

  tags = { Name = "${local.name_prefix}-alarms" }
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count = var.alarm_email == null ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarm_actions = [aws_sns_topic.alarms.arn]

  ec2_dimensions = { InstanceId = aws_instance.app.id }

  alb_dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
}



# ---------------------------------------------------------------------------
# Load balancer alarms
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  alarm_name          = "${local.name_prefix}-alb-unhealthy-hosts"
  alarm_description   = "The target group has no healthy target - /health is failing behind the ALB."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = local.alb_dimensions
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

# ---------------------------------------------------------------------------
# Application error alarm (from Metric Filter)
#
# Fires when the container log group emits >= 5 ERROR-level log lines in a
# 60-second window for 2 consecutive evaluation periods (~2 minutes sustained).
# Wired to the same SNS topic as all other alarms.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "app_error_rate" {
  alarm_name          = "${local.name_prefix}-application-error-rate"
  alarm_description   = "Application is emitting >=5 ERROR log lines per minute for 2 consecutive periods. Check container logs and /api/users."
  namespace           = local.metrics_namespace
  metric_name         = "ApplicationErrorCount"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.app_errors]
}



# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = "${local.name_prefix}-overview"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "EC2 - CPU utilization (%)"
          region  = var.aws_region
          view    = "timeSeries"
          stat    = "Average"
          period  = 300
          metrics = [["AWS/EC2", "CPUUtilization", "InstanceId", aws_instance.app.id]]
          yAxis   = { left = { min = 0, max = 100 } }
          annotations = {
            horizontal = [{ label = "alarm", value = var.cpu_alarm_threshold }]
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "EC2 - memory and root disk used (%)"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Average"
          period = 300
          metrics = [
            [local.metrics_namespace, "mem_used_percent", "InstanceId", aws_instance.app.id],
            [local.metrics_namespace, "disk_used_percent", "InstanceId", aws_instance.app.id],
          ]
          yAxis = { left = { min = 0, max = 100 } }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "ALB - requests and response codes"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.this.arn_suffix, "TargetGroup", aws_lb_target_group.app.arn_suffix],
            [".", "HTTPCode_Target_2XX_Count", ".", ".", ".", "."],
            [".", "HTTPCode_Target_4XX_Count", ".", ".", ".", "."],
            [".", "HTTPCode_Target_5XX_Count", ".", ".", ".", "."],
            ["AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "LoadBalancer", aws_lb.this.arn_suffix],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "ALB - target response time (s)"
          region = var.aws_region
          view   = "timeSeries"
          period = 300
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.this.arn_suffix, "TargetGroup", aws_lb_target_group.app.arn_suffix, { stat = "p50", label = "p50" }],
            ["...", { stat = "p95", label = "p95" }],
            ["...", { stat = "p99", label = "p99" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "ALB - target group health"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Maximum"
          period = 60
          metrics = [
            ["AWS/ApplicationELB", "HealthyHostCount", "LoadBalancer", aws_lb.this.arn_suffix, "TargetGroup", aws_lb_target_group.app.arn_suffix],
            [".", "UnHealthyHostCount", ".", ".", ".", "."],
          ]
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Container logs - errors"
          region = var.aws_region
          view   = "table"
          query  = "SOURCE '${local.log_group_docker}' | fields @timestamp, @message | filter @message like /(?i)(error|exception|traceback|critical)/ | sort @timestamp desc | limit 50"
        }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Database Connection Timeout Alarm
#
# Fires when the container log group emits a database connection timeout error.
# Wired to the same SNS topic as all other alarms.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "db_connection_timeouts" {
  name           = "${local.name_prefix}-db-timeout-count"
  log_group_name = aws_cloudwatch_log_group.docker.name

  # Matches string anywhere in the log event
  pattern = "\"connection_timeout\""

  metric_transformation {
    name          = "DatabaseConnectionTimeoutCount"
    namespace     = local.metrics_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "db_connection_timeout_rate" {
  alarm_name          = "${local.name_prefix}-db-connection-timeout-rate"
  alarm_description   = "Database connection timeouts detected. Check RDS health and database connections."
  namespace           = local.metrics_namespace
  metric_name         = "DatabaseConnectionTimeoutCount"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1  # Alert on any DB connection timeout
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.db_connection_timeouts]
}
