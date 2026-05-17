[CmdletBinding()]
param(
    [int]$Repeat = 3,
    [string]$SummaryPath = "",
    [string[]]$PytestArgs = @(),
    [switch]$FailFast,
    [switch]$FailIfSkipped
)

$ErrorActionPreference = "Stop"

$stabilityScript = Join-Path $PSScriptRoot "run_lane_stability.ps1"
& $stabilityScript -Lane "integration" -Repeat $Repeat -SummaryPath $SummaryPath -PytestArgs $PytestArgs -FailFast:$FailFast -FailIfSkipped:$FailIfSkipped
exit $LASTEXITCODE
