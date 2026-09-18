# ---------------------------------------------------------------------------
# Lambda — RDS Connection Exhaustion Scenario
#
# Creates a Lambda function that uses the connection-per-invocation
# anti-pattern to exhaust RDS max_connections under load.
#
# Resources created:
#   - aws_iam_role.lambda_conn_exhaust          — execution role
#   - aws_iam_role_policy.lambda_conn_exhaust   — inline policy (VPC + SSM)
#   - aws_cloudwatch_log_group.lambda_conn_exhaust
#   - terraform_data.lambda_pkg                — pip install + package build
#   - data.archive_file.lambda_conn_exhaust    — zips the build dir
#   - aws_lambda_function.conn_exhaust
#   - aws_lambda_function_url.conn_exhaust      — HTTPS URL (no auth)
#   - aws_security_group.lambda_conn_exhaust    — Lambda VPC SG
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# IAM execution role
# ---------------------------------------------------------------------------

resource "aws_iam_role" "lambda_conn_exhaust" {
  name = "${local.name_prefix}-lambda-conn-exhaust-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name_prefix}-lambda-conn-exhaust-role" }
}

resource "aws_iam_role_policy" "lambda_conn_exhaust" {
  name = "${local.name_prefix}-lambda-conn-exhaust-policy"
  role = aws_iam_role.lambda_conn_exhaust.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # CloudWatch Logs — write Lambda logs
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      # VPC networking — required for Lambda inside a VPC
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
          "ec2:AssignPrivateIpAddresses",
          "ec2:UnassignPrivateIpAddresses",
        ]
        Resource = "*"
      },
      # SSM — read the DATABASE_URL SecureString
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter${local.ssm_prefix}/*"
      },
      # KMS — decrypt SSM SecureStrings (if using a customer-managed key)
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# CloudWatch log group — pre-created so retention is set + destroyed cleanly
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda_conn_exhaust" {
  name              = "/aws/lambda/${local.name_prefix}-conn-exhaust"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name_prefix}-lambda-conn-exhaust-logs" }
}

# ---------------------------------------------------------------------------
# Lambda security group (VPC)
# ---------------------------------------------------------------------------

resource "aws_security_group" "lambda_conn_exhaust" {
  name        = "${local.name_prefix}-lambda-sg"
  description = "Lambda conn-exhaust function - allows DB egress only"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-lambda-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

# Lambda needs outbound to reach RDS and AWS services (SSM, CloudWatch)
resource "aws_vpc_security_group_egress_rule" "lambda_all" {
  security_group_id = aws_security_group.lambda_conn_exhaust.id
  description       = "All outbound - RDS, SSM, CloudWatch"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------------------------------------------------------------------------
# Allow Lambda SG → RDS SG on port 5432
# ---------------------------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "rds_from_lambda" {
  security_group_id            = aws_security_group.rds.id
  description                  = "PostgreSQL from Lambda conn-exhaust SG"
  referenced_security_group_id = aws_security_group.lambda_conn_exhaust.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# ---------------------------------------------------------------------------
# Build the deployment zip
#
# Step 1: terraform_data.lambda_pkg runs pip install via PowerShell local-exec.
#         Ensure Python 3 + pip are on PATH before running terraform apply.
#         The .build/ directory is gitignored.
#
# Step 2: data.archive_file zips the build dir using the archive provider
#         (already required in versions.tf).
#
# The resource re-provisions whenever the handler or requirements file changes.
# ---------------------------------------------------------------------------

locals {
  lambda_src_dir = "${path.module}/lambda"
  lambda_build   = "${path.module}/.build/lambda_pkg"
  lambda_zip     = "${path.module}/.build/lambda_conn_exhaust.zip"
}

resource "terraform_data" "lambda_pkg" {
  # Re-run when source files change
  triggers_replace = [
    filesha256("${local.lambda_src_dir}/connection_exhaust_handler.py"),
    filesha256("${local.lambda_src_dir}/requirements.txt"),
  ]

  provisioner "local-exec" {
    # Using bash since Terraform is running in WSL/Linux
    interpreter = ["bash", "-c"]
    command     = <<-EOF
      set -e
      buildDir="./.build/lambda_pkg"
      srcDir="./lambda"

      rm -rf "$buildDir"
      mkdir -p "$buildDir"

      if command -v pip3 &> /dev/null; then PIP_CMD="pip3"
      elif command -v pip &> /dev/null; then PIP_CMD="pip"
      elif command -v pip.exe &> /dev/null; then PIP_CMD="pip.exe"
      elif command -v python3 &> /dev/null; then PIP_CMD="python3 -m pip"
      elif command -v python.exe &> /dev/null; then PIP_CMD="python.exe -m pip"
      else
        echo "Could not find pip or python. Please install python3-pip."
        exit 1
      fi

      $PIP_CMD install --quiet --target "$buildDir" -r "$srcDir/requirements.txt" \
        --platform manylinux2014_x86_64 \
        --only-binary=:all: \
        --python-version 3.12 \
        --upgrade
      cp "$srcDir/connection_exhaust_handler.py" "$buildDir"
      
      echo "Lambda package built successfully using $PIP_CMD in: $buildDir"
    EOF
  }
}

data "archive_file" "lambda_conn_exhaust" {
  type        = "zip"
  source_dir  = local.lambda_build
  output_path = local.lambda_zip

  depends_on = [terraform_data.lambda_pkg]
}

# ---------------------------------------------------------------------------
# Lambda function
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "conn_exhaust" {
  function_name = "${local.name_prefix}-conn-exhaust"
  description   = "RCA test: connection-per-invocation anti-pattern against RDS"

  filename         = data.archive_file.lambda_conn_exhaust.output_path
  source_code_hash = data.archive_file.lambda_conn_exhaust.output_base64sha256
  handler          = "connection_exhaust_handler.lambda_handler"
  runtime          = "python3.12"

  role        = aws_iam_role.lambda_conn_exhaust.arn
  memory_size = 512
  timeout     = 30

  # Cap concurrency at 50 so a single load-test run maxes out RDS connections
  # without impacting other Lambda functions in the account.
  reserved_concurrent_executions = 50

  # VPC config — same VPC as RDS so private networking is used
  vpc_config {
    subnet_ids         = aws_subnet.public[*].id
    security_group_ids = [aws_security_group.lambda_conn_exhaust.id]
  }

  environment {
    variables = {
      # DATABASE_URL is fetched from SSM at Terraform apply time and injected
      # as a plain-text env var (Lambda encrypts env vars at rest with KMS).
      DATABASE_URL    = aws_ssm_parameter.database_url.value
      HOLD_SECONDS    = "8"     # hold each connection 8 s — keeps conns occupied
      CONNECT_TIMEOUT = "10"    # seconds before giving up a new connection
      LOG_LEVEL       = "INFO"
    }
  }

  # Ensure the log group exists before Lambda creates it
  depends_on = [
    aws_cloudwatch_log_group.lambda_conn_exhaust,
    terraform_data.lambda_pkg,
    aws_iam_role_policy.lambda_conn_exhaust,
  ]

  tags = { Name = "${local.name_prefix}-conn-exhaust" }
}

# ---------------------------------------------------------------------------
# Function URL - HTTPS endpoint, no auth required
# Makes it trivially easy to hit with curl / ab / PowerShell Invoke-WebRequest
# ---------------------------------------------------------------------------

resource "aws_lambda_function_url" "conn_exhaust" {
  function_name      = aws_lambda_function.conn_exhaust.function_name
  authorization_type = "NONE"

  cors {
    allow_origins = ["*"]
    allow_methods = ["GET"]
  }
}

resource "aws_lambda_permission" "allow_public_function_url" {
  statement_id_prefix    = "FunctionURLAllowPublicAccess-"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.conn_exhaust.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
