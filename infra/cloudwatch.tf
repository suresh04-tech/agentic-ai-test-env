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
      # ── Row 1: EC2 ─────────────────────────────────────────────────────────
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
      # ── Row 2: ALB ─────────────────────────────────────────────────────────
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
      # ── Row 3: ALB health + container logs ─────────────────────────────────
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
      # ── Row 4: Lambda → RDS Connection Exhaustion Scenario ─────────────────
      {
        type   = "metric"
        x      = 0
        y      = 18
        width  = 12
        height = 6
        properties = {
          title  = "[RCA Scenario] Lambda - Errors (Alarm 1: symptom)"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.conn_exhaust.function_name, { label = "Errors", color = "#d62728" }],
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.conn_exhaust.function_name, { label = "Invocations", color = "#1f77b4" }],
          ]
          annotations = {
            horizontal = [{ label = "alarm threshold", value = 1, color = "#d62728" }]
          }
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 18
        width  = 12
        height = 6
        properties = {
          title  = "[RCA Scenario] Lambda - Duration (Alarm 2: leading indicator)"
          region = var.aws_region
          view   = "timeSeries"
          period = 60
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.conn_exhaust.function_name, { stat = "Average", label = "avg" }],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.conn_exhaust.function_name, { stat = "p95", label = "p95", color = "#ff7f0e" }],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.conn_exhaust.function_name, { stat = "Maximum", label = "max", color = "#d62728" }],
          ]
          annotations = {
            horizontal = [{ label = "alarm threshold (8 s)", value = 8000, color = "#ff7f0e" }]
          }
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 24
        width  = 12
        height = 6
        properties = {
          title  = "[RCA Scenario] RDS - DatabaseConnections (Alarm 3: ROOT CAUSE)"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Maximum"
          period = 60
          metrics = [
            ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", aws_db_instance.postgres.identifier, { label = "Connections (Max)", color = "#d62728" }],
          ]
          annotations = {
            horizontal = [
              { label = "alarm threshold (70)", value = 70, color = "#ff7f0e" },
              { label = "approx max_connections (~87)", value = 87, color = "#d62728" },
            ]
          }
          yAxis = { left = { min = 0 } }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 24
        width  = 12
        height = 6
        properties = {
          title  = "[RCA Scenario] RDS - CPUUtilization (Alarm 4: correlation evidence)"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Average"
          period = 60
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.postgres.identifier, { label = "CPU %", color = "#ff7f0e" }],
          ]
          annotations = {
            horizontal = [{ label = "alarm threshold (40%)", value = 40, color = "#ff7f0e" }]
          }
          yAxis = { left = { min = 0, max = 100 } }
        }
      },
      # ── Row 5: Lambda "too many connections" log metric ─────────────────────
      {
        type   = "metric"
        x      = 0
        y      = 30
        width  = 24
        height = 6
        properties = {
          title  = "[RCA Scenario] Lambda - 'too many connections' errors from logs"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Sum"
          period = 60
          metrics = [
            [local.metrics_namespace, "LambdaDBTooManyConnections", { label = "too_many_connections errors", color = "#d62728" }],
          ]
          yAxis = { left = { min = 0 } }
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

# ---------------------------------------------------------------------------
# RDS CPU Utilization Alarm
#
# Fires when the RDS instance CPU utilization is >= 50%.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${local.name_prefix}-rds-cpu-high"
  alarm_description   = "RDS CPU utilization is >= 50%"
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 1
  threshold           = 50
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.postgres.identifier
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}


# ===========================================================================
# Lambda → RDS Connection Exhaustion Scenario Alarms
#
# All 4 alarms are tuned for fastest possible ALARM state during testing:
#   - period             = 60 s   (minimum CloudWatch resolution)
#   - evaluation_periods = 1      (fire on the very first bad window)
#   - threshold          = 1 or   lowest meaningful value
#
# RCA story: Alarm 1 (Lambda Errors) is the symptom.
#            Alarm 3 (RDS DatabaseConnections) is the root cause.
#            Alarm 2 (Lambda Duration) is the leading indicator.
#            Alarm 4 (RDS CPU) is correlation evidence.
# ===========================================================================


# ---------------------------------------------------------------------------
# Lambda log group metric filter — extract "too many connections" events
# so a custom metric can be graphed alongside the native Lambda Errors metric.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "lambda_too_many_connections" {
  name           = "${local.name_prefix}-lambda-too-many-conn"
  log_group_name = aws_cloudwatch_log_group.lambda_conn_exhaust.name

  # Match the exact JSON field emitted by the handler on OperationalError
  pattern = "\"too many connections\""

  metric_transformation {
    name          = "LambdaDBTooManyConnections"
    namespace     = local.metrics_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }

  depends_on = [aws_cloudwatch_log_group.lambda_conn_exhaust]
}


