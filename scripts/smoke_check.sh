#!/usr/bin/env bash
# =============================================================================
# Smoke-check a running Lambat Registry Bot via its HTTP endpoints.
#
# Verifies:
#   /healthz  → 200, JSON with status=ok, gateway=true, db=true
#   /metrics  → 200, Prometheus text with all expected metric names
#
# Does NOT touch Discord. Use this AFTER `python main.py` (or `docker compose
# up`) to confirm the bot is live and the DB is reachable from inside the
# process.
#
# Usage:
#   ./scripts/smoke_check.sh                  # default: http://localhost:10000
#   PORT=8080 ./scripts/smoke_check.sh        # custom port
#   BASE_URL=http://1.2.3.4:10000 ./scripts/smoke_check.sh
# =============================================================================
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:${PORT:-10000}}"
FAIL=0

green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$1"; }
bold()  { printf "\033[1m%s\033[0m\n" "$1"; }

echo ""
bold "Lambat Registry Bot — HTTP smoke check"
bold "Target: $BASE_URL"
echo ""

# --- /healthz -----------------------------------------------------------------
bold "1. /healthz"
health_response=$(curl -sS -m 5 -w "\n%{http_code}" "$BASE_URL/healthz" 2>/dev/null) || {
  red "  ✗ Could not connect to $BASE_URL/healthz"
  red "    Is the bot running? Start it with: python main.py  (or: docker compose up)"
  exit 1
}
health_code=$(echo "$health_response" | tail -1)
health_body=$(echo "$health_response" | sed '$d')

if [ "$health_code" != "200" ]; then
  red "  ✗ /healthz returned HTTP $health_code (expected 200)"
  red "    Body: $health_body"
  FAIL=1
else
  # Parse the JSON body without jq (keep the script dep-free).
  status=$(echo "$health_body" | grep -oE '"status"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*"status"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')
  gw=$(echo "$health_body" | grep -oE '"gateway"[[:space:]]*:[[:space:]]*(true|false)' | head -1 | sed -E 's/.*:(true|false).*/\1/')
  db=$(echo "$health_body" | grep -oE '"db"[[:space:]]*:[[:space:]]*(true|false)' | head -1 | sed -E 's/.*:(true|false).*/\1/')

  if [ "$status" = "ok" ] && [ "$gw" = "true" ] && [ "$db" = "true" ]; then
    green "  ✓ /healthz 200 — status=ok, gateway=true, db=true"
  else
    red "  ✗ /healthz returned 200 but unhealthy state:"
    red "    status=$status gateway=$gw db=$db"
    red "    Body: $health_body"
    FAIL=1
  fi
fi
echo ""

# --- /metrics -----------------------------------------------------------------
bold "2. /metrics"
metrics_response=$(curl -sS -m 5 -w "\n%{http_code}" "$BASE_URL/metrics" 2>/dev/null) || {
  red "  ✗ Could not connect to $BASE_URL/metrics"
  FAIL=1
}
metrics_code=$(echo "$metrics_response" | tail -1)
metrics_body=$(echo "$metrics_response" | sed '$d')

if [ "$metrics_code" != "200" ]; then
  red "  ✗ /metrics returned HTTP $metrics_code (expected 200)"
  FAIL=1
else
  green "  ✓ /metrics returned 200"
  echo ""
  bold "3. Required metric names present"
  # Every metric the ROADMAP §1.5 mandates + the scrap-time gauges added in
  # core/metrics.py collect_metrics(). If any is missing, the bot's
  # /metrics endpoint is broken.
  required_metrics=(
    "lambat_citizens_total"
    "lambat_active_citizens"
    "lambat_settlements_total"
    "lambat_civinfo_cache_hits_total"
    "lambat_civinfo_auth_broken"
    "lambat_civmc_online"
    "lambat_last_outage_duration_seconds"
  )
  for metric in "${required_metrics[@]}"; do
    if echo "$metrics_body" | grep -q "^# HELP ${metric}\|^# TYPE ${metric}\|^${metric}"; then
      value=$(echo "$metrics_body" | grep -E "^${metric}( |\{|$)" | head -1 | awk '{print $2}')
      green "  ✓ ${metric} = ${value:-<unset>}"
    else
      red "  ✗ ${metric} — MISSING from /metrics output"
      FAIL=1
    fi
  done
fi
echo ""

# --- Summary ------------------------------------------------------------------
if [ "$FAIL" -ne 0 ]; then
  red "$(bold '✗ Smoke check FAILED — see above.')"
  exit 1
else
  green "$(bold '✓ All smoke checks passed.')"
  echo ""
  echo "Next: open Discord and run /help in your test server."
  exit 0
fi
