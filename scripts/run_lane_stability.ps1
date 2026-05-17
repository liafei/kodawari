[CmdletBinding()]
param(
    [ValidateSet("always-on", "integration", "real-review-success", "real-review-fail-closed", "models-v2-workall-real")]
    [string]$Lane = "always-on",
    [int]$Repeat = 3,
    [string]$SummaryPath = "",
    [string[]]$PytestArgs = @(),
    [switch]$FailFast,
    [switch]$FailIfSkipped
)

$ErrorActionPreference = "Stop"

function Write-Utf8Json {
    param(
        [string]$Path,
        [object]$Payload
    )

    $json = $Payload | ConvertTo-Json -Depth 6
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $encoding)
}

function Write-Utf8Text {
    param(
        [string]$Path,
        [string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Get-LaneCommands {
    param([string]$LaneName)

    if ($LaneName -eq "integration") {
        return @{
            list_only = "powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane.ps1 -ListOnly"
            single_repeat = "powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane_repeat.ps1 -Repeat 1 -FailFast -FailIfSkipped"
            standard_repeat = "powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane_repeat.ps1 -Repeat 3 -FailIfSkipped"
        }
    }
    if ($LaneName -in @("real-review-success", "real-review-fail-closed", "models-v2-workall-real")) {
        return @{
            list_only = "powershell -ExecutionPolicy Bypass -File .\scripts\invoke_test_lane.ps1 -Lane $LaneName -ListOnly"
            single_repeat = "powershell -ExecutionPolicy Bypass -File .\scripts\run_lane_stability.ps1 -Lane $LaneName -Repeat 1 -FailFast -FailIfSkipped"
            standard_repeat = "powershell -ExecutionPolicy Bypass -File .\scripts\run_lane_stability.ps1 -Lane $LaneName -Repeat 3 -FailIfSkipped"
        }
    }
    return @{
        list_only = "powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane.ps1 -ListOnly"
        single_repeat = "powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane_repeat.ps1 -Repeat 1 -FailFast"
        standard_repeat = "powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane_repeat.ps1 -Repeat 3"
    }
}

function Get-FailureSignatureRows {
    param([object[]]$Runs)

    $counts = @{}
    foreach ($run in @($Runs)) {
        $status = [string]$run.status
        if ($status -eq "PASS") {
            continue
        }
        $signature = [string]$run.message
        if ([string]::IsNullOrWhiteSpace($signature)) {
            $signature = $status
        }
        $signature = $signature.Trim()
        if (-not $counts.ContainsKey($signature)) {
            $counts[$signature] = 0
        }
        $counts[$signature] += 1
    }

    $rows = @()
    foreach ($entry in ($counts.GetEnumerator() | Sort-Object @{ Expression = "Value"; Descending = $true }, @{ Expression = "Name"; Descending = $false })) {
        $rows += @{
            signature = [string]$entry.Name
            count = [int]$entry.Value
        }
    }
    return $rows
}

function Get-MissingEnvUnion {
    param([object[]]$Runs)

    $set = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($run in @($Runs)) {
        foreach ($name in @($run.missing_env)) {
            $normalized = [string]$name
            if ([string]::IsNullOrWhiteSpace($normalized)) {
                continue
            }
            [void]$set.Add($normalized.Trim())
        }
    }
    return @($set | Sort-Object)
}

function Test-AllNonPassRunsMatch {
    param(
        [object[]]$Runs,
        [string]$Needle
    )

    $nonPass = @($Runs | Where-Object { [string]$_.status -ne "PASS" })
    if ($nonPass.Count -eq 0) {
        return $false
    }
    foreach ($run in $nonPass) {
        $message = [string]$run.message
        if ($message -notlike "*$Needle*") {
            return $false
        }
    }
    return $true
}

function Get-RootCauseInfo {
    param(
        [string]$ClassificationId,
        [string]$Status,
        [object[]]$Runs,
        [string[]]$MissingEnv,
        [string]$Headline
    )

    $messages = @()
    foreach ($run in @($Runs)) {
        if ([string]$run.status -eq "PASS") {
            continue
        }
        $message = [string]$run.message
        if (-not [string]::IsNullOrWhiteSpace($message)) {
            $messages += $message.Trim().ToLowerInvariant()
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($Headline)) {
        $messages += $Headline.Trim().ToLowerInvariant()
    }
    $blob = [string]::Join(" | ", $messages)
    $bucket = "unknown"
    $label = "Unknown"

    if ($ClassificationId -eq "lane.stable_pass" -or $Status -eq "PASS") {
        $bucket = "stable_pass"
        $label = "Stable pass"
    } elseif (@($MissingEnv).Count -gt 0 -or $ClassificationId -in @("lane.integration_env_missing", "lane.integration_env_missing_fail_closed") -or $blob -like "*required integration environment is incomplete*") {
        $bucket = "env_missing"
        $label = "Environment missing"
    } elseif ($blob -match "429|rate limit|too many requests") {
        $bucket = "rate_limit"
        $label = "Rate limited"
    } elseif ($blob -match "timeout|timed out|deadline exceeded") {
        $bucket = "timeout"
        $label = "Timeout"
    } elseif ($blob -match "gateway|connection refused|name or service not known|service unavailable|temporarily unavailable|dns|ssl|tls|proxy|socket") {
        $bucket = "external_gateway"
        $label = "External gateway or network"
    } elseif ($blob -match "gate blocked|advisory gate|quality gate|blocking_violations|blocking violations") {
        $bucket = "gate_blocked"
        $label = "Gate blocked"
    } elseif ($blob -match "error at setup|failed at setup|fixture|scopemismatch|setup_error|verify setup") {
        $bucket = "verify_setup"
        $label = "Verify setup failure"
    } elseif ($blob -match "assertionerror|assertion failed|verify_failed|verification failed") {
        $bucket = "verify_failure"
        $label = "Verify failure"
    } elseif ($blob -match "task blocked|blocked:task_blocked|blocked by task") {
        $bucket = "task_blocked"
        $label = "Task blocked"
    } elseif ($blob -match "max_cycles|max cycles|cycle limit|round_limit") {
        $bucket = "max_cycles"
        $label = "Max cycles"
    } elseif ($blob -match "no_progress|no progress|no file changes") {
        $bucket = "no_progress"
        $label = "No progress"
    } elseif ($blob -match "stuck|repeated error") {
        $bucket = "stuck"
        $label = "Repeated failure / stuck"
    } elseif ($ClassificationId -eq "lane.flaky_failure") {
        $bucket = "flaky_failure"
        $label = "Flaky failure"
    } elseif ($Status -in @("FAIL", "SKIP")) {
        $bucket = "runtime_error"
        $label = "Runtime error"
    }

    return @{
        bucket = $bucket
        label = $label
    }
}

function New-LaneTriagePayload {
    param(
        [hashtable]$Summary,
        [string]$SummaryPath
    )

    $runs = @($Summary.runs)
    $commands = Get-LaneCommands -LaneName $Summary.lane
    $missingEnv = Get-MissingEnvUnion -Runs $runs
    $failureSignatures = Get-FailureSignatureRows -Runs $runs
    $envMissingPattern = Test-AllNonPassRunsMatch -Runs $runs -Needle "required integration environment is incomplete"

    $classificationId = "lane.unclassified"
    $classificationLabel = "Unclassified lane outcome"
    $alertLevel = "warning"
    $headline = "Lane outcome requires manual interpretation."
    $operatorActions = @(
        "Inspect the uploaded lane summary and CI console log before retrying the lane."
    )
    $ciActions = @(
        "Keep the fixed lane recipe unchanged until the failure has a concrete root cause."
    )
    $recommendedCommands = @(
        [string]$commands.list_only,
        [string]$commands.single_repeat
    )

    if ([string]$Summary.status -eq "PASS") {
        $classificationId = "lane.stable_pass"
        $classificationLabel = "Stable pass"
        $alertLevel = "info"
        $headline = "Lane repeated cleanly across all requested runs."
        $operatorActions = @(
            "Keep the current repeat count and review uploaded artifacts during the normal weekly standing-proof check.",
            "Only widen the lane recipe after a separate targeted validation, not inside the nightly lane."
        )
        $ciActions = @(
            "No CI recipe change is required.",
            "Retain the uploaded summary and triage artifacts for trend review."
        )
        $recommendedCommands = @(
            [string]$commands.standard_repeat
        )
    } elseif ([string]$Summary.status -eq "SKIP" -and $envMissingPattern) {
        $classificationId = "lane.integration_env_missing"
        $classificationLabel = "Integration environment missing"
        $alertLevel = "warning"
        $headline = "Lane skipped because the required integration environment is incomplete."
        $operatorActions = @(
            "Populate the missing integration secrets or gateway variables before treating this lane as standing proof.",
            "Do not count this run as green coverage for real-review standing proof."
        )
        $ciActions = @(
            "Verify `WORKFLOW_REVIEWER_API_KEY` and `WORKFLOW_REVIEWER_BASE_URL` are available in the target environment.",
            "Keep the lane fail-closed in dedicated integration jobs."
        )
        $recommendedCommands = @(
            "powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane.ps1 -FailIfSkipped",
            "powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane_repeat.ps1 -Repeat 3 -FailIfSkipped"
        )
    } elseif ([string]$Summary.status -eq "FAIL" -and $envMissingPattern -and [bool]$Summary.fail_if_skipped) {
        $classificationId = "lane.integration_env_missing_fail_closed"
        $classificationLabel = "Integration environment missing (fail-closed)"
        $alertLevel = "error"
        $headline = "Lane failed closed because required integration variables were missing."
        $operatorActions = @(
            "Treat this as an environment configuration incident, not a product-code regression.",
            "Restore the missing integration variables, then rerun the integration lane with the same fixed recipe."
        )
        $ciActions = @(
            "Check workflow secret scope, environment selection, and gateway reachability before rerunning.",
            "Do not remove `-FailIfSkipped`; the failure is the intended protection."
        )
        $recommendedCommands = @(
            "powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane_repeat.ps1 -Repeat 1 -FailFast -FailIfSkipped",
            "powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane_repeat.ps1 -Repeat 3 -FailIfSkipped"
        )
    } elseif ([int]$Summary.failed_runs -gt 0 -and [int]$Summary.passed_runs -gt 0) {
        $classificationId = "lane.flaky_failure"
        $classificationLabel = "Flaky lane"
        $alertLevel = "warning"
        $headline = "Lane is unstable: at least one repeat passed and at least one repeat failed."
        $operatorActions = @(
            "Compare the first failing repeat against the passing repeat before changing any recipe or threshold.",
            "If this is the integration lane, verify gateway availability and secret freshness before blaming product code."
        )
        $ciActions = @(
            "Keep the repeat-based nightly job in place so the instability remains visible.",
            "Escalate only after the same failure pattern reproduces in focused reruns."
        )
        $recommendedCommands = @(
            [string]$commands.single_repeat,
            [string]$commands.list_only
        )
    } elseif ([int]$Summary.failed_runs -gt 0) {
        $classificationId = "lane.consistent_failure"
        $classificationLabel = "Consistent lane failure"
        $alertLevel = "error"
        $headline = "Lane failed consistently across every executed repeat."
        $operatorActions = @(
            "Inspect the failing lane log and recipe targets, then reproduce with a single fail-fast rerun before editing code.",
            "Use targeted `-PytestArgs` only after confirming the fixed lane recipe itself is correct."
        )
        $ciActions = @(
            "Keep the lane fail-closed and fix the underlying regression instead of weakening the recipe.",
            "Use the uploaded triage artifact as the incident handoff summary."
        )
        $recommendedCommands = @(
            [string]$commands.list_only,
            [string]$commands.single_repeat
        )
    }
    $rootCause = Get-RootCauseInfo -ClassificationId $classificationId -Status ([string]$Summary.status) -Runs $runs -MissingEnv $missingEnv -Headline $headline

    return @{
        schema_version = "lane.triage.v1"
        triage_version = "lane.triage.v1"
        lane = [string]$Summary.lane
        status = [string]$Summary.status
        alert_level = $alertLevel
        classification_id = $classificationId
        classification_label = $classificationLabel
        root_cause_bucket = [string]$rootCause.bucket
        root_cause_label = [string]$rootCause.label
        headline = $headline
        summary_path = $SummaryPath
        repeat_requested = [int]$Summary.repeat_requested
        repeat_completed = [int]$Summary.repeat_completed
        passed_runs = [int]$Summary.passed_runs
        failed_runs = [int]$Summary.failed_runs
        skipped_runs = [int]$Summary.skipped_runs
        fail_if_skipped = [bool]$Summary.fail_if_skipped
        missing_env = @($missingEnv)
        failure_signatures = @($failureSignatures)
        operator_actions = @($operatorActions)
        ci_actions = @($ciActions)
        recommended_commands = @($recommendedCommands)
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    }
}

function New-LaneTriageMarkdown {
    param([hashtable]$Triage)

    $lines = @(
        "# Lane Triage Report",
        "",
        "- lane: $($Triage.lane)",
        "- status: $($Triage.status)",
        "- alert_level: $($Triage.alert_level)",
        "- classification: $($Triage.classification_id)",
        "- root_cause_bucket: $($Triage.root_cause_bucket)",
        "- summary_path: $($Triage.summary_path)",
        "- generated_at_utc: $($Triage.generated_at_utc)",
        "",
        "## Headline",
        "",
        $Triage.headline,
        "",
        "## Evidence",
        "",
        "- repeat_requested: $($Triage.repeat_requested)",
        "- repeat_completed: $($Triage.repeat_completed)",
        "- passed_runs: $($Triage.passed_runs)",
        "- failed_runs: $($Triage.failed_runs)",
        "- skipped_runs: $($Triage.skipped_runs)",
        "- fail_if_skipped: $($Triage.fail_if_skipped)"
    )

    if (@($Triage.missing_env).Count -gt 0) {
        $lines += ""
        $lines += "### Missing Env"
        $lines += ""
        foreach ($name in @($Triage.missing_env)) {
            $lines += ("- {0}" -f [string]$name)
        }
    }

    $lines += ""
    $lines += "## Failure Signatures"
    $lines += ""
    if (@($Triage.failure_signatures).Count -eq 0) {
        $lines += "- (none)"
    } else {
        foreach ($item in @($Triage.failure_signatures)) {
            $lines += ("- {0} (count={1})" -f [string]$item.signature, [int]$item.count)
        }
    }

    $lines += ""
    $lines += "## Operator Actions"
    $lines += ""
    foreach ($item in @($Triage.operator_actions)) {
        $lines += "- $item"
    }

    $lines += ""
    $lines += "## CI Actions"
    $lines += ""
    foreach ($item in @($Triage.ci_actions)) {
        $lines += "- $item"
    }

    $lines += ""
    $lines += "## Suggested Commands"
    $lines += ""
    foreach ($item in @($Triage.recommended_commands)) {
        $lines += ("- {0}" -f [string]$item)
    }

    return [string]::Join([Environment]::NewLine, $lines) + [Environment]::NewLine
}

if ($Repeat -lt 1) {
    throw "Repeat must be >= 1"
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$invokeScript = Join-Path $PSScriptRoot "invoke_test_lane.ps1"

if (-not (Test-Path $invokeScript)) {
    throw "Lane invoke script not found: $invokeScript"
}

if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
    $summaryDir = Join-Path $repoRoot "planning"
    $SummaryPath = Join-Path $summaryDir "lane_stability_$Lane.json"
}

$summaryDir = Split-Path -Path $SummaryPath -Parent
if ([string]::IsNullOrWhiteSpace($summaryDir)) {
    $summaryDir = $repoRoot
}
if (-not [string]::IsNullOrWhiteSpace($summaryDir)) {
    New-Item -Path $summaryDir -ItemType Directory -Force | Out-Null
}

$runs = @()
$failedRuns = 0
$skippedRuns = 0
$startedAtUtc = (Get-Date).ToUniversalTime().ToString("o")

for ($index = 1; $index -le $Repeat; $index++) {
    Write-Host "[stability:$Lane] run $index/$Repeat"
    $runStartedUtc = (Get-Date).ToUniversalTime().ToString("o")
    $runResultPath = Join-Path $summaryDir "lane_${Lane}_run_${index}.json"
    $runPayload = $null
    try {
        & $invokeScript -Lane $Lane -PytestArgs $PytestArgs -FailIfSkipped:$FailIfSkipped -ResultPath $runResultPath
        $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    } catch {
        $exitCode = if ($null -eq $LASTEXITCODE -or [int]$LASTEXITCODE -eq 0) { 1 } else { [int]$LASTEXITCODE }
        if (Test-Path $runResultPath) {
            $runPayload = Get-Content -LiteralPath $runResultPath -Raw | ConvertFrom-Json
        } else {
            $runPayload = [pscustomobject]@{
                status = "FAIL"
                exit_code = $exitCode
                message = $_.Exception.Message
                missing_env = @()
            }
        }
    }
    if ($null -eq $runPayload -and (Test-Path $runResultPath)) {
        $runPayload = Get-Content -LiteralPath $runResultPath -Raw | ConvertFrom-Json
    }
    $runFinishedUtc = (Get-Date).ToUniversalTime().ToString("o")
    $runStatus = if ($null -eq $runPayload) { if ($exitCode -eq 0) { "PASS" } else { "FAIL" } } else { [string]$runPayload.status }
    if ($runStatus -eq "FAIL") {
        $failedRuns += 1
    } elseif ($runStatus -eq "SKIP") {
        $skippedRuns += 1
    }
    $runs += @{
        run_index = $index
        status = $runStatus
        exit_code = $exitCode
        started_at_utc = $runStartedUtc
        finished_at_utc = $runFinishedUtc
        message = if ($null -eq $runPayload) { "" } else { [string]$runPayload.message }
        missing_env = if ($null -eq $runPayload) { @() } else { @($runPayload.missing_env) }
    }
    if (Test-Path $runResultPath) {
        Remove-Item -LiteralPath $runResultPath -Force
    }
    if (($runStatus -eq "FAIL") -and $FailFast) {
        break
    }
}

$finishedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
$triageJsonPath = Join-Path $summaryDir "lane_triage_$Lane.json"
$triageMarkdownPath = Join-Path $summaryDir "lane_triage_$Lane.md"
$summary = @{
    schema_version = "lane.stability.v1"
    summary_version = "lane.stability.v1"
    lane = $Lane
    repeat_requested = $Repeat
    repeat_completed = $runs.Count
    started_at_utc = $startedAtUtc
    finished_at_utc = $finishedAtUtc
    failed_runs = $failedRuns
    skipped_runs = $skippedRuns
    passed_runs = $runs.Count - $failedRuns - $skippedRuns
    fail_if_skipped = [bool]$FailIfSkipped
    pytest_args = @($PytestArgs)
    status = if ($failedRuns -gt 0) { "FAIL" } elseif ($skippedRuns -gt 0) { "SKIP" } else { "PASS" }
    triage_artifacts = @{
        json = $triageJsonPath
        markdown = $triageMarkdownPath
    }
    runs = $runs
}

$triage = New-LaneTriagePayload -Summary $summary -SummaryPath $SummaryPath
$triageMarkdown = New-LaneTriageMarkdown -Triage $triage

Write-Utf8Json -Path $SummaryPath -Payload $summary
Write-Utf8Json -Path $triageJsonPath -Payload $triage
Write-Utf8Text -Path $triageMarkdownPath -Content $triageMarkdown
Write-Host "[stability:$Lane] summary written to $SummaryPath"
Write-Host "[stability:$Lane] triage written to $triageJsonPath"
Write-Host "[stability:$Lane] triage markdown written to $triageMarkdownPath"

if ($failedRuns -eq 0) {
    exit 0
}
exit 1
