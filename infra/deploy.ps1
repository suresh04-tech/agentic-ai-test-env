<#
.SYNOPSIS
    Deploy the test-rca-app stack to AWS in one command.

.DESCRIPTION
    Runs terraform init + apply with no prompts, then prints the ALB URL and
    waits for /health to answer. Re-run it after changing application code:
    the bundle hash changes, which replaces the instance with the new build.

.EXAMPLE
    .\deploy.ps1

.EXAMPLE
    .\deploy.ps1 -SkipWait
#>
[CmdletBinding()]
param(
    # Skip polling the ALB for health after apply.
    [switch]$SkipWait,

    # How long to wait for the first healthy response.
    [int]$TimeoutMinutes = 12
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
    throw "terraform is not on PATH. Install it from https://developer.hashicorp.com/terraform/install"
}

if (-not (Test-Path 'terraform.tfvars')) {
    throw "terraform.tfvars not found. Copy terraform.tfvars.example to terraform.tfvars and fill in database_url and grafana_admin_password."
}

Write-Host "==> terraform init" -ForegroundColor Cyan
terraform init -input=false -upgrade
if ($LASTEXITCODE -ne 0) { throw "terraform init failed" }

Write-Host "==> terraform apply" -ForegroundColor Cyan
terraform apply -auto-approve -input=false
if ($LASTEXITCODE -ne 0) { throw "terraform apply failed" }

$url = (terraform output -raw application_url).Trim()

Write-Host ""
Write-Host "Application URL : $url" -ForegroundColor Green
Write-Host "Dashboard       : $((terraform output -raw cloudwatch_dashboard_url).Trim())"
Write-Host "Instance shell  : $((terraform output -raw ssm_session_command).Trim())"
Write-Host ""

if ($SkipWait) {
    Write-Host "Skipping the health wait. The instance still needs a few minutes to build its images."
    return
}

# The instance has to install Docker, pull base images and build the app image
# before the target group can turn healthy. Several minutes is normal.
Write-Host "==> Waiting for $url/health (up to $TimeoutMinutes min)" -ForegroundColor Cyan
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$healthy = $false

while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri "$url/health" -TimeoutSec 10 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    }
    catch {
        # 502/503 from the ALB while the target is still starting is expected.
    }
    Start-Sleep -Seconds 15
    Write-Host "." -NoNewline
}

Write-Host ""

if ($healthy) {
    Write-Host "Healthy. The application is live at $url" -ForegroundColor Green
}
else {
    Write-Warning "No healthy response within $TimeoutMinutes minutes."
    Write-Host "Check the bootstrap log:"
    Write-Host "  aws logs tail $((terraform output -raw system_log_group).Trim()) --region $((terraform output -raw region).Trim()) --since 20m"
    exit 1
}
