# =============================================================================
# Lambat Registry Bot - Windows E2E test orchestrator.
#
# Runs the full end-to-end verification suite on a Windows machine and writes
# a structured report (e2e_report.md + e2e_report.txt) you can paste back to a
# collaborator. Designed for PowerShell 5.1 (Win10 default) AND PowerShell 7.
#
# Stages (in order):
#   S0  prereqs       - python, docker, git, .env presence
#   S1  ruff format   - ruff format --check .
#   S2  ruff check    - ruff check .
#   S3  mypy          - mypy core services api tasks web utils.py main.py
#   S4  pytest        - pytest -q
#   S5  docker build  - docker compose build
#   S6  docker up     - docker compose up -d
#   S7  wait health   - poll /healthz until healthy (or timeout)
#   S8  preflight     - Discord token/guild/role/channel checks (needs token)
#   S9  seed          - python scripts/seed.py
#   S10 smoke         - scripts/smoke_check.ps1 (healthz + metrics)
#   S11 db verify     - python scripts/db_verify.py --check-seed
#   S12 cmd audit     - python scripts/command_audit.py (Discord slash sync)
#   S13 docker logs   - capture last 200 lines of bot logs
#   S14 teardown      - docker compose down (skipped with -KeepRunning)
#
# Usage:
#   .\scripts\run_e2e.ps1                        # full run, tear down at end
#   .\scripts\run_e2e.ps1 -KeepRunning          # leave bot running for /cmd tests
#   .\scripts\run_e2e.ps1 -SkipDiscord          # skip stages needing DISCORD_TOKEN
#   .\scripts\run_e2e.ps1 -SkipDocker            # only lint+type+tests (no container)
#   .\scripts\run_e2e.ps1 -ReportPath .\out.md   # custom report path
#   .\scripts\run_e2e.ps1 -Stages S1,S2,S3,S4    # run only listed stages
#
# Exit codes: 0 = all run stages passed, 1 = at least one failed.
#
# NOTE: ASCII-only on purpose for Windows PowerShell 5.1 default encoding.
# =============================================================================

