# Lambat Registry Bot — End-to-End Test Checklist

A walk-through of every Phase 0–4 feature against a real Discord + real
Postgres. Run this AFTER `python scripts/preflight.py` passes and the bot is
running (`python main.py` or `docker compose up`).

---

## Automated harness (run this FIRST)

Before doing the manual Discord walk-through below, run the automated Windows
harness — it covers everything that does NOT need a human clicking in Discord:

```powershell
# One-time: install the dev tooling into your Python.
pip install -e ".[dev]"

# Full run (lint + types + tests + docker + seed + smoke + DB verify + command
# sync audit). Writes e2e_report.md + e2e_report.txt.
.\scripts\run_e2e.ps1

# Leave the bot running afterwards so you can do the manual /command checklist:
.\scripts\run_e2e.ps1 -KeepRunning

# No Discord token yet? Skip the Discord stages:
.\scripts\run_e2e.ps1 -SkipDiscord

# Just the fast local checks (no docker):
.\scripts\run_e2e.ps1 -SkipDocker

# Re-run a single stage:
.\scripts\run_e2e.ps1 -Stages S3
```

The harness runs 15 stages (S0–S14) and writes a structured markdown report.
**Only after the automated report is all-green should you start the manual
Discord checklist below** (the manual steps exercise the slash commands the
automated harness cannot click).

| Stage | What it checks | Needs |
|-------|----------------|-------|
| S0 | python / docker / git / .env present | — |
| S1 | `ruff format --check .` | python + deps |
| S2 | `ruff check .` | python + deps |
| S3 | `mypy` (28 source files) | python + deps |
| S4 | `pytest` (294 tests) | python + deps |
| S5 | `docker compose build` | Docker Desktop |
| S6 | `docker compose up -d` | Docker Desktop |
| S7 | poll `/healthz` until healthy (≤2 min) | bot running |
| S8 | `preflight.py` — token/guild/role/channel | DISCORD_TOKEN |
| S9 | `seed.py` — 5 settlements + 6 citizens | DB up |
| S10 | `smoke_check.ps1` — /healthz + /metrics | bot running |
| S11 | `db_verify.py --check-seed` — schema + seed | DB up |
| S12 | `command_audit.py` — all 13 slash cmds synced | DISCORD_TOKEN |
| S13 | capture last 200 lines of bot logs | Docker |
| S14 | `docker compose down` (unless `-KeepRunning`) | Docker |

---

**How to use:** tick each box as the command succeeds. If anything fails,
check `lambat_bot.log` first — every failure is logged at WARNING/ERROR with
a traceback.

**Prerequisite:** run `python scripts/seed.py` first so the read commands have
data to show. (Skip this if you want to test the empty-state UX too.)

---

## Setup (do once)

- [ ] `cp .env.example .env` and filled in DISCORD_TOKEN, GUILD_ID, role IDs
- [ ] `python scripts/preflight.py` — all critical checks pass
- [ ] `docker compose up --build` (OR `python main.py` if running locally with your own Postgres)
- [ ] Bot log shows: `Discord gateway: READY` + `Synced N commands to guild`
- [ ] `./scripts/smoke_check.sh` — all HTTP + metric checks pass
- [ ] `python scripts/seed.py` — registry seeded with 5 settlements + 6 citizens

---

## Phase 0 — Backup, health, naming

- [ ] `/data backup note:smoke` → "Backup created" + a `.sql` file appears in `backups/`
- [ ] `/data list_backups` → your smoke backup is listed
- [ ] `curl http://localhost:10000/healthz` → `{"status":"ok","gateway":true,"db":true,...}`
- [ ] `curl http://localhost:10000/metrics` → contains `lambat_citizens_total`, `lambat_settlements_total`, etc.

---

## Phase 1 — Quality foundation

- [ ] `ruff format --check .` → clean
- [ ] `ruff check .` → clean
- [ ] `mypy` (no args) → "Success: no issues found in 28 source files"
- [ ] `pytest` → 294 passed
- [ ] Bot log lines are structured (`[info     ]` tag from structlog)
- [ ] `/metrics` exposes all 7 mandated metrics (smoke_check.ps1 verified this)

