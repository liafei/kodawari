[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("always-on", "integration", "real-review-success", "real-review-fail-closed", "models-v2-workall-real")]
    [string]$Lane,

    [string[]]$PytestArgs = @(),

    [switch]$ListOnly,

    [switch]$FailIfSkipped,

    [string]$ResultPath = ""
)

$ErrorActionPreference = "Stop"

function Write-LaneStep {
    param([string]$Message)
    Write-Host "[lane:$Lane] $Message"
}

function Write-Utf8Json {
    param(
        [string]$Path,
        [object]$Payload
    )

    $json = $Payload | ConvertTo-Json -Depth 6
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $encoding)
}

function Write-LaneResult {
    param(
        [string]$Status,
        [int]$ExitCode,
        [string]$Message,
        [string[]]$MissingEnv = @(),
        [string[]]$PytestCommand = @(),
        [string[]]$PytestTargets = @()
    )

    if ([string]::IsNullOrWhiteSpace($ResultPath)) {
        return
    }

    $resultDir = Split-Path -Path $ResultPath -Parent
    if (-not [string]::IsNullOrWhiteSpace($resultDir)) {
        New-Item -Path $resultDir -ItemType Directory -Force | Out-Null
    }

    $payload = @{
        schema_version = "lane.run_result.v1"
        lane = $Lane
        status = $Status
        exit_code = $ExitCode
        message = $Message
        summary = [string]$recipe.summary
        missing_env = @($MissingEnv)
        pytest_targets = @($PytestTargets)
        pytest_command = @($PytestCommand)
        fail_if_skipped = [bool]$FailIfSkipped
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    }
    Write-Utf8Json -Path $ResultPath -Payload $payload
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$recipePath = Join-Path $PSScriptRoot "test_lane_recipes.json"

if (-not (Test-Path $recipePath)) {
    throw "Lane recipe file not found: $recipePath"
}

$recipes = Get-Content -LiteralPath $recipePath -Raw | ConvertFrom-Json
$recipe = $recipes.$Lane
if ($null -eq $recipe) {
    throw "Lane recipe '$Lane' is not defined in $recipePath"
}

$defaultPytestArgs = @($recipe.default_pytest_args | ForEach-Object { [string]$_ })
$pytestTargets = @($recipe.pytest_targets | ForEach-Object { [string]$_ })
$pytestCommand = @("-m", "pytest") + $defaultPytestArgs + $pytestTargets + $PytestArgs

$missingEnv = @()
foreach ($name in @($recipe.skip_if_env_missing)) {
    $value = [Environment]::GetEnvironmentVariable([string]$name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        $missingEnv += [string]$name
    }
}

if ($missingEnv.Count -gt 0) {
    $message = "skipped because required integration environment is incomplete: $($missingEnv -join ', ')"
    if ($FailIfSkipped) {
        Write-LaneResult -Status "FAIL" -ExitCode 1 -Message $message -MissingEnv $missingEnv -PytestCommand $pytestCommand -PytestTargets $pytestTargets
        throw $message
    }
    Write-Host "[lane:$Lane] SKIP $message" -ForegroundColor Yellow
    Write-LaneResult -Status "SKIP" -ExitCode 0 -Message $message -MissingEnv $missingEnv -PytestCommand $pytestCommand -PytestTargets $pytestTargets
    exit 0
}

$runtimeVenvPython = Join-Path $repoRoot ".workflow_runtime\local-env\.venv\Scripts\python.exe"
$legacyVenvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$venvPython = if (Test-Path $runtimeVenvPython) { $runtimeVenvPython } else { $legacyVenvPython }
$pythonPath = $null

if (Test-Path $venvPython) {
    $pythonPath = $venvPython
    Write-LaneStep "using repo-local python: $pythonPath"
} else {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCmd) {
        throw "Python executable not found. Bootstrap with .\scripts\bootstrap_kodawari.ps1 first."
    }
    $pythonPath = if ($pythonCmd.Path) { $pythonCmd.Path } else { $pythonCmd.Source }
    Write-LaneStep "using shell python fallback: $pythonPath"
}

Write-LaneStep "summary: $($recipe.summary)"
Write-LaneStep "pytest targets:"
foreach ($target in $pytestTargets) {
    Write-Host "  - $target"
}

if ($ListOnly) {
    Write-LaneResult -Status "LIST_ONLY" -ExitCode 0 -Message "listed pytest targets without execution" -PytestCommand $pytestCommand -PytestTargets $pytestTargets
    exit 0
}

Write-LaneStep "executing: $pythonPath $($pytestCommand -join ' ')"

Push-Location $repoRoot
$previousSummaryPath = [Environment]::GetEnvironmentVariable("WORKFLOW_SDK_PYTEST_SUMMARY_JSON")
$summaryPath = Join-Path $repoRoot "planning\pytest_summary_latest.json"
try {
    [Environment]::SetEnvironmentVariable("WORKFLOW_SDK_PYTEST_SUMMARY_JSON", $summaryPath, "Process")
    & $pythonPath @pytestCommand
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    $status = if ($exitCode -eq 0) { "PASS" } else { "FAIL" }
    $message = if ($exitCode -eq 0) { "lane completed" } else { "pytest exited with code $exitCode" }
    Write-LaneResult -Status $status -ExitCode $exitCode -Message $message -PytestCommand $pytestCommand -PytestTargets $pytestTargets
    exit $exitCode
} finally {
    [Environment]::SetEnvironmentVariable("WORKFLOW_SDK_PYTEST_SUMMARY_JSON", $previousSummaryPath, "Process")
    Pop-Location
}
