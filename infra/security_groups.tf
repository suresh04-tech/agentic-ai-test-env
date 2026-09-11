resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb-sg"
  description = "Public entry point for the ${var.project_name} ALB"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-alb-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  count = length(var.alb_allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from ${var.alb_allowed_cidrs[count.index]}"
  cidr_ipv4         = var.alb_allowed_cidrs[count.index]
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count = var.acm_certificate_arn == null ? 0 : length(var.alb_allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from ${var.alb_allowed_cidrs[count.index]}"
  cidr_ipv4         = var.alb_allowed_cidrs[count.index]
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to nginx on the app instance"
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "app" {
  name        = "${local.name_prefix}-app-sg"
  description = "EC2 instance running the ${var.project_name} Compose stack"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-app-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

# Only the ALB may reach nginx. Prometheus (9090), Loki (3100) and Grafana
# (3000) bind to 127.0.0.1 in docker-compose.yml and are unreachable from the
# network regardless; Grafana is served through nginx by Host header.
resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  security_group_id            = aws_security_group.app.id
  description                  = "nginx, from the ALB only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "app_ssh" {
  count = length(var.ssh_allowed_cidrs)

  security_group_id = aws_security_group.app.id
  description       = "SSH from ${var.ssh_allowed_cidrs[count.index]}"
  cidr_ipv4         = var.ssh_allowed_cidrs[count.index]
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

# Outbound is open: the instance pulls OS packages, Docker images and the
# application artifact, and reaches the external PostgreSQL host.
resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  description       = "Package, image, S3, SSM and database egress"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