---

## Phase 2 — Auditability + data model

- [ ] `/settlement add name:Testville duchy:Capital` → "Added settlement"
- [ ] `/settlement list` → shows Testville under Capital (with the capital emoji)
- [ ] `/settlement remove name:Testville` → confirmation button → removed
- [ ] `/audit search` → paginated list of every mutation you just made (settlement add/remove)
- [ ] Audit channel in Discord got an embed for each mutation
- [ ] `/emoji set namespace:settler emoji:🎯 value:🛡️` → "Emoji updated"
- [ ] `/emoji list` → shows your new emoji
- [ ] `/citizen add ign:TestUser user:@you settlement:Lambat City recruiters:@you` → citizen registered + you got the Citizen/Settler/Lambat City roles
- [ ] `/citizen dossier ign:TestUser` → shows your join date, recruiters, activity (⚪ if no CivInfo key)
- [ ] `/citizen update ign:TestUser settlement:New September` → your roles updated (Lambat City removed, New September added)
- [ ] `/citizen recruited-by user:@you` → lists TestUser as someone you recruited

---

## Phase 3 — Feature expansion

### 3.1 CSV bulk import
- [ ] Create a CSV with columns: `ign,discord_id,settlement,recruiter_ids` (3-4 rows)
- [ ] `/citizen import` → attach the CSV → dry-run preview shows the parsed rows
- [ ] Click **Confirm** → citizens inserted, audit entries written
- [ ] `/citizen list` → shows the newly-imported citizens
- [ ] Re-run with a CSV containing a duplicate IGN → dry-run reports the conflict, no insertion

### 3.2 Citizen search
- [ ] `/citizen search query:test` → returns all TestKingAlice, TestQueenBob, … (trigram ILIKE)
- [ ] `/citizen search query:lambat` → returns citizens in Lambat City (searches settlement column too)
- [ ] `/citizen search query:100000000000000001` → returns TestKingAlice (numeric query searches Discord ID)
- [ ] `/citizen search query:zzz` → empty result page (no crash)

### 3.3 Settlement info dashboard
- [ ] `/settlement info name:Lambat City` → embed shows: total citizens, activity breakdown (Active/Semi/Inactive counts), growth since last snapshot, top recruiters, member list
- [ ] `/settlement info name:New September` → similar dashboard for a different settlement

### 3.4 Self-service /apply
- [ ] As a NON-citizen Discord account (use an alt or ask a friend), run `/apply ign:NewApplicant settlement:Lambat City`
- [ ] APPLICATIONS_CHANNEL_ID channel got an embed with Approve/Reject buttons
- [ ] `/application list` (as Council) → shows the pending application
- [ ] Click **Approve** on the embed → NewApplicant becomes a citizen, audit + governance posts fire
- [ ] `/citizen dossier ign:NewApplicant` → confirms they're now registered
- [ ] Try `/apply` again from the same Discord account → blocked (one pending app per user)
- [ ] Submit another app, click **Reject** with a note → app status flips to rejected