[CmdletBinding()]
param(
    [switch]$KeepRunning,
    [switch]$SkipDiscord,
    [switch]$SkipDocker,
    [string]$ReportPath = "e2e_report.md",
    [string]$Stages = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0

# --- Resolve repo root (script lives in <root>/scripts/) --------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

# --- Helpers ----------------------------------------------------------------
function Write-Section($n, $t) { Write-Host "`n=== S$n $t ===" -ForegroundColor Cyan }
function Write-Ok($m) { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Write-Bad($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Write-Inf($m) { Write-Host "  [info] $m" -ForegroundColor DarkGray }

# Results: array of hashtables. Each: Name, Label, Status(PASS/FAIL/SKIP),
# Duration(sec), ExitCode, Output(snippet).
$script:Results = New-Object System.Collections.ArrayList
$script:OverallStart = Get-Date

function Add-Result($name, $label, $status, $dur, $rc, $out) {
    $null = $script:Results.Add(@{
        Name = $name; Label = $label; Status = $status
        Duration = $dur; ExitCode = $rc; Output = $out
    })
}

function Invoke-NativeStage {
    # Runs a native command inline (no background job) so $LASTEXITCODE is
    # captured reliably. The wrapped tools are all well-behaved (ruff/mypy/
    # pytest exit promptly; preflight + command_audit have their own internal
    # timeouts; docker compose build is the only potentially-slow one and it
    # always terminates). A hung tool would stall the harness -- that's an
    # acceptable trade for trustworthy exit codes.
    param(
        [string]$Name, [string]$Label, [string[]]$Argv, [int]$TailLines = 40
    )
    Write-Section $Name $Label
    Write-Inf ("run: " + ($Argv -join " "))
    $start = Get-Date
    $exe = $Argv[0]
    $rest = @()
    if ($Argv.Length -gt 1) { $rest = $Argv[1..($Argv.Length - 1)] }

    $output = $null
    $rc = -1
    try {
        $output = & $exe @rest 2>&1
        $rc = $LASTEXITCODE
        if ($null -eq $rc) { $rc = 0 }
    } catch {
        $output = @("[EXCEPTION] $($_.Exception.Message)")
        $rc = 1
    }
    $dur = [int]((Get-Date) - $start).TotalSeconds

    # Coerce output to a flat string array.
    if ($null -eq $output) { $output = @() }
    $outLines = @($output | ForEach-Object { [string]$_ })
    $snippet = ($outLines | Select-Object -Last $TailLines) -join "`n"

    if ($rc -eq 0) {
        Write-Ok ("$Label passed (${dur}s)")
        Add-Result $Name $Label "PASS" $dur $rc $snippet
    } else {
        Write-Bad ("$Label FAILED (exit=$rc, ${dur}s)")
        if ($outLines.Count -gt 0) {
            Write-Host "  --- last output ---" -ForegroundColor DarkGray
            $outLines | Select-Object -Last 15 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        }
        Add-Result $Name $Label "FAIL" $dur $rc $snippet
    }
    return $rc
}

function Invoke-ScriptStage {
    param(
        [string]$Name, [string]$Label, [scriptblock]$Block
    )
    Write-Section $Name $Label
    $start = Get-Date
    $output = @()
    $rc = 0
    try {
        $output = & $Block 2>&1
        $rc = if ($LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    } catch {
        $output = @("[EXCEPTION] $($_.Exception.Message)")
        $rc = 1
    }
    $dur = [int]((Get-Date) - $start).TotalSeconds
    if ($null -eq $output) { $output = @() }
    $outLines = @($output | ForEach-Object { [string]$_ })
    $snippet = ($outLines | Select-Object -Last 40) -join "`n"
    if ($rc -eq 0) {
        Write-Ok ("$Label passed (${dur}s)")
        Add-Result $Name $Label "PASS" $dur $rc $snippet
    } else {
        Write-Bad ("$Label FAILED (exit=$rc, ${dur}s)")
        $outLines | Select-Object -Last 15 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        Add-Result $Name $Label "FAIL" $dur $rc $snippet
    }
    return $rc
}

# --- Detect Python ----------------------------------------------------------
function Resolve-Python {
    # Returns a string array like @("python") or @("py","-3"), or $null.
    # Caller MUST wrap with @(...) to survive PS single-element unwrapping:
    #   $py = @(Resolve-Python)
    foreach ($c in @("python", "py -3", "python3")) {
        $parts = $c -split " "
        $exe = $parts[0]
        $extra = @()
        if ($parts.Length -gt 1) { $extra = $parts[1..($parts.Length - 1)] }
        try {
            $ver = & $exe @extra --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$ver" -match "Python") {
                Write-Inf ("python: $c -> $ver")
                $result = @($exe)
                if ($extra.Count -gt 0) { $result = @($exe) + $extra }
                return , $result   # comma-wrap so a 1-element array survives
            }
        } catch { }
    }
    return $null
}

# --- Detect docker compose --------------------------------------------------
function Resolve-Compose {
    try {
        $null = & docker compose version 2>&1
        if ($LASTEXITCODE -eq 0) { return ,@("docker", "compose") }
    } catch { }
    try {
        $null = & docker-compose version 2>&1
        if ($LASTEXITCODE -eq 0) { return ,@("docker-compose") }
    } catch { }
    return $null
}

# --- Stage selection --------------------------------------------------------
$AllStages = @(
    "S0","S1","S2","S3","S4","S5","S6","S7","S8","S9","S10","S11","S12","S13","S14"
)
if ($Stages -ne "") {
    $Selected = $Stages -split "," | ForEach-Object { $_.Trim().ToUpper() }
} else {
    $Selected = $AllStages
}
function Should-Run($n) { $Selected -contains $n }

# =========================================================================
# S0 - Prerequisites
# =========================================================================
if (Should-Run "S0") {
    Write-Section "S0" "Prerequisites"
    $start = Get-Date
    $issues = @()
    $py = Resolve-Python
    if ($null -eq $py) { $issues += "python not found on PATH (install Python 3.11+)" }
    $compose = Resolve-Compose
    $dockerOk = $true
    if ($null -eq $compose) { $dockerOk = $false }
    if (-not (Test-Path "$RepoRoot\.env")) { $issues += ".env not found (cp .env.example .env)" }

    $hasToken = $false
    if (Test-Path "$RepoRoot\.env") {
        $envContent = Get-Content "$RepoRoot\.env" -ErrorAction SilentlyContinue
        $tokLine = $envContent | Where-Object { $_ -match "^DISCORD_TOKEN=" } | Select-Object -First 1
        if ($tokLine -and $tokLine -notmatch "your_bot_token_here" -and $tokLine.Length -gt 30) {
            $hasToken = $true
        }
    }

    Write-Inf ("python: " + $(if ($py) { $py -join " " } else { "MISSING" }))
    Write-Inf ("docker compose: " + $(if ($compose) { $compose -join " " } else { "MISSING" }))
    Write-Inf (".env present: " + $(Test-Path "$RepoRoot\.env"))
    Write-Inf ("DISCORD_TOKEN set: $hasToken")

    $rc = 0
    if ($issues.Count -gt 0) {
        foreach ($i in $issues) { Write-Bad $i }
        $rc = 1
    } else {
        Write-Ok "prereqs OK"
        if (-not $hasToken) { Write-Inf "DISCORD_TOKEN not set -> S8/S12 will be skipped (use -SkipDiscord)" }
    }
    $dur = [int]((Get-Date) - $start).TotalSeconds
    Add-Result "S0" "Prerequisites" $(if ($rc -eq 0) {"PASS"} else {"FAIL"}) $dur $rc ($issues -join "`n")

    # Force-skip Discord stages if no token (unless user explicitly insists).
    if (-not $hasToken -and -not $SkipDiscord) {
        Write-Inf "Auto-skipping S8/S12 (no DISCORD_TOKEN). Use -SkipDiscord to silence."
    }
}

# Recompute token presence after S0 (env may have changed).
$hasTokenNow = $false
if (Test-Path "$RepoRoot\.env") {
    $tokLine = Get-Content "$RepoRoot\.env" -ErrorAction SilentlyContinue |
        Where-Object { $_ -match "^DISCORD_TOKEN=" } | Select-Object -First 1
    if ($tokLine -and $tokLine -notmatch "your_bot_token_here" -and $tokLine.Length -gt 30) {
        $hasTokenNow = $true
    }
}
$runDiscord = (-not $SkipDiscord) -and $hasTokenNow

# =========================================================================
# S1-S4 - Lint / type / unit tests (skip if docker-only requested)
# =========================================================================
$py = Resolve-Python
if ($null -ne $py) {
    if (Should-Run "S1") {
        Invoke-NativeStage "S1" "ruff format --check ." @($py + @("-m", "ruff", "format", "--check", "."))
    }
    if (Should-Run "S2") {
        Invoke-NativeStage "S2" "ruff check ." @($py + @("-m", "ruff", "check", "."))
    }
    if (Should-Run "S3") {
        Invoke-NativeStage "S3" "mypy" @($py + @("-m", "mypy", "core", "services", "api", "tasks", "web", "utils.py", "main.py"))
    }
    if (Should-Run "S4") {
        Invoke-NativeStage "S4" "pytest" @($py + @("-m", "pytest", "-q"))
    }
} else {
    foreach ($s in @("S1","S2","S3","S4")) {
        if (Should-Run $s) { Add-Result $s "lint/type/test (skipped: no python)" "SKIP" 0 0 "python not found" }
    }
}

# =========================================================================
# S5-S7, S13, S14 - Docker stages
# =========================================================================
$compose = Resolve-Compose
$canDocker = (-not $SkipDocker) -and ($null -ne $compose)

if ($canDocker) {
    if (Should-Run "S5") {
        Invoke-NativeStage "S5" "docker compose build" @($compose + @("build"))
    }
    if (Should-Run "S6") {
        Invoke-NativeStage "S6" "docker compose up -d" @($compose + @("up", "-d"))
    }
    if (Should-Run "S7") {
        Write-Section "S7" "Wait for /healthz healthy"
        $start = Get-Date
        $baseUrl = $env:BASE_URL
        if (-not $baseUrl) {
            $port = if ($env:PORT) { $env:PORT } else { "10000" }
            $baseUrl = "http://localhost:$port"
        }
        $healthy = $false
        $lastBody = ""
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 2
            try {
                $r = Invoke-WebRequest -Uri "$baseUrl/healthz" -TimeoutSec 3 -UseBasicParsing
                if ($r.StatusCode -eq 200) {
                    $h = $r.Content | ConvertFrom-Json
                    if ($h.status -eq "ok" -and $h.discord_gateway -eq $true -and $h.database -eq $true) {
                        $healthy = $true; break
                    }
                    $lastBody = $r.Content
                }
            } catch { $lastBody = "$($_.Exception.Message)" }
            Write-Inf ("waiting for health... attempt $($i+1)/60")
        }
        $dur = [int]((Get-Date) - $start).TotalSeconds
        if ($healthy) {
            Write-Ok "bot healthy after ${dur}s"
            Add-Result "S7" "Wait /healthz healthy" "PASS" $dur 0 "200 status=ok gateway=true db=true"
        } else {
            Write-Bad "bot not healthy after ${dur}s (last: $lastBody)"
            Add-Result "S7" "Wait /healthz healthy" "FAIL" $dur 1 "last body: $lastBody"
        }
    }
} else {
    foreach ($s in @("S5","S6","S7")) {
        if (Should-Run $s) {
            $reason = if ($SkipDocker) { "skipped (-SkipDocker)" } else { "docker not found" }
            Add-Result $s "docker stage ($reason)" "SKIP" 0 0 $reason
            Write-Inf "$s $reason"
        }
    }
}

# =========================================================================
# S8 - Preflight (Discord token/guild/role/channel checks)
# =========================================================================
if (Should-Run "S8") {
    if ($runDiscord) {
        Invoke-NativeStage "S8" "preflight.py (Discord)" @($py + @("scripts/preflight.py"))
    } else {
        $reason = if ($SkipDiscord) { "-SkipDiscord" } else { "no DISCORD_TOKEN" }
        Write-Inf "S8 skipped ($reason)"
        Add-Result "S8" "preflight ($reason)" "SKIP" 0 0 $reason
    }
}

# =========================================================================
# S9 - Seed test data
# =========================================================================
if (Should-Run "S9") {
    if ($canDocker) {
        # seed.py reads DATABASE_URL from .env; under docker, the compose `db`
        # service is on localhost:5432. Override DATABASE_URL to match.
        $seedUrl = "postgresql://lambat:lambat_dev_password@localhost:5432/lambat"
        $env:DATABASE_URL = $seedUrl
        Invoke-NativeStage "S9" "seed.py" @($py + @("scripts/seed.py"))
    } else {
        $reason = if ($SkipDocker) { "skipped (-SkipDocker)" } else { "docker not found / not running" }
        Add-Result "S9" "seed ($reason)" "SKIP" 0 0 $reason
        Write-Inf "S9 $reason"
    }
}

# =========================================================================
# S10 - HTTP smoke check (healthz + metrics)
# =========================================================================
if (Should-Run "S10") {
    if ($canDocker) {
        Invoke-ScriptStage "S10" "smoke_check.ps1" {
            & "$RepoRoot\scripts\smoke_check.ps1"
        }
    } else {
        $reason = if ($SkipDocker) { "skipped (-SkipDocker)" } else { "docker not found / not running" }
        Write-Inf "S10 $reason"
        Add-Result "S10" "smoke check ($reason)" "SKIP" 0 0 $reason
    }
}

# =========================================================================
# S11 - DB verify
# =========================================================================
if (Should-Run "S11") {
    if ($canDocker) {
        $env:DATABASE_URL = "postgresql://lambat:lambat_dev_password@localhost:5432/lambat"
        Invoke-NativeStage "S11" "db_verify.py --check-seed" @($py + @("scripts/db_verify.py", "--check-seed"))
    } else {
        $reason = if ($SkipDocker) { "skipped (-SkipDocker)" } else { "docker not found / not running" }
        Write-Inf "S11 $reason"
        Add-Result "S11" "db verify ($reason)" "SKIP" 0 0 $reason
    }
}

# =========================================================================
# S12 - Discord command sync audit
# =========================================================================
if (Should-Run "S12") {
    if ($runDiscord) {
        Invoke-NativeStage "S12" "command_audit.py" @($py + @("scripts/command_audit.py"))
    } else {
        $reason = if ($SkipDiscord) { "-SkipDiscord" } else { "no DISCORD_TOKEN" }
        Write-Inf "S12 skipped ($reason)"
        Add-Result "S12" "command audit ($reason)" "SKIP" 0 0 $reason
    }
}

# =========================================================================
# S13 - Capture docker logs (informational, never fails)
# =========================================================================
if (Should-Run "S13" -and $canDocker) {
    Write-Section "S13" "Capture docker logs (last 200 lines)"
    $start = Get-Date
    try {
        # Append the sub-args BEFORE slicing so the index math works for both
        # `docker compose` (2 elements) and `docker-compose` (1 element).
        $full = @($compose) + @("logs", "--tail", "200", "bot")
        $logs = & $full[0] @($full[1..($full.Length - 1)]) 2>&1
        $logText = ($logs | ForEach-Object { [string]$_ }) -join "`n"
        $logPath = Join-Path $RepoRoot "e2e_docker_logs.txt"
        Set-Content -Path $logPath -Value $logText -Encoding UTF8
        Write-Ok "logs saved to e2e_docker_logs.txt"
        Add-Result "S13" "docker logs" "PASS" ([int]((Get-Date)-$start).TotalSeconds) 0 ($logText -split "`n" | Select-Object -Last 20 | Out-String)
    } catch {
        Write-Bad "could not capture logs: $($_.Exception.Message)"
        Add-Result "S13" "docker logs" "FAIL" ([int]((Get-Date)-$start).TotalSeconds) 1 "$($_.Exception.Message)"
    }
} elseif (Should-Run "S13") {
    Add-Result "S13" "docker logs (skipped)" "SKIP" 0 0 "no docker"
}

# =========================================================================
# S14 - Teardown
# =========================================================================
if (Should-Run "S14") {
    if ($KeepRunning) {
        Write-Section "S14" "Teardown (SKIPPED - -KeepRunning)"
        Write-Inf "bot left running. Stop with: docker compose down"
        Add-Result "S14" "teardown" "SKIP" 0 0 "-KeepRunning"
    } elseif ($canDocker) {
        Invoke-NativeStage "S14" "docker compose down" @($compose + @("down"))
    } else {
        Add-Result "S14" "teardown (skipped)" "SKIP" 0 0 "no docker"
    }
}

# =========================================================================
# Render report
# =========================================================================
$totalDur = [int]((Get-Date) - $script:OverallStart).TotalSeconds
$passed = ($script:Results | Where-Object { $_.Status -eq "PASS" }).Count
$failed = ($script:Results | Where-Object { $_.Status -eq "FAIL" }).Count
$skipped = ($script:Results | Where-Object { $_.Status -eq "SKIP" }).Count

$osVer = "$([System.Environment]::OSVersion.VersionString)"
$psVer = "$($PSVersionTable.PSVersion)"
$pyVer = "n/a"
if ($py) {
    try {
        if ($py.Length -gt 1) {
            $pyVer = (& $py[0] @($py[1..($py.Length - 1)] + @("--version"))) 2>&1
        } else {
            $pyVer = (& $py[0] --version) 2>&1
        }
    } catch { }
}

$overall = if ($failed -eq 0) { "PASS" } else { "FAIL" }

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("# LambatRegistryBot - E2E Test Report")
[void]$sb.AppendLine("")
[void]$sb.AppendLine("- **Generated:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')")
[void]$sb.AppendLine("- **Overall:** $overall")
[void]$sb.AppendLine("- **Host:** $osVer")
[void]$sb.AppendLine("- **PowerShell:** $psVer")
[void]$sb.AppendLine("- **Python:** $pyVer")
[void]$sb.AppendLine("- **Total wall time:** ${totalDur}s")
[void]$sb.AppendLine("- **Stages:** $passed passed, $failed failed, $skipped skipped (of $($script:Results.Count) run)")
[void]$sb.AppendLine("")
[void]$sb.AppendLine("## Summary table")
[void]$sb.AppendLine("")
[void]$sb.AppendLine("| Stage | Name | Status | Exit | Duration |")
[void]$sb.AppendLine("|-------|------|--------|------|----------|")
foreach ($r in $script:Results) {
    $icon = if ($r.Status -eq "PASS") { ":white_check_mark:" } elseif ($r.Status -eq "FAIL") { ":x:" } else { ":white_circle:" }
    [void]$sb.AppendLine("| $($r.Name) | $($r.Label) | $icon $($r.Status) | $($r.ExitCode) | $($r.Duration)s |")
}
[void]$sb.AppendLine("")
[void]$sb.AppendLine("## Stage details")
[void]$sb.AppendLine("")
foreach ($r in $script:Results) {
    [void]$sb.AppendLine("### $($r.Name) - $($r.Label)  [$($r.Status), exit=$($r.ExitCode), $($r.Duration)s]")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine('```')
    $out = if ($r.Output) { $r.Output } else { "(no output)" }
    [void]$sb.AppendLine($out)
    [void]$sb.AppendLine('```')
    [void]$sb.AppendLine("")
}
[void]$sb.AppendLine("---")
[void]$sb.AppendLine("## Manual Discord checklist (run by a human)")
[void]$sb.AppendLine("")
[void]$sb.AppendLine("Cursor cannot click Discord buttons. After this automated run passes,")
[void]$sb.AppendLine("a human should run the slash-command checklist in scripts/E2E_CHECKLIST.md")
[void]$sb.AppendLine("(Phase 0 - Phase 4 sections) against the live bot, then report results.")
[void]$sb.AppendLine("")
[void]$sb.AppendLine("## Notes")
[void]$sb.AppendLine("- Stages S8 + S12 require a real DISCORD_TOKEN + GUILD_ID in .env.")
[void]$sb.AppendLine("- Stages S5-S7, S9-S11, S13-S14 require Docker Desktop running.")
[void]$sb.AppendLine("- Full docker logs (if captured) are in e2e_docker_logs.txt.")
[void]$sb.AppendLine("- Re-run a single stage with: .\scripts\run_e2e.ps1 -Stages S3")

$report = $sb.ToString()
Set-Content -Path $ReportPath -Value $report -Encoding UTF8

# Also write a plain-text twin (same content, .txt) for easy paste anywhere.
$txtPath = [IO.Path]::ChangeExtension($ReportPath, "txt")
Set-Content -Path $txtPath -Value $report -Encoding UTF8

# Console summary
Write-Host ""
Write-Host ("=" * 60) -ForegroundColor Cyan
if ($failed -eq 0) {
    Write-Host "OVERALL: PASS  ($passed passed, $skipped skipped, ${totalDur}s)" -ForegroundColor Green
} else {
    Write-Host "OVERALL: FAIL  ($failed failed of $($script:Results.Count), ${totalDur}s)" -ForegroundColor Red
}
Write-Host "Report: $ReportPath" -ForegroundColor Cyan
Write-Host "       $txtPath" -ForegroundColor Cyan
Write-Host ("=" * 60) -ForegroundColor Cyan

if ($failed -gt 0) { exit 1 } else { exit 0 }
