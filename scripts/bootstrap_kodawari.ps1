[CmdletBinding()]
param(
    [switch]$Recreate,
    [switch]$SkipPipUpgrade,
    [switch]$Smoke
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[bootstrap] $Message"
}

function Resolve-CommandPath {
    param([string]$CommandName)
    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        return $null
    }
    if ($cmd.Path) {
        return $cmd.Path
    }
    if ($cmd.Source -and (Test-Path $cmd.Source)) {
        return $cmd.Source
    }
    if ($cmd.Definition -and (Test-Path $cmd.Definition)) {
        return $cmd.Definition
    }
    return $null
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $repoRoot ".workflow_runtime"
$venvDir = Join-Path $runtimeRoot "local-env\.venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvWorkflowctl = Join-Path $venvDir "Scripts\kodawari.exe"

if ($Recreate -and (Test-Path $venvDir)) {
    Write-Step "Removing existing repo-local venv"
    Remove-Item -Path $venvDir -Recurse -Force
}

if (-not (Test-Path $venvPython)) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCmd) {
        throw "Python executable not found in current shell. Install Python 3.11+ and retry."
    }
    New-Item -Path (Split-Path -Path $venvDir -Parent) -ItemType Directory -Force | Out-Null
    Write-Step "Creating repo-local virtual environment at $venvDir"
    & python -m venv $venvDir
}

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment creation failed: $venvPython not found."
}

if (-not $SkipPipUpgrade) {
    Write-Step "Upgrading pip/setuptools/wheel inside repo-local venv"
    & $venvPython -m pip install --upgrade pip setuptools wheel
}

Write-Step "Installing kodawari in editable mode into repo-local venv"
& $venvPython -m pip install --editable $repoRoot

if (-not (Test-Path $venvWorkflowctl)) {
    throw "Bootstrap failed: $venvWorkflowctl was not generated."
}

Write-Step "Verifying runtime dependency import: jsonschema"
& $venvPython -c "import jsonschema" | Out-Null

if ($Smoke) {
    Write-Step "Running smoke check: kodawari --help"
    & $venvWorkflowctl --help | Out-Null
    Write-Step "Running smoke check: kodawari telemetry --help"
    & $venvWorkflowctl telemetry --help | Out-Null
}

$expectedWorkflowctl = (Resolve-Path $venvWorkflowctl).Path
$resolvedWorkflowctl = Resolve-CommandPath -CommandName "kodawari"
$resolvedWorkflowctlPath = $null
if ($resolvedWorkflowctl -and (Test-Path $resolvedWorkflowctl)) {
    $resolvedWorkflowctlPath = (Resolve-Path $resolvedWorkflowctl).Path
}

Write-Step "Done. Repo-local kodawari is ready."
Write-Host ""
Write-Host "Canonical repo-local usage:"
Write-Host "  .\scripts\kodawari.ps1 gate --help"
Write-Host "  .\scripts\kodawari.ps1 status --feature acceptance-smoke"
Write-Host "  .\scripts\kodawari.ps1 stability-report --help"
Write-Host ""
Write-Host "Direct repo-local venv binary (explicit):"
Write-Host "  $venvWorkflowctl gate --help"
Write-Host "  $venvWorkflowctl status --feature acceptance-smoke"
Write-Host "  $venvWorkflowctl stability-report --help"
Write-Host ""

if ($resolvedWorkflowctlPath) {
    if ($resolvedWorkflowctlPath -ieq $expectedWorkflowctl) {
        Write-Step "Current shell kodawari resolution is repo-local: $resolvedWorkflowctlPath"
    } else {
        Write-Host "[bootstrap] WARNING: current shell 'kodawari' resolves to: $resolvedWorkflowctlPath" -ForegroundColor Yellow
        Write-Host "[bootstrap]         This is not the repo-local executable: $expectedWorkflowctl" -ForegroundColor Yellow
        Write-Host "[bootstrap]         To avoid hitting stale installs, use '.\scripts\kodawari.ps1 ...' from this repo." -ForegroundColor Yellow
    }
} else {
    Write-Host "[bootstrap] NOTE: current shell has no resolvable 'kodawari' command." -ForegroundColor Yellow
    Write-Host "[bootstrap]       Use '.\scripts\kodawari.ps1 ...' or the explicit venv path above." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Optional session-only safeguard (current PowerShell window):"
Write-Host "  Set-Alias kodawari '$repoRoot\scripts\kodawari.ps1'"
