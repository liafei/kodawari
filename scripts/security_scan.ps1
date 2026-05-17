[CmdletBinding()]
param(
    [switch]$SkipGitleaksVersionCheck,
    [switch]$SkipDetectSecretsVersionCheck
)

$ErrorActionPreference = "Stop"

$GitleaksVersion = "8.24.3"
$DetectSecretsVersion = "1.5.0"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeDir = Join-Path $repoRoot ".workflow_runtime\security"
$gitleaksReport = Join-Path $runtimeDir "gitleaks-report.json"
$detectSecretsReport = Join-Path $runtimeDir "detect-secrets-report.json"
$excludePattern = "(\.workflow_runtime|\.workflow|\.workflow_real_runs|\.tmp|\.venv|\.venv_probe|\.pytest_cache|node_modules|web[\\/]dist|web[\\/]src-tauri[\\/]target)"

function Require-Command {
    param([string]$Name, [string]$InstallHint)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        throw "$Name is not installed. $InstallHint"
    }
}

function Assert-Version {
    param([string]$Name, [string]$Expected, [string]$Actual)
    if ($Actual -notmatch [regex]::Escape($Expected)) {
        throw "$Name version mismatch: expected $Expected, got '$Actual'"
    }
}

New-Item -Path $runtimeDir -ItemType Directory -Force | Out-Null

Require-Command -Name "gitleaks" -InstallHint "Install pinned version: go install github.com/gitleaks/gitleaks/v8@v$GitleaksVersion"
Require-Command -Name "detect-secrets" -InstallHint "Install pinned version: python -m pip install detect-secrets==$DetectSecretsVersion"

if (-not $SkipGitleaksVersionCheck) {
    $gitleaksVersionText = (& gitleaks version) -join "`n"
    Assert-Version -Name "gitleaks" -Expected $GitleaksVersion -Actual $gitleaksVersionText
}
if (-not $SkipDetectSecretsVersionCheck) {
    $detectVersionText = (& detect-secrets --version) -join "`n"
    Assert-Version -Name "detect-secrets" -Expected $DetectSecretsVersion -Actual $detectVersionText
}

Push-Location $repoRoot
try {
    & gitleaks detect --no-git --redact --source . --config .gitleaks.toml --report-format json --report-path $gitleaksReport
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    & detect-secrets scan --all-files --baseline .secrets.baseline --exclude-files $excludePattern . | Out-File -FilePath $detectSecretsReport -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}

Write-Host "security scan passed"
Write-Host "gitleaks report: $gitleaksReport"
Write-Host "detect-secrets report: $detectSecretsReport"
