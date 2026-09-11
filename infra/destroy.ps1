<#
.SYNOPSIS
    Tear down the entire test-rca-app stack in one command.

.DESCRIPTION
    Destroys everything this module created: VPC, EC2 instance, ALB, target
    group, S3 artifact bucket, SSM parameters, IAM role, CloudWatch log groups,
    alarms and dashboard. Nothing is shared with other stacks, so nothing is
    left behind.

    The external PostgreSQL database is NOT touched - it is not managed here.

.EXAMPLE
    .\destroy.ps1

.EXAMPLE
    .\destroy.ps1 -Force
#>
[CmdletBinding()]
param(
    # Skip the typed confirmation.
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
    throw "terraform is not on PATH."
}

if (-not $Force) {
    Write-Host "This destroys the whole stack: EC2 instance, ALB, VPC, S3 bucket," -ForegroundColor Yellow
    Write-Host "SSM parameters, CloudWatch log groups, alarms and dashboard." -ForegroundColor Yellow
    Write-Host "Container logs and Prometheus/Loki/Grafana data on the instance are lost." -ForegroundColor Yellow
    Write-Host ""
    $answer = Read-Host "Type 'destroy' to continue"
    if ($answer -ne 'destroy') {
        Write-Host "Aborted."
        return
    }
}

Write-Host "==> terraform destroy" -ForegroundColor Cyan
terraform destroy -auto-approve -input=false
if ($LASTEXITCODE -ne 0) { throw "terraform destroy failed" }

# Local build artifact - regenerated on the next deploy.
if (Test-Path '.build') { Remove-Item -Recurse -Force '.build' }

Write-Host ""
Write-Host "Stack destroyed. The external PostgreSQL database was not touched." -ForegroundColor Green