# ---------------------------------------------------------------------------
# Alarm 1 — Lambda Errors (the visible symptom)
#
# Native AWS/Lambda Errors metric counts invocations that threw an exception.
# Under connection exhaustion every concurrent invocation above the RDS limit
# will fail, so even 1 error is a strong signal.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name        = "${local.name_prefix}-lambda-conn-exhaust-errors"
  alarm_description = <<-EOT
    Lambda conn-exhaust function errors ≥ 1 in 60 s.
    SYMPTOM — not the root cause. Trace to RDS DatabaseConnections alarm.
    Likely cause: psycopg OperationalError: too many connections.
  EOT

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.conn_exhaust.function_name
  }

  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}


# ---------------------------------------------------------------------------
# Alarm 2 — Lambda Duration p95 (leading indicator / latency signal)
#
# Normal: ~300 ms (fast SELECT + 8 s hold = Lambda overhead only)
# Incident: 8,000–15,000 ms (connect timeout waiting for a free connection)
#
# p95 catches the tail without being noisy from occasional cold starts.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "lambda_duration_p95" {
  alarm_name        = "${local.name_prefix}-lambda-conn-exhaust-duration-p95"
  alarm_description = <<-EOT
    Lambda conn-exhaust p95 duration ≥ 8000 ms (8 s).
    LEADING INDICATOR — connection attempts are timing out.
    Under normal load the function completes in ~8–9 s (8 s hold + overhead).
    Threshold crossed when connect_timeout triggers before a slot is available.
  EOT

  namespace   = "AWS/Lambda"
  metric_name = "Duration"
  dimensions = {
    FunctionName = aws_lambda_function.conn_exhaust.function_name
  }

  extended_statistic  = "p95"
  period              = 60
  evaluation_periods  = 1
  threshold           = 8000
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}


# ---------------------------------------------------------------------------
# Alarm 3 — RDS DatabaseConnections (the ROOT CAUSE)
#
# db.t3.micro = 1 GiB RAM → max_connections ≈ 87 (PostgreSQL default formula:
#   LEAST({DBInstanceClassMemory/9531392}, 5000))
# EC2 app pool: 5 (pool_size) + 5 (max_overflow) = up to 10 connections.
# Lambda at 50 concurrency: 50 connections.
# Total peak: 60 — exceeds the ~87 limit when traffic spikes further.
#
# Threshold = 70 ≈ 80% of 87 — fires before exhaustion to give RCA lead time.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "rds_connections_high" {
  alarm_name        = "${local.name_prefix}-rds-conn-exhaust-connections-high"
  alarm_description = <<-EOT
    RDS DatabaseConnections ≥ 70 (≈ 80% of db.t3.micro max ≈ 87).
    ROOT CAUSE — connection exhaustion is underway.
    Lambda anti-pattern (new conn per invocation) is filling the connection limit.
    Resolution: add RDS Proxy, or reuse module-level connections in Lambda.
  EOT

  namespace   = "AWS/RDS"
  metric_name = "DatabaseConnections"
  dimensions = {
    DBInstanceIdentifier = aws_db_instance.postgres.identifier
  }

  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 70
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}


# ---------------------------------------------------------------------------
# Alarm 4 — RDS CPU (correlation / secondary evidence)
#
# Connection management overhead (auth, TCP handshakes, session setup × 50)
# causes a measurable CPU bump even though the queries themselves are trivial.
# This alarm should not be the primary signal — it confirms the RCA.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "rds_cpu_conn_exhaust" {
  alarm_name        = "${local.name_prefix}-rds-conn-exhaust-cpu"
  alarm_description = <<-EOT
    RDS CPUUtilization ≥ 40% during Lambda connection-exhaustion test.
    CORRELATION EVIDENCE — high CPU from connection management overhead,
    not query load. Confirms the root cause is connection exhaustion,
    not a slow/expensive query.
  EOT

  namespace   = "AWS/RDS"
  metric_name = "CPUUtilization"
  dimensions = {
    DBInstanceIdentifier = aws_db_instance.postgres.identifier
  }

  statistic           = "Average"
  period              = 60
  evaluation_periods  = 1
  threshold           = 40
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}