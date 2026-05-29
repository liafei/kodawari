[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$invocationCwd = (Get-Location).Path
$venvWorkflowctl = Join-Path $repoRoot ".workflow_runtime\local-env\.venv\Scripts\kodawari.exe"
$bootstrapScript = Join-Path $repoRoot "scripts\bootstrap_kodawari.ps1"
$srcPath = Join-Path $repoRoot "src"

$env:WORKFLOWCTL_REPO_ROOT = $repoRoot
$env:WORKFLOWCTL_WRAPPER = $PSCommandPath
$env:WORKFLOWCTL_INVOCATION_CWD = $invocationCwd
$env:WORKFLOWCTL_CANONICAL_WRAPPER = $PSCommandPath
$env:PYTHONPATH = $srcPath

if (-not (Test-Path $venvWorkflowctl)) {
    powershell -ExecutionPolicy Bypass -File `"$bootstrapScript`"
}

if (Test-Path $venvWorkflowctl) {
    & $venvWorkflowctl @Args
    exit $LASTEXITCODE
}

& python -m kodawari.cli.main @Args
exit $LASTEXITCODE

