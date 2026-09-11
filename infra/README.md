# Infrastructure — `test-rca-app` on EC2 behind an ALB

Terraform that builds the AWS environment for the application in this
repository and deploys it, in one command. One more command tears it all back
down.

```
Internet ──▶ ALB :80/:443 ──▶ EC2 :80 (nginx) ──▶ app :8080 ──▶ external PostgreSQL
                                  │
                                  ├── Prometheus / Loki / Alloy / Grafana / node-exporter
                                  └── CloudWatch agent ──▶ metrics, logs, alarms, dashboard
```

---

## What gets created

| Area | Resources |
|---|---|
| Network | VPC, internet gateway, two public subnets across two AZs, route table, locked-down default SG |
| Compute | One EC2 instance (Amazon Linux 2023, gp3 encrypted root, IMDSv2 required), instance profile |
| Load balancing | Application Load Balancer, HTTP target group on port 80 with a `/health` check, HTTP listener, optional HTTPS listener |
| Delivery | S3 artifact bucket + zipped application bundle |
| Config | SSM SecureStrings for `DATABASE_URL` and the Grafana password, plus the CloudWatch agent config |
| Observability | Two CloudWatch log groups, eight alarms, an SNS topic, and a dashboard |

Everything is self-contained. Nothing is borrowed from the default VPC, so
`destroy` leaves nothing behind.

**Not created:** the PostgreSQL database. The application connects to an
external one, and this module never touches it.

---

## Prerequisites

- Terraform >= 1.5
- AWS credentials with permission to create the resources above
- A reachable PostgreSQL database and its connection string

---

## Deploy

```bash
cp terraform.tfvars.example terraform.tfvars
```

Fill in at minimum `aws_region`, `database_url`, and `grafana_admin_password`.

Then, from the `infra/` directory:

```powershell
.\deploy.ps1
```

or, with GNU make:

```bash
make deploy
```

or plain Terraform:

```bash
terraform init && terraform apply -auto-approve
```

`apply` returns in about two minutes. The instance then needs another three to
five to install Docker, pull images and build the app image before the target
group turns healthy — `deploy.ps1` waits for that and tells you when it is up.

## Destroy

```powershell
.\destroy.ps1
```

```bash
make destroy
```

```bash
terraform destroy -auto-approve
```

---

## Redeploying after a code change

Edit the application, then run `deploy.ps1` again. Terraform re-zips the
working tree; a changed bundle changes the S3 key, which changes the EC2 user
data, which replaces the instance with one running the new build. No commit, no
push, no manual SSH.

The tradeoff is that a code change costs an instance replacement (a few
minutes) and wipes the Prometheus/Loki/Grafana volumes on that instance. For
faster iteration on a live box, use `make shell` and run
`cd /opt/app && docker compose up -d --build` directly.

---

## Reaching things

| What | How |
|---|---|
| Application | `terraform output application_url` |
| Shell on the instance | `make shell`, or `aws ssm start-session --target <instance-id>` |
| CloudWatch dashboard | `terraform output cloudwatch_dashboard_url` |
| Container logs | `make logs` |
| Bootstrap / cloud-init logs | `make boot-logs` |
| Grafana | see below |
| Prometheus, Loki | localhost-only on the instance — SSH tunnel or Session Manager port forwarding |

### Grafana

`nginx.conf` routes by `Host` header: `grafana.<your-domain>` goes to Grafana,
everything else goes to the app. The ALB forwards the `Host` header unchanged,
so pointing that subdomain at the ALB is all that is needed:

```
grafana.example.com  CNAME  <terraform output alb_dns_name>
```

The subdomain nginx matches is hardcoded in [`nginx.conf`](../nginx.conf) —
change it there if your domain differs. Grafana's `GF_SERVER_ROOT_URL` in
[`docker-compose.yml`](../docker-compose.yml) needs the same value.

---

## CloudWatch

**Metrics.** Native EC2 metrics (CPU, network, status checks) plus memory,
swap, disk, disk I/O and netstat from the CloudWatch agent, published to the
`<project>/<environment>` namespace. Detailed (1-minute) monitoring is on by
default.

**Logs.** Two groups, retention set by `log_retention_days`:

- `/<project>/<environment>/system` — `bootstrap.log`, `cloud-init-output.log`, `/var/log/messages`
- `/<project>/<environment>/containers` — every container's stdout and stderr

**Alarms.** All eight publish to one SNS topic; set `alarm_email` to receive
them (AWS sends a confirmation link you must click).

