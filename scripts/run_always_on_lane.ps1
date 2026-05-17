[CmdletBinding()]
param(
    [string[]]$PytestArgs = @(),
    [switch]$ListOnly
)

$ErrorActionPreference = "Stop"

$laneScript = Join-Path $PSScriptRoot "invoke_test_lane.ps1"
& $laneScript -Lane "always-on" -PytestArgs $PytestArgs -ListOnly:$ListOnly
exit $LASTEXITCODE