### 3.5 Activity time-series chart
- [ ] `/report activity` → renders a time-series PNG from monthly_snapshots (may be empty if no snapshots exist yet — that's OK)
- [ ] `/report activity settlement:Lambat City` → per-settlement chart
- [ ] Run `python -c "import asyncio; from tasks.activity_monitor import ActivityMonitor; ..."` to manually trigger a snapshot, then re-run `/report activity` to see data

### 3.6 Activity export
- [ ] `/report export` → DMs you a CSV with citizen columns only
- [ ] `/report export include_activity:True` → CSV now has an `activity` column with values like "Active", "Semi-active", "Inactive", "Unknown"
- [ ] Open the CSV in a spreadsheet — rows match `/citizen list`

### 3.7 Governance notifications
- [ ] Every mutation in the sections above also posted a plain-language embed to GOVERNANCE_CHANNEL_ID (e.g. "🟢 New Citizen: TestUser joined Lambat City")
- [ ] Governance embeds are DIFFERENT from the terse audit embeds (less technical, more readable)
- [ ] If GOVERNANCE_CHANNEL_ID == AUDIT_CHANNEL_ID, only one post appears (no duplicate)

### 3.8 CivMC online-now list
- [ ] `/server online` → fetches mcsrvstat `players.list`, cross-references with the registry
- [ ] Lambat citizens appear at the top with their settlement name
- [ ] Other online players listed below
- [ ] If CivMC is empty, the embed says so (no crash)
- [ ] If mcsrvstat is unreachable, the embed degrades gracefully (no 500)

---

## Phase 4 — Hardening & ops polish

### 4.1 Graceful shutdown (SIGTERM)
- [ ] `docker compose stop bot` → logs show an orderly close (loops cancelled, DB pool released, gateway closed) within `SHUTDOWN_GRACE_SECONDS`
- [ ] No `SIGKILL` warning in the docker logs (i.e. shutdown finished within the grace window)

### 4.2 Dockerfile HEALTHCHECK
- [ ] `docker inspect lambat-bot --format '{{.State.Health.Status}}'` → `healthy`
- [ ] `docker inspect lambat-bot --format '{{json .State.Health.Log}}'` → recent entries show exit 0

### 4.3 Rate-limit-aware bulk role ops
- [ ] Bulk role sync (ROLE_SYNC_AUTO=true + a citizen with a missing role) completes without hitting Discord's 429; the `rate_limit_guard` log lines show spaced retries if any

### 4.4 Matplotlib Filipino glyph font
- [ ] `/report trends` → PNG renders without tofu boxes (DejaVu Sans covers Latin + common glyphs)

### 4.5 i18n (LOCALE=en default; fil stretch)
- [ ] With `LOCALE=en`, `/help` shows English strings
- [ ] Set `LOCALE=fil`, restart, `/help` → Filipino /help embed (other strings fall back to en — documented)

### 4.6 Snapshot annotations + /snapshot cog
- [ ] `/snapshot annotate date:<a monthly snapshot date> note:Test annotation` → "Snapshot annotated"
- [ ] `/snapshot list` → shows the annotated snapshot with its note
- [ ] `/snapshot clear date:<same>` → note removed
- [ ] `/report trends` → annotated months show the note text on the chart

### Phase B (CivInfo plan) — /server trends + /factory
- [ ] `/server trends period:Last 24 hours` → embed with a 24h player-count sparkline/summary
- [ ] `/factory info <factory>` → setup cost + recipes
- [ ] `/factory list` → all FactoryMod factories
- [ ] `/factory recipe <recipe>` → inputs + outputs

---

## Background tasks (wait or trigger manually)

- [ ] Watch logs at 02:00 UTC → `daily_check` runs (CivInfo refresh)
- [ ] Watch logs at 02:00 UTC → `daily_backup` runs (pg_dump)
- [ ] On the 1st of the month → monthly census report posted to MONTHLY_REPORT_CHANNEL_ID + snapshot saved
- [ ] Uptime monitor polls every 5 min — check logs for `UptimeMonitor` INFO lines
- [ ] Weekly role sync runs on ROLE_SYNC_WEEKLY_DAY at ROLE_SYNC_WEEKLY_HOUR UTC

---

## Clean teardown

- [ ] Ctrl-C the bot → log shows clean shutdown ("Bot shutdown complete.")
- [ ] `docker compose down` (if using compose) — containers stop cleanly
- [ ] `docker compose down -v` — wipes the Postgres volume for a fresh re-test

---

## What to do if something fails

1. **Check `lambat_bot.log`** — every error is there with a traceback.
2. **Re-run `python scripts/preflight.py`** — catches config / permission regressions.
3. **Re-run `./scripts/smoke_check.sh`** — catches HTTP / metrics regressions.
4. **Check the DB directly:**
   ```bash
   docker exec -it lambat-db psql -U lambat -d lambat -c "SELECT * FROM citizens LIMIT 5;"
   docker exec -it lambat-db psql -U lambat -d lambat -c "SELECT * FROM audit_log ORDER BY ts DESC LIMIT 10;"
   ```
5. **File an issue** at https://github.com/trinik15/LambatRegistryBot/issues with:
   - The failing command + its error message
   - The relevant log lines
   - Output of `python scripts/preflight.py` and `./scripts/smoke_check.sh`