| Alarm | Fires when |
|---|---|
| `ec2-cpu-high` | CPU > 80% for 10 min |
| `ec2-status-check-failed` | Either EC2 status check fails for 2 min |
| `ec2-memory-high` | Memory > 85% for 10 min |
| `ec2-disk-high` | Root volume > 85% for 10 min |
| `alb-unhealthy-hosts` | Any unhealthy target for 3 min |
| `alb-target-5xx` | > 10 application 5xx in 5 min |
| `alb-elb-5xx` | > 5 load-balancer 5xx in 5 min |
| `alb-target-latency` | p95 response time > 2s for 10 min |

The app's own failure-injection endpoints (`/api/cpu-stress`, the DB failure
simulation, slow queries) will trip these on purpose — that is the point of the
environment, not a misconfiguration.

**Dashboard.** `<project>-<environment>-overview`: CPU, memory and disk,
request counts by response code, p50/p95/p99 latency, target health, and a
Logs Insights panel filtering container logs for errors.

---

## Security notes

- Secrets go to SSM Parameter Store as SecureStrings and are fetched at boot.
  They are never written into EC2 user data, which is readable through IMDS and
  by anyone holding `ec2:DescribeInstanceAttribute`.
- The instance security group accepts port 80 **only** from the ALB security
  group. No inbound SSH rule exists unless you set `ssh_allowed_cidrs`, and the
  variable refuses `0.0.0.0/0`. Use Session Manager instead.
- IMDSv2 is required and the hop limit is 1, so containers on the instance
  cannot reach the metadata service and assume the instance role.
- The instance role is scoped to this stack's own S3 prefix and SSM parameters,
  plus the two AWS-managed policies needed for Session Manager and the
  CloudWatch agent.
- Root EBS volume and S3 bucket are encrypted; the bucket blocks all public
  access.
- The instance sits in a public subnet with a public IP. That is a deliberate
  cost tradeoff — a private subnet would need a NAT gateway (~$32/month) purely
  to pull container images. Set `associate_public_ip_address` to `false` and add
  a NAT gateway if that tradeoff does not suit you.

### Terraform state contains secrets

`database_url` and `grafana_admin_password` are stored in the state file.
Keep it local and gitignored (the default here), or uncomment the S3 backend in
[`versions.tf`](versions.tf) and use an encrypted bucket. Never commit
`terraform.tfstate` or `terraform.tfvars`.

---

## Rough monthly cost

At `ap-south-1` on-demand pricing, idle:

| Item | Approx. |
|---|---|
| t3.medium, 24/7 | ~$30 |
| ALB (no traffic) | ~$18 |
| 30 GB gp3 | ~$2.50 |
| CloudWatch (detailed monitoring, agent metrics, logs, alarms) | ~$5–10 |
| S3, SSM, SNS | < $1 |

Roughly **$55–60/month**. `destroy.ps1` takes it to zero — which is the point
of keeping the whole thing in one destroyable stack.

---

## Layout

```
infra/
├── versions.tf              provider + backend
├── variables.tf             all inputs
├── locals.tf                naming, bundle file list, .env defaults
├── network.tf               VPC, subnets, IGW, routing
├── security_groups.tf       ALB and instance SGs
├── iam.tf                   instance role, scoped policy, profile
├── artifact.tf              zip the working tree, S3 bucket + object
├── ssm.tf                   SecureStrings + CloudWatch agent config
├── ec2.tf                   AMI lookup, instance, user data
├── alb.tf                   ALB, target group, listeners
├── cloudwatch.tf            log groups, alarms, SNS, dashboard
├── outputs.tf               URLs, IDs, helper commands
├── deploy.ps1 / destroy.ps1 single-command wrappers (Windows)
├── Makefile                 single-command wrappers (make)
└── templates/
    ├── user_data.sh.tftpl        instance bootstrap
    └── cloudwatch_agent.json.tftpl
```

---

## Troubleshooting

**Target group stuck unhealthy.** Give it five minutes from `apply`; the image
build is the slow part. Then:

```bash
make boot-logs
```

Look for the `[bootstrap]` lines. A `WARNING: /health did not answer in time`
means the containers came up but the app did not — check `make logs`.

**App is up, API calls return 500.** Almost always the database. Open a shell
and check that the connection string resolves:

```bash
curl -s localhost/api/db-check
```

It reports the target host and port without credentials.

**`terraform destroy` leaves the S3 bucket.** It should not — `force_destroy`
is set. If a destroy was interrupted mid-way, re-run it.
