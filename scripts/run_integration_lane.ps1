[CmdletBinding()]
param(
    [string[]]$PytestArgs = @(),
    [switch]$ListOnly,
    [switch]$FailIfSkipped
)

$ErrorActionPreference = "Stop"

$laneScript = Join-Path $PSScriptRoot "invoke_test_lane.ps1"
& $laneScript -Lane "integration" -PytestArgs $PytestArgs -ListOnly:$ListOnly -FailIfSkipped:$FailIfSkipped
exit $LASTEXITCODE
