# =============================================================================
# Lambat Registry Bot - HTTP smoke check (native PowerShell version).
#
# PowerShell equivalent of smoke_check.sh. Verifies:
#   /healthz  -> 200, JSON with status=ok, discord_gateway=true, database=true
#   /metrics  -> 200, Prometheus text with all expected metric names
#
# NOTE: web/health.py returns the fields: status, discord_gateway, database,
# civinfo_ok, timestamp. Older versions returned "gateway"/"db" -- this script
# now checks the real field names.
#
# Does NOT touch Discord. Use this AFTER starting the bot (docker compose up
# or python main.py) to confirm the process is live and the DB is reachable.
#
# Usage:
#   .\scripts\smoke_check.ps1                                  # default port 10000
#   $env:PORT=8080; .\scripts\smoke_check.ps1                  # custom port
#   $env:BASE_URL="http://1.2.3.4:10000"; .\scripts\smoke_check.ps1
#
# Exit codes: 0 = all passed, 1 = at least one check failed.
#
# NOTE: This file is intentionally ASCII-only (no Unicode emoji/dashes) for
# maximum compatibility with Windows PowerShell 5.1 default encoding.
# =============================================================================

# Stop on first error in our own cmdlets, but we handle HTTP errors explicitly.
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Host "This script requires PowerShell 5.1 or later (Windows 10+ ships with 5.1 by default)." -ForegroundColor Red
    Write-Host "You have: PowerShell $($PSVersionTable.PSVersion)" -ForegroundColor Red
    exit 1
}

# Resolve BASE_URL - $env:PORT takes precedence, then $env:BASE_URL, then default.
if ($env:BASE_URL) {
    $baseUrl = $env:BASE_URL
} else {
    $port = if ($env:PORT) { $env:PORT } else { "10000" }
    $baseUrl = "http://localhost:$port"
}

$fail = 0

function Write-Pass($msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Bold($msg) { Write-Host $msg -ForegroundColor White }

Write-Host ""
Write-Bold "Lambat Registry Bot - HTTP smoke check"
Write-Bold "Target: $baseUrl"
Write-Host ""

# --- /healthz -----------------------------------------------------------------
Write-Bold "1. /healthz"
try {
    $response = Invoke-WebRequest -Uri "$baseUrl/healthz" -TimeoutSec 5 -UseBasicParsing
} catch {
    Write-Fail "Could not connect to $baseUrl/healthz"
    Write-Host "    Is the bot running? Start it with: docker compose up" -ForegroundColor Red
    exit 1
}

if ($response.StatusCode -ne 200) {
    Write-Fail "/healthz returned HTTP $($response.StatusCode) (expected 200)"
    Write-Host "    Body: $($response.Content)" -ForegroundColor Red
    $fail = 1
} else {
    try {
        $health = $response.Content | ConvertFrom-Json
    } catch {
        Write-Fail "/healthz returned 200 but body is not valid JSON"
        Write-Host "    Body: $($response.Content)" -ForegroundColor Red
        $fail = 1
        $health = $null
    }

    if ($health) {
        # web/health.py returns: status, discord_gateway, database, civinfo_ok,
        # timestamp. We gate on status + discord_gateway + database (CivInfo is
        # a soft-degraded third-party dep, not gating -- see health.py header).
        $gw = $health.discord_gateway
        $db = $health.database
        $civ = $health.civinfo_ok
        if ($health.status -eq "ok" -and $gw -eq $true -and $db -eq $true) {
            $civTag = if ($civ -eq $true) { "civinfo_ok" } else { "civinfo DEGRADED" }
            Write-Pass "/healthz 200 - status=ok, gateway=true, db=true ($civTag)"
        } else {
            Write-Fail "/healthz returned 200 but unhealthy state:"
            Write-Host "    status=$($health.status) discord_gateway=$gw database=$db civinfo_ok=$civ" -ForegroundColor Red
            Write-Host "    Body: $($response.Content)" -ForegroundColor Red
            $fail = 1
        }
    }
}
Write-Host ""

# --- /metrics -----------------------------------------------------------------
Write-Bold "2. /metrics"
try {
    $metricsResponse = Invoke-WebRequest -Uri "$baseUrl/metrics" -TimeoutSec 5 -UseBasicParsing
} catch {
    Write-Fail "Could not connect to $baseUrl/metrics"
    $fail = 1
    $metricsResponse = $null
}

if ($metricsResponse) {
    if ($metricsResponse.StatusCode -ne 200) {
        Write-Fail "/metrics returned HTTP $($metricsResponse.StatusCode) (expected 200)"
        $fail = 1
    } else {
        Write-Pass "/metrics returned 200"
        Write-Host ""
        Write-Bold "3. Required metric names present"
        $metricsBody = $metricsResponse.Content
        $requiredMetrics = @(
            "lambat_citizens_total",
            "lambat_active_citizens",
            "lambat_settlements_total",
            "lambat_civinfo_cache_hits_total",
            "lambat_civinfo_auth_broken",
            "lambat_civmc_online",
            "lambat_last_outage_duration_seconds"
        )
        foreach ($metric in $requiredMetrics) {
            # Match either a HELP/TYPE comment line or a metric sample line.
            $matchesFound = $metricsBody -split "`n" | Where-Object {
                $_ -match "^# (HELP|TYPE) $metric" -or $_ -match "^$metric( |\{|$)"
            }
            if ($matchesFound) {
                # Extract the value from the sample line (if present).
                $sampleLine = $matchesFound | Where-Object { $_ -notmatch "^#" } | Select-Object -First 1
                if ($sampleLine) {
                    $parts = $sampleLine -split "\s+"
                    $value = if ($parts.Length -ge 2) { $parts[1] } else { "<unset>" }
                } else {
                    $value = "<comment-only>"
                }
                Write-Pass "$metric = $value"
            } else {
                Write-Fail "$metric - MISSING from /metrics output"
                $fail = 1
            }
        }
    }
}
Write-Host ""

# --- Summary ------------------------------------------------------------------
Write-Host ("=" * 60)
if ($fail -ne 0) {
    Write-Host "[FAIL] Smoke check FAILED - see above." -ForegroundColor Red
    exit 1
} else {
    Write-Host "[OK] All smoke checks passed." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next: open Discord and run /help in your test server."
    exit 0
}
