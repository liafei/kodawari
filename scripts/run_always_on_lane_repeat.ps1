[CmdletBinding()]
param(
    [int]$Repeat = 3,
    [string]$SummaryPath = "",
    [string[]]$PytestArgs = @(),
    [switch]$FailFast
)

$ErrorActionPreference = "Stop"

$stabilityScript = Join-Path $PSScriptRoot "run_lane_stability.ps1"
& $stabilityScript -Lane "always-on" -Repeat $Repeat -SummaryPath $SummaryPath -PytestArgs $PytestArgs -FailFast:$FailFast
exit $LASTEXITCODE
