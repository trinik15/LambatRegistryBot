# Lambat Registry Bot — Development Roadmap

> **Language pledge:** This roadmap stays 100% in **Python**. Every proposed change is
> implemented in the existing stack (discord.py 2.6 / asyncpg / aiohttp / matplotlib).
> No rewrite, no migration to another language or framework.

---

## 0. How this roadmap was built

This document is the result of:

1. **Web research** — CivMC server, the Kingdom of Lambat (a real CivMC nation with
   five duchies + a capital, part of the Lyrean Commonwealth, Filipino-themed), the
   CivInfo activity API, mcsrvstat.us, and discord.py cog architecture.
2. **A full clone** of `trinik15/LambatRegistryBot` (81 commits, `38e6e18` HEAD).
3. **A line-by-line read of every file** — all 24 Python modules (~3,694 lines),
   `README.md`, `.env.example`, `Dockerfile`, `.gitignore`, `.dockerignore`,
   `requirements.txt`, and the full git log.

Every finding below cites a real file and (where useful) a real code location, so a
future contributor can verify it directly instead of trusting this document.

---

## 1. What this project actually is

| | |
|---|---|
| **Domain** | The **Kingdom of Lambat**, a player nation on **CivMC** — a Minecraft civilization server where gameplay revolves around politics, economy, diplomacy, and reinforcement-based territory protection. Lambat has five duchies (Lambat City, Florraine, Valle Occidental, Capeland, Margaritaville) plus the capital, and is part of the Lyrean Commonwealth. |
| **Purpose** | A **Discord bot** that acts as the nation's civil registry: it tracks citizens (by Minecraft IGN + Discord user), the settlement/duchy they belong to, who recruited them, their CivMC login activity, and population trends over time. It also watches the CivMC server itself for outages. |
| **User (not "Trinidad vehicle registry"!)** | Despite the maintainer's `trinik15` handle, this has **nothing to do with Trinidad & Tobago vehicle transfers.** That was a false lead from keyword matching "registry." The real user is Lambat's leadership/council on Discord. |
| **Runtime** | Python 3.11+, PostgreSQL 12+ (with `citext`), `pg_dump`/`psql` on PATH, a single Discord guild. Designed for free-tier hosts (Render/Railway/Fly.io) — hence the HTTP keep-alive server. |

### 1.1 The feature surface today (verified from code)

| Command group | Commands | Access |
|---|---|---|
| `/citizen` | `add`, `update`, `remove`, `list`, `dossier` | Council (write) / View (read) |
| `/settlement` | `add`, `remove`, `list` | Council (write) / Everyone (list) |
| `/report` | `census`, `stats`, `trends`, `export` | View |
| `/server` | `status`, `ping` | Everyone |
| `/data` | `backup`, `list_backups`, `restore` | Council |
| `/sync`, `/help` | — | Owner / Everyone |

### 1.2 Background work (verified from code)

- **Daily activity check @ 02:00 UTC** — refreshes CivInfo activity for every citizen; on the 1st of the month, generates the monthly census report and persists a population snapshot (`tasks/activity_monitor.py`).
- **Daily backup @ 02:00 UTC** — `pg_dump` (`main.py::daily_backup`).
- **Uptime monitor @ every 300s** — polls mcsrvstat.us, edge-triggered outage/recovery alerts with a 2-poll confirmation threshold (`tasks/uptime_monitor.py`).
- **HTTP keep-alive** — tiny `http.server` on `PORT` so hosts don't idle-kill the process (`web/http_keepalive.py`).

---

## 2. Architecture (as read from the code)

