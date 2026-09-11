# Latest Amazon Linux 2023 AMI for the region, resolved from the public SSM
# parameter AWS maintains — no hardcoded, drifting AMI IDs.
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    project_name           = var.project_name
    environment            = var.environment
    aws_region             = var.aws_region
    artifact_bucket        = aws_s3_bucket.artifacts.id
    bundle_key             = aws_s3_object.app.key
    docker_compose_version = var.docker_compose_version
    cw_agent_parameter     = aws_ssm_parameter.cloudwatch_agent_config.name
    ssm_db_url             = aws_ssm_parameter.database_url.name
    ssm_grafana_password   = aws_ssm_parameter.grafana_admin_password.name
    app_env_block          = local.app_env_block
  })
}

resource "aws_instance" "app" {
  ami           = data.aws_ssm_parameter.al2023_ami.value
  instance_type = var.instance_type
  subnet_id     = aws_subnet.public[0].id
  key_name      = var.key_pair_name

  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  associate_public_ip_address = true
  monitoring                  = var.enable_detailed_monitoring

  user_data = local.user_data

  # A code change changes the bundle key, which changes user data, which
  # rebuilds the instance. Deploying an update stays one `terraform apply`.
  user_data_replace_on_change = true

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size
    encrypted             = true
    delete_on_termination = true

    tags = { Name = "${local.name_prefix}-root" }
  }

  # IMDSv2 only, and a hop limit of 1 so containers on the instance cannot
  # reach the metadata service and borrow the instance role.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  tags = { Name = "${local.name_prefix}-app" }

  # Replace the EC2 instance whenever the DATABASE_URL SSM parameter changes.
  # The boot script reads DATABASE_URL from SSM only once, so without this
  # the running instance would keep the stale connection string indefinitely.
  lifecycle {
    replace_triggered_by = [
      aws_ssm_parameter.database_url,
    ]
  }

  depends_on = [
    aws_iam_role_policy.instance,
    aws_iam_role_policy_attachment.ssm_core,
    aws_iam_role_policy_attachment.cloudwatch_agent,
    aws_route_table_association.public,
    aws_cloudwatch_log_group.system,
    aws_cloudwatch_log_group.docker,
    # Ensure the database is ready before the EC2 instance boots and tries to connect.
    aws_db_instance.postgres,
    aws_ssm_parameter.database_url,
  ]
}