```
main.py                         — PaviaBot(commands.Bot): setup_hook, /sync, error handler,
                                   daily_backup loop, graceful close.
core/
  config.py                     — Class-level env loading; validates on import (raises on bad config).
  database.py                   — asyncpg pool (double-checked locking), idempotent migrations
                                   (TEXT→CITEXT for ign, TEXT→DATE for join_date), execute_query helper.
  constants.py                  — Colors, Limits, and hardcoded Lambat emoji/duchy mappings.
api/
  civinfo_api.py                — CivInfo client: per-IGN TTL cache (5/2/1 min by status),
                                   auth-broken flag with 10-min TTL, honest degradation.
services/
  backup.py                     — pg_dump/psql wrapper: path-traversal-safe, timeout-protected,
                                   emergency pre-restore backup, portable --no-owner dumps.
  role_manager.py               — Discord role add/remove/swap with Forbidden handling.
  charts.py                     — matplotlib (Agg) → PNG; 2- or 3-panel trend chart.
tasks/
  activity_monitor.py           — daily_check loop + monthly report + snapshot persistence.
  uptime_monitor.py             — state-machine outage monitor (online/offline/recovery).
cogs/
  citizen.py  (622 lines)       — the largest module; CRUD + dossier + autocomplete cache.
  reports.py  (393 lines)       — census (paginated), stats, trends (chart), export (CSV).
  data.py     (240 lines)       — backup/list/restore with confirm view + cache invalidation.
  server.py   (160 lines)       — live CivMC status via mcsrvstat.us.
  settlement.py (136 lines)     — settlement CRUD + name cache.
  help.py     (122 lines)       — /help embed + owner-only /sync.
web/
  http_keepalive.py             — 200-OK-on-any-path liveness server.
utils.py                        — PaginationView, date parse/format helpers.
```

**Database schema** (PostgreSQL, from `core/database.py`):

- `settlements(name TEXT PK)`
- `citizens(ign CITEXT PK, discord_id TEXT UNIQUE, settlement FK, recruiter_ids TEXT, address, mailbox, notes, join_date DATE)`
- `activity_cache(ign CITEXT PK FK→citizens, last_login TIMESTAMP, status TEXT, last_checked TIMESTAMP)`
- `monthly_snapshots(id SERIAL PK, snapshot_date DATE, duchy TEXT, district TEXT NULLABLE, total INT, active INT, UNIQUE(snapshot_date, duchy, district))`

---

## 3. Current-state assessment

### 3.1 Strengths (things to preserve)

1. **Production-grade resilience patterns.** Graceful CivInfo auth-broken degradation (won't lie about "0 active" — `api/civinfo_api.py:86`), emergency pre-restore backup (`services/backup.py:165`), edge-triggered outage alerts with a 2-poll confirmation threshold to avoid false alarms (`tasks/uptime_monitor.py:24`), and concurrent bounded CivInfo fetching with a semaphore (`tasks/activity_monitor.py:17`).
2. **Honest error surfacing.** Partial failures (role assignment fails after DB commit) are reported to the user, not swallowed (`cogs/citizen.py:255-311`). Truncated reports say so (`cogs/reports.py:172`). The monthly report annotates "Activity data unavailable" instead of faking zero.
3. **Idempotent migrations.** `init_db()` detects old TEXT columns and migrates them to CITEXT/DATE within a transaction, dropping and recreating the FK safely (`core/database.py:105-144`).
4. **Security hygiene.** Path-traversal-safe backup paths with `os.path.commonpath` belt-and-suspenders (`services/backup.py:32`), filename-safe note slugging, non-root Docker user, least-privilege intents (message_content intentionally off — `main.py:31`), `--no-owner --no-privileges` portable dumps.
5. **Operational fit.** Single-guild instant command sync, configurable cooldown tiers (fast/medium/slow/critical), per-IGN TTL cache absorbing repeat load, double-checked locking on the pool.

### 3.2 Gaps found while reading every line (priority-ranked)

These are concrete defects/risks I observed in the source, not generic advice.

#### 🔴 P0 — Correctness / data-integrity / security risks

| # | Finding | Location |
|---|---|---|
| G1 | **Backups live only on local disk (`backups/`).** If the host (Render/Railway) loses its ephemeral filesystem or the container is recreated, every backup is gone — including the "emergency pre-restore" safety net. This is the single biggest operational risk. | `services/backup.py:46`, `main.py:170` |
| G2 | **No backup retention policy.** Daily auto-backups accumulate forever; disk fills until the host kills the bot or `pg_dump` fails on ENOSPC. | `services/backup.py` (no pruning anywhere) |
| G3 | **`settlements.name` is `TEXT`, not `CITEXT`.** IGNs are case-insensitive (CITEXT), but settlement names are case-sensitive — so "New September" and "new september" are two different settlements. Role lookup by name (`discord.utils.get(guild.roles, name=settlement)`) is also case-sensitive, so a settlement added with the wrong case silently never gets a role assigned. | `core/database.py:66`, `services/role_manager.py:19` |
| G4 | **No audit log.** A governance registry with no record of *who added/updated/removed whom, and when*, is a liability. `logger.info` lines exist in `citizen_update` but are not queryable and don't capture adds/removes. | `cogs/citizen.py` (add/remove/update) |
| G5 | **`/citizen update` lets you set a `join_date` in the future.** `is_valid_date` only checks the format, not the value. A typo like `25/12/2099` is accepted and then corrupts the "recent joins (30d)" stat and the monthly snapshot. | `utils.py:60`, `cogs/citizen.py:405` |
| G6 | **`recruiter_ids` stored as comma-separated TEXT.** Cannot query "list everyone recruiter X sponsored" without a string split in Python. Also can't enforce that a recruiter is a registered citizen. | `core/database.py:74`, `cogs/citizen.py` |
| G7 | **Naming debt: the bot still thinks it's "PaviaBot".** The class is `PaviaBot` (`main.py:27`), the log file is `pavia_bot.log` (`main.py:20` and `.gitignore:169`), and `PROXY_URL` is read via `os.getenv` directly in `__init__` instead of through `Config` (`main.py:33`). Confusing for new contributors and leaks in logs. | `main.py:20,27,33` |

#### 🟠 P1 — Robustness & operability

| # | Finding | Location |
|---|---|---|
| G8 | **The HTTP keep-alive server always returns 200**, even if the DB pool is dead, the Discord gateway is disconnected, or CivInfo auth is broken. A host liveness probe that lies defeats the purpose of the probe. | `web/http_keepalive.py:15` |
| G9 | **No automated tests.** `.pytest_cache` is gitignored but there is no `tests/` directory and no test framework configured. Every migration and every "honest degradation" branch is unverified. | repo root |
| G10 | **No lint/format/type-check tooling.** No `pyproject.toml`, no ruff/black/mypy config, no pre-commit hook, no CI. The codebase mixes English and Italian comments (e.g. `services/backup.py:49`, `tasks/activity_monitor.py:73`) and is inconsistent. | repo root |
| G11 | **Hardcoded `SETTLEMENT_TO_DUCHY` dict.** The README itself flags this as fragile ("will show Unknown as its duchy"). Adding a settlement requires a code change + redeploy. | `tasks/activity_monitor.py:37` |
| G12 | **Hardcoded custom-emoji IDs** in `core/constants.py` are tied to one specific guild's uploaded emojis. If an emoji is deleted or the bot is ever used by another Lambat-affiliated server, the report renders broken `<:LCity:...>` literals. | `core/constants.py:30-69` |
| G13 | **No structured logging / no error tracking.** `logging.basicConfig` to a file + stdout. No JSON logs for log aggregation, no Sentry, no `on_resumed`/`on_disconnect` hooks for gateway observability. | `main.py:16` |
| G14 | **No automatic role reconciliation.** If a citizen's Discord account is deleted, or someone manually adds/removes a role in Discord, the DB and Discord drift. There's no periodic "are the DB roles in sync with Discord roles?" task. | (absent) |
| G15 | **`/report census` fetches CivInfo for every citizen on every call.** The 5-min TTL cache helps, but on a cold cache for a large registry this can approach Discord's 15-min interaction window (the code already had to fix this once — see the comment at `tasks/activity_monitor.py:14`). | `cogs/reports.py:84` |
| G16 | **CSV export omits activity data** — only registry fields. Leadership exporting for offline analysis loses the most decision-relevant column. | `cogs/reports.py:366` |
| G17 | **`PG_TIMEOUT` is read from env in `services/backup.py` but is not documented in `.env.example`.** Operators won't discover it. | `services/backup.py:17` vs `.env.example` |

#### 🟡 P2 — Features & UX

| # | Finding | Location |
|---|---|---|
| G18 | **No bulk import.** Migrating from a spreadsheet or another tool means adding citizens one `/citizen add` at a time — painful for a nation of hundreds. | (absent) |
| G19 | **No `/citizen search`.** You can autocomplete by IGN, but can't search by partial IGN, Discord user, settlement, or "recruited by X". | `cogs/citizen.py` |
| G20 | **No `/settlement info`.** No single-settlement detail view (population, active rate, growth, member list). `/report census <settlement>` is the closest, but it's a census, not a dashboard. | `cogs/settlement.py` |
| G21 | **No self-service citizen application flow.** Every add is council-only. A `/apply` command that creates a pending request for council approval would reduce council toil. | (absent) |
| G22 | **No notification channel for governance events.** When a citizen is added/removed, nothing is posted to a (configurable) audit/modlog channel — the action just happens silently. | `cogs/citizen.py` |
| G23 | **No per-settlement or per-duchy leader/representative role** beyond the flat citizen/settler roles. | `services/role_manager.py` |
| G24 | **No `/report activity` time-series for a single citizen or settlement** — only the national trend chart. | `cogs/reports.py` |
| G25 | **Charts use the Agg backend fine, but font support is default.** Lambat uses Filipino names ("Mabuhay", "Kahiran ng Lambat") and accented settlement names ("Tierra del Cabo", "Bazariskes"). Default matplotlib fonts may render these poorly. | `services/charts.py:29` |
| G26 | **No i18n.** The bot is English-only with Italian dev comments. Lambat is Filipino-themed; a Filipino (`fil`) locale for at least the `/help` and monthly report would resonate with the community. | `cogs/help.py`, `tasks/activity_monitor.py:180` |

#### 🟢 P3 — Polish & modernization

| # | Finding | Location |
|---|---|---|
| G27 | **No `pyproject.toml` / modern packaging.** Only `requirements.txt` with loose `>=` pins; no lockfile, no dev-dependency separation. | `requirements.txt` |
| G28 | **Dockerfile has no `HEALTHCHECK`.** The keep-alive server exists but Docker doesn't probe it. | `Dockerfile` |
| G29 | **No Prometheus `/metrics` endpoint.** The keep-alive server could expose citizen count, active rate, CivInfo cache hit rate, uptime state, last outage duration — all useful for a Grafana board. | `web/http_keepalive.py` |
| G30 | **`monthly_snapshots` has no context/notes field.** Can't annotate "snapshot taken during the Great Diamond Crisis week" for historical context. | `core/database.py:92` |
| G31 | **No graceful SIGTERM handling** beyond `KeyboardInterrupt` in `main()`. Container orchestrators send SIGTERM, not SIGINT. | `main.py:220` |

---

## 4. The Roadmap (phased, all in Python)

Each phase is independently shippable. Effort estimates are rough (S ≈ half a day,
M ≈ 1–2 days, L ≈ 3–5 days) and assume a single contributor who knows discord.py.

> **Status:** ✅ Phase 0 **implemented** (commit `b9d0d68`).
> ✅ Phase 1 **implemented** — `pyproject.toml` (ruff/mypy/pytest), GitHub Actions
> CI, 54 tests covering the honesty branches, structlog JSON logging + gateway
> lifecycle hooks + optional Sentry, and a Prometheus `/metrics` endpoint.
> ✅ Phase 2 **implemented** — audit log, recruiters junction, duchy column,
> DB-backed emojis, role-sync weekly task (commit `2bfd457`).
> ✅ Phase 3 **implemented** — CSV import, citizen search, settlement dashboard,
> self-service applications, activity time-series + activity export, governance
> notifications, CivMC online-now list (168 tests, all green).
> Phases 4–5 remain planned.

### Phase 0 — Stabilize & de-risk (before adding anything new)

**Goal:** close the operational holes that could lose data or mislead leadership.

| ID | Task | Effort | Touches |
|---|---|---|---|
| **0.1** | **Off-site backup upload.** Add a `BackupSink` abstraction with two implementations: `LocalSink` (current behaviour) and `S3Sink` (boto3) / `GCSSink` (google-cloud-storage). After every `create_backup`, push the `.sql` + `.meta` to the configured sink. Make the sink configurable via `BACKUP_SINK=local\|s3\|gcs` + creds. | M | `services/backup.py`, new `services/backup_sinks.py`, `core/config.py`, `.env.example` |
| **0.2** | **Backup retention policy.** Keep last N daily (default 30), all monthly (1st-of-month, flagged via the existing `type` meta), and never delete manual backups. Add `BACKUP_RETENTION_DAILY=N` and a pruning pass at the end of `create_backup`. | S | `services/backup.py` |
| **0.3** | **Real health endpoint.** Replace the always-200 handler with `/healthz` that returns 200 only when: DB pool is live (`SELECT 1`), Discord gateway is connected (`bot.is_ready() and not bot.is_closed()`), and CivInfo isn't in a long-auth-broken state. Add `/metrics` (Prometheus text format) in the same server. | M | `web/http_keepalive.py` → rename `web/health.py` |
| **0.4** | **`settlements.name` → CITEXT + role-lookup case-insensitivity.** Migration mirrors the existing `ign` migration. Update `role_manager` to match roles case-insensitively (`discord.utils.find(lambda r: r.name.lower() == settlement.lower(), guild.roles)`). | S | `core/database.py`, `services/role_manager.py` |
| **0.5** | **Join-date sanity.** Reject future dates and dates older than CivMC's launch (2022) in `is_valid_date`; add `MIN_JOIN_DATE` and a "not in the future" check. | S | `utils.py`, `cogs/citizen.py:405` |
| **0.6** | **Rename `PaviaBot` → `LambatRegistryBot`**, log file → `lambat_bot.log`, route `PROXY_URL` through `Config`. One-shot refactor, large goodwill. | S | `main.py`, `.gitignore`, `core/config.py` |

**Exit criteria:** a backup survives host loss; `/healthz` honestly reports state;
settlement roles work regardless of case; no future join dates; the codebase calls
itself Lambat everywhere.

---

### Phase 1 — Observability & quality foundation

**Goal:** make the bot measurable and the codebase safe to change.

| ID | Task | Effort | Touches |
|---|---|---|---|
| **1.1** | **Add `pyproject.toml`** with ruff (lint+format), mypy (strict on `core/` and `services/`, lenient on `cogs/`), and pytest config. Pin runtime deps with a lockfile (`uv` lock or `pip-tools`). | M | repo root |
| **1.2** | **CI via GitHub Actions** — matrix on Python 3.11/3.12: ruff check + format --check, mypy, pytest, and a `docker build` smoke step. Block PRs on failure. | M | `.github/workflows/ci.yml` |
| **1.3** | **Test the critical-honesty branches.** Unit tests for: `civinfo_api.is_auth_broken` TTL logic, `_ttl_for` status mapping, `backup._safe_backup_path` traversal rejection, `backup._sanitize_note`, `utils.parse_join_date`/`format_date` across DATE/datetime/string inputs, and the `UptimeMonitor` state machine (online→offline→recovery with duration). Use `pytest-asyncio` + `asynctest`/`unittest.mock` for the pool. | L | new `tests/` |
| **1.4** | **Structured logging + error tracking.** Switch to `structlog` (JSON when `LOG_FORMAT=json`), add `bot.add_listener` for `on_resumed`/`on_disconnect`/`on_guild_available` to log gateway lifecycle, and optional Sentry hook via `SENTRY_DSN`. | M | `main.py`, `core/config.py` |
| **1.5** | **Prometheus metrics.** Expose counters/gauges: `lambat_citizens_total`, `lambat_active_citizens`, `lambat_settlements_total`, `lambat_civinfo_cache_hits_total`, `lambat_civinfo_auth_broken` (gauge), `lambat_civmc_online` (gauge), `lambat_last_outage_duration_seconds`. | M | `web/health.py`, new `core/metrics.py` |

**Exit criteria:** CI is green on every PR; the honesty branches are covered; a Grafana
board could be built off `/metrics`.

---

### Phase 2 — Data-model normalization & auditability  ✅ COMPLETE

**Goal:** make the registry queryable and accountable.

| ID | Task | Effort | Touches | Status |
|---|---|---|---|---|
| **2.1** | **Audit log table + emitter.** New `audit_log(id BIGSERIAL, ts TIMESTAMPTZ, actor_discord_id, action, target_ign, details JSONB)` table. A `services/audit.py` emits on every citizen add/update/remove and settlement add/remove. Add `/audit search` (Council) with filters by actor, action, target, date range. Also post a summary to an optional `AUDIT_CHANNEL_ID`. | L | new `services/audit.py`, `cogs/citizen.py`, `cogs/settlement.py`, `core/database.py`, new `cogs/audit.py` | ✅ |
| **2.2** | **Normalize `recruiter_ids` into a `recruiters` junction table.** `recruiters(ign FK→citizens, recruiter_discord_id, recruited_at)`. Migration back-fills from the comma-split. Enables `/citizen recruited-by @user` and recruiter leaderboard in `/report`. | M | `core/database.py`, `cogs/citizen.py`, `cogs/reports.py`, new `services/recruiters.py` | ✅ |
| **2.3** | **Promote `SETTLEMENT_TO_DUCHY` to a DB column.** Add `duchy TEXT NOT NULL` to `settlements` (nullable for the migration, then back-filled from the existing dict, then `NOT NULL`). `/settlement add` takes a `duchy` param; `/settlement list` groups by duchy. The monthly report reads from the DB instead of the hardcoded dict. | M | `core/database.py`, `cogs/settlement.py`, `tasks/activity_monitor.py`, `core/constants.py` | ✅ |
| **2.4** | **Configurable emoji mapping in DB.** Move `Emojis.PROVINCE` / `Emojis.DISTRICT` into a `guild_emojis(key TEXT, emoji_str TEXT)` table seeded from the current constants, with `/emoji set` (Council) for runtime updates. Decouples reports from one guild's emoji IDs. | M | new `core/emojis.py` (DB-backed), new `cogs/emoji.py` | ✅ |
| **2.5** | **Role reconciliation task.** Weekly `tasks.loop` that, for every citizen, checks their Discord member still has the citizen/settler/settlement roles and the guest role is absent; logs discrepancies to the audit channel. Auto-fix is opt-in (`ROLE_SYNC_AUTO=true`). | M | new `tasks/role_sync.py`, `main.py`, `core/config.py` | ✅ |

**Exit criteria:** every registry mutation is auditable; recruiters are first-class
queryable entities; duchies and emojis are data, not code. — **MET**

**What shipped:**
- `audit_log` table with indexes on ts/actor/action/target; transaction-aware
  `audit.emit()` (atomic with the mutation); best-effort Discord channel mirror
  via `AUDIT_CHANNEL_ID`; `/audit search` with actor/action/target/date filters.
- `recruiters` junction table back-filled from `citizens.recruiter_ids`;
  dual-write (junction + legacy cache) so existing reads keep working;
  `/citizen recruited-by` and `/report recruiters` leaderboard.
- `settlements.duchy TEXT NOT NULL`, back-filled from the seed dict;
  `/settlement add` requires a duchy; `/settlement list` groups by duchy;
  monthly report JOINs settlements instead of using the hardcoded dict.
- `guild_emojis` table seeded from `Emojis.PROVINCE`/`DISTRICT`; `core/emojis.py`
  with in-process cache; `/emoji set` + `/emoji list`; activity_monitor uses
  DB-backed lookups.
- `tasks/role_sync.py` weekly loop with `detect_role_issues()` pure helper;
  `ROLE_SYNC_AUTO` opt-in auto-fix; discrepancies logged to audit_log + channel.
- 55 new tests (audit vocabulary/summary/json, emoji validation/cache, recruiter
  cleaning, role-sync discrepancy detection). Total: 109 passing.

---

### Phase 3 — Feature expansion (the "more features" the community will feel)  ✅ COMPLETE

**Goal:** reduce council toil and give leadership richer tooling.

| ID | Task | Effort | Touches | Status |
|---|---|---|---|---|
| **3.1** | **Bulk import via CSV.** `/citizen import` (Council) accepts a CSV attachment with the same columns as `/report export`. Dry-run preview (shows conflicts: duplicate IGN, unknown settlement, bad IGN format) then a confirm button. Reuses `role_manager` and `civinfo_api` per row with the existing semaphore. | L | new `cogs/citizen.py` subcommand or `cogs/import_.py` | ✅ |
| **3.2** | **`/citizen search`.** Full-text-ish search across IGN, discord_id, settlement, recruiter. Paginated results reusing `PaginationView`. Adds a GIN index on `citizens` if needed. | M | `cogs/citizen.py` | ✅ |
| **3.3** | **`/settlement info <name>`.** Single-settlement dashboard embed: total, active rate, growth since last snapshot, top recruiters, member list (paginated). | M | `cogs/settlement.py` | ✅ |
| **3.4** | **Self-service `/apply`.** Non-citizen runs `/apply ign settlement recruiter`; creates a `citizen_applications` row (status=pending) and posts to an `APPLICATIONS_CHANNEL_ID` with Approve/Reject buttons. Council approval triggers the normal `citizen_add` path. | L | new `cogs/applications.py`, `core/database.py` | ✅ |
| **3.5** | **`/report activity <scope>`.** Time-series for a single citizen or a single settlement over the available snapshots (line chart). Extends `services/charts.py` with a `render_activity_series` function. | M | `cogs/reports.py`, `services/charts.py` | ✅ |
| **3.6** | **Activity export.** Extend `/report export` with an optional `include_activity=true` flag that joins `activity_cache` (and falls back to a live CivInfo fetch + cache) so the CSV has the Active/Semi/Inactive column leadership actually wants. | S | `cogs/reports.py` | ✅ |
| **3.7** | **Governance notifications.** Configurable `GOVERNANCE_CHANNEL_ID` that receives an embed on every citizen add/remove and settlement add/remove (read-only mirror of the audit log for the wider council). | S | `cogs/citizen.py`, `cogs/settlement.py`, `services/audit.py` | ✅ |
| **3.8** | **CivMC-player online-now list.** `/server online` lists currently-online players (mcsrvstat returns a `players.list`) cross-referenced with the registry, so leadership can see "which of *our* citizens are on right now." | S | `cogs/server.py` | ✅ |

**Exit criteria:** council adds citizens in bulk; citizens can apply themselves;
leadership has per-settlement and per-citizen activity dashboards. — **MET**

**What shipped:**
- `services/csv_import.py` pure parser with `parse_csv()` + `ParsedRow`/`ParseResult`
  dataclasses; validates IGN format, Discord ID, settlement existence, duplicate
  IGNs (both within CSV and against existing registry); handles UTF-8 BOM + case-
  insensitive headers. `/citizen import` with dry-run preview embed + ConfirmImportView
  (Confirm/Cancel buttons); imports via transaction-aware `_import_single_citizen`.
- `citizen_applications` table (BIGSERIAL PK, CITEXT ign, applicant_discord_id,
  settlement, recruiter, status, timestamps) + partial unique index preventing
  duplicate pending apps per user; `services/applications.py` data layer;
  `cogs/applications.py` with `/apply` (open to all), `/application list` (Council),
  ApplicationReviewView with Approve/Reject buttons. Approval runs the full
  citizen_add path (INSERT + recruiters junction + audit + governance mirror).
- `/citizen search` with ILIKE + pg_trgm GIN index for fast partial matching;
  searches IGN + settlement (and Discord ID + recruiter when query is numeric);
  paginated via PaginationView (10 per page).
- `/settlement info` dashboard: total citizens, activity breakdown (Active/Semi/
  Inactive from CivInfo batch fetch), growth since last snapshot, top recruiters,
  member list. `_compute_growth_text` pure helper tested.
- `render_activity_series` in `services/charts.py` — single-panel line chart with
  optional active/inactive stacked area. `/report activity <settlement?>` renders
  time-series from monthly_snapshots. `/report export` extended with
  `include_activity` flag (LEFT JOINs activity_cache + falls back to live CivInfo
  batch fetch for missing entries).
- `audit.post_to_governance_channel()` + `_build_governance_embed()` — plain-
  language embed for the wider council (non-technical audience). Wired into all
  5 mutation points (citizen add/update/remove, settlement add/remove).
  `GOVERNANCE_CHANNEL_ID` config + no-op when it equals `AUDIT_CHANNEL_ID`.
- `/server online` cross-references mcsrvstat `players.list` with the registry;
  `_partition_citizens` pure helper splits online players into citizens + non-
  citizens (case-insensitive CITEXT match); embed shows Lambat citizens first
  with their settlement, then other players.
- 59 new tests (csv_import parser, search embed builder, settlement growth,
  activity label mapping, activity chart rendering, online-now partition + embed,
  applications IGN validation + status constants). Total: 168 passing.

---

### Phase 4 — Hardening & ops polish

**Goal:** production-grade operational behaviour.

| ID | Task | Effort | Touches |
|---|---|---|---|
| **4.1** | **SIGTERM graceful shutdown.** Install a `signal.signal(SIGTERM, ...)` that cancels loops and calls `bot.close()`, so container orchestrators get a clean exit. Add `stop_timeout` for in-flight commands. | S | `main.py` |
| **4.2** | **Dockerfile `HEALTHCHECK`.** `HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/healthz').read()"` — uses the real `/healthz` from Phase 0.3. | S | `Dockerfile` |
| **4.3** | **Discord-rate-limit-aware bulk operations.** Wrap `role_manager` calls in a shared `discord_rate_limit_guard` that respects `bot.is_ws_ratelimited()` and backs off, so bulk import (3.1) doesn't get the bot rate-limited. | M | `services/role_manager.py` |
| **4.4** | **Matplotlib font with full Latin/Filipino glyph coverage.** Bundle a font (e.g. DejaVu Sans, which ships with matplotlib) explicitly via `font.family` rcParam; verify "ñ", "ü", accented settlement names render. | S | `services/charts.py` |
| **4.5** | **i18n scaffolding.** Extract user-facing strings to a `locales/{en,fil}.json` with a `tr(key, **kwargs)` helper. Start with `/help` and the monthly report (the two highest-visibility surfaces). Filipino is a stretch goal; the scaffolding is the valuable part. | L | new `core/i18n.py`, `cogs/help.py`, `tasks/activity_monitor.py` |
| **4.6** | **Snapshot annotations.** Add `notes TEXT` to `monthly_snapshots` and an optional `/snapshot annotate <date> <text>` (Council) so historical context survives. | S | `core/database.py`, new `cogs/snapshot.py` |

**Exit criteria:** clean container shutdown; honest Docker health; bulk ops don't trip
rate limits; charts render all settlement names correctly.

---

### Phase 5 — Future directions (deliberately speculative)

These are bigger bets, listed to seed discussion — not committed.

- **Multi-nation support.** Generalize from "Lambat" to any CivMC nation by making the duchy/emoji/settlement-role config fully guild-scoped. The schema already supports it; the hardcoded constants don't.
- **CivMC namemc/Nameless integration** as a fallback when CivInfo auth is broken, so activity tracking degrades less often.
- **A read-only web dashboard** (FastAPI + HTMX, still Python) for leadership who prefer a browser to Discord. Reuses the same `core/` and `services/` modules; gated behind Discord OAuth so only council can see it.
- **Pearl/bastion tracking.** CivMC nations care about reinforced assets; a `bastions`/`pearls` table tracked alongside citizens would be a natural extension of the registry's "where are our people and our stuff" mission.
- **Predictive churn alerts.** With enough monthly snapshots, a simple "citizen X has been Semi-Inactive for 2 months → likely to churn" nudge to their recruiter.

---

## 5. Sequencing recommendation

```
Phase 0 (de-risk)   ──►  Phase 1 (quality)  ──►  Phase 2 (data model)  ──►  Phase 3 (features)
                              │
                              └──►  Phase 4 (ops polish) can run in parallel with Phase 2/3
```

- **Do Phase 0 first** — it closes real data-loss and correctness risks that no amount
  of new features will compensate for.
- **Phase 1 next** — without tests and CI, Phase 2's migrations are dangerous.
- **Phase 2 before Phase 3** — the recruiter junction table, duchy column, and audit
  log make Phase 3's features (bulk import, applications, search) far easier to build.
- **Phase 4 slots in anytime** after Phase 1; each item is independent.

---

## 6. Open decisions for the maintainer

1. **Backup sink target.** S3 (most common) vs GCS vs Backblaze B2 (cheapest). Do you have an existing cloud account?
2. **Audit log retention.** Forever (cheap for text), or 2-year rolling?
3. **Self-service applications** — does Lambat's council *want* applicants to self-serve, or is council-gated add a deliberate filter?
4. **i18n scope.** Is Filipino a real goal or just aesthetic? If real, who translates?
5. **Multi-nation.** Is there appetite to share this bot with allied nations, or is it permanently Lambat-only? (Affects how aggressively to de-hardcode.)

---

## 7. What this roadmap deliberately does NOT do

- **Does not rewrite in another language.** Python stays. discord.py stays. asyncpg stays.
- **Does not switch databases.** PostgreSQL + asyncpg is correct for this workload; no move to SQLite/Redis/Mongo.
- **Does not add a web frontend as the primary surface.** Discord is where the users are; a web dashboard (Phase 5) is optional and read-only.
- **Does not change the deployment model.** Single-process, single-guild, free-tier-host-friendly remains the target.

---

*Roadmap authored from a full line-by-line read of commit `38e6e18` (81 commits,
~3,694 lines of Python). Every file reference above was verified against the source.*
