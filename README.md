# Lambat Registry Bot

A Discord bot for managing the citizen registry of the **Lambat nation** on
[CivMC](https://civmc.net) (a Minecraft civilization server). Tracks citizens,
settlements, activity, and population trends — and monitors the CivMC server
for outages.

---

## Features

| Command | Description | Access |
|---|---|---|
| `/citizen add` | Register a new citizen (IGN, Discord, settlement, recruiters, address) | Council |
| `/citizen update` | Update any citizen field (settlement, Discord user, address, etc.) | Council |
| `/citizen remove` | Remove a citizen (with confirmation button) | Council |
| `/citizen list` | List all citizens grouped by settlement (paginated) | View |
| `/citizen dossier` | Full dossier for one citizen (activity, recruiters, join date) | View |
| `/settlement add` | Add a new settlement | Council |
| `/settlement remove` | Remove an empty settlement | Council |
| `/settlement list` | List all settlements | Everyone |
| `/report census` | Live population + activity report (paginated, per settlement) | View |
| `/report stats` | Quick population statistics | View |
| `/report trends` | Historical population trend charts (PNG, from monthly snapshots) | View |
| `/report export` | Download all citizen data as CSV | View |
| `/server status` | Live CivMC server status (players, version, MOTD, icon) | Everyone |
| `/server ping` | Quick one-line server check | Everyone |
| `/data backup` | Manual database backup | Council |
| `/data list_backups` | List all available backups | Council |
| `/data restore` | Restore database from a backup (with emergency pre-restore backup) | Council |
| `/sync` | Re-sync slash commands to the server | Owner |
| `/help` | Show this command list | Everyone |

### Background tasks

- **Daily activity check** (02:00 UTC): refreshes CivInfo activity cache for all
  citizens. On the 1st of each month, generates and posts the monthly census
  report and saves a population snapshot for `/report trends`.
- **Daily backup** (02:00 UTC): automatic `pg_dump` backup.
- **Uptime monitor** (every 5 min): polls mcsrvstat.us and posts
  edge-triggered outage/recovery alerts to `ALERT_CHANNEL_ID`.
- **HTTP keep-alive**: tiny HTTP server on `PORT` so hosts like Render don't
  mark the bot as idle.

---

## Prerequisites

1. **Python 3.11+**
2. **PostgreSQL 12+** (with the `citext` extension — the bot creates it
   automatically on startup)
3. **`pg_dump` and `psql`** on the PATH (for backup/restore). On Debian/Ubuntu:
   `apt install postgresql-client`.
4. A **Discord bot application** with:
   - **Privileged Gateway Intents → SERVER MEMBERS** enabled (required to
     assign/remove roles).
   - The bot invited to your server with `applications.commands` and `bot`
     scopes, and **Manage Roles** permission (or it must be granted the specific
     roles it manages).
   - The bot's **top role must be above** every role it assigns (Citizen,
     Settler, settlement roles). Otherwise `add_roles` silently fails with
     `discord.Forbidden`.

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/trinik15/LambatRegistryBot.git
cd LambatRegistryBot
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in **at least** these required values:

| Variable | How to get it |
|---|---|
| `DISCORD_TOKEN` | Discord Developer Portal → your application → Bot → Reset Token |
| `DATABASE_URL` | Your Postgres connection string |
| `OWNER_ID` | Right-click your own username in Discord → Copy ID (enable Developer Mode) |
| `GUILD_ID` | Right-click your server name → Copy ID (**recommended** — enables instant command sync) |
| `CITIZEN_ROLE_IDS` | Comma-separated role IDs the bot assigns to registered citizens |
| `FULL_ACCESS_ROLE_IDS` | Comma-separated role IDs for council members who can add/remove citizens |

See `.env.example` for all options and their descriptions.

### 3. Database

The bot creates all tables and runs migrations automatically on startup (no
manual SQL needed). The only requirement is that PostgreSQL is reachable and
the `citext` extension can be created (the bot runs `CREATE EXTENSION IF NOT
EXISTS citext` — the DB user needs CREATE privilege, which is the default for
the database owner).

### 4. Run

```bash
python main.py
```

On first startup, the bot syncs slash commands. If `GUILD_ID` is set, commands
appear instantly. If not, they may take up to 1 hour to propagate globally (in
that case, you can run `/sync` as the owner to force a re-sync).

### 5. First-time setup in Discord

1. Use `/settlement add` to create your nation's settlements (e.g. "New
   September", "Timberbourg", …). Settlement names must match the Discord role
   names you want the bot to assign — the bot looks up settlement roles by
   name.
2. Use `/citizen add` to register your first citizens. You'll need their IGN
   (Minecraft username), Discord user, and settlement.

---

## Quick E2E testing

The `scripts/` directory contains four tools that turn the 6-step manual
testing flow into ~5 minutes of work. Run them in this order:

### 1. Pre-flight checks (before starting the bot)

Validates that your `.env` is correct, the bot can reach your guild, the
role hierarchy is right, channels are visible, and slash commands are synced —
all without launching the full gateway.

```bash
python scripts/preflight.py
```

Catches the 5 most common "why isn't my bot working" mistakes in <5 seconds.
See `python scripts/preflight.py --help` for skip options.

### 2. Bring up Postgres + bot together

```bash
docker compose up --build
```

This starts Postgres 16 (with a healthcheck) and the bot, wired together. The
bot's `DATABASE_URL` is overridden to point at the compose `db` service, so
you don't need to set it in `.env`. Logs stream to your terminal; look for
`Discord gateway: READY` + `Synced N commands`.

To wipe the DB and start fresh: `docker compose down -v`.

### 3. Smoke-check the HTTP endpoints

In a second terminal, while the bot is running:

```bash
./scripts/smoke_check.sh          # Mac / Linux / Git Bash
.\scripts\smoke_check.ps1         # Windows PowerShell
```

Verifies `/healthz` returns 200 with `status=ok, gateway=true, db=true`, and
`/metrics` exposes all 7 required Prometheus metric names.

### 4. Seed test data

```bash
python scripts/seed.py
```

Idempotently inserts 5 settlements + 6 test citizens (with fake Discord IDs)
so read commands like `/citizen list`, `/report census`, and
`/settlement info` have something to show immediately. Re-run safely; pass
`--reset` to wipe first. (This script does NOT touch Discord — use
`/citizen add` in Discord to test the role-assignment path with real users.)

### 5. Walk the command checklist

Open [`scripts/E2E_CHECKLIST.md`](scripts/E2E_CHECKLIST.md) and tick through
every slash command, grouped by phase. Covers all 25+ commands including
Phase 3's CSV import, `/citizen search`, `/settlement info`, self-service
`/apply`, activity charts, governance notifications, and the CivMC
online-now list.

### One-shot test loop

```bash
# Mac / Linux / Git Bash
python scripts/preflight.py     && \
docker compose up --build -d    && \
sleep 8                         && \
./scripts/smoke_check.sh        && \
python scripts/seed.py          && \
echo "✅ Ready for Discord E2E — open scripts/E2E_CHECKLIST.md"
```

```powershell
# Windows PowerShell
python scripts\preflight.py
if ($LASTEXITCODE -eq 0) {
    docker compose up --build -d
    Start-Sleep -Seconds 8
    .\scripts\smoke_check.ps1
    if ($LASTEXITCODE -eq 0) { python scripts\seed.py }
}
```

---

## CivInfo API key (activity tracking)

The bot uses [CivInfo](https://civinfo.net) to check when citizens last logged
into CivMC. The API now **requires authentication**. To get a key:

1. Email `minecraft.gjum@gmail.com` requesting a CivInfo API key.
2. Set `CIVINFO_API_KEY=your_key` in `.env`.

**Without a key**, the bot still works — it degrades gracefully:
- `/citizen add` succeeds with a warning ("Activity Unverified").
- `/report census` and `/report stats` show "⚠️ Activity data unavailable"
  instead of fake "0 active" counts.
- `/citizen dossier` shows ⚪ "API Auth Required" for activity.

### Phase A: mc-accounts/full endpoint + richer activity data

The bot now calls the **`mc-accounts/full`** endpoint (instead of the legacy
`mc-sessions/all`). One call returns `first_joined`, `last_login`, AND
`last_logout` — giving us three capabilities we didn't have before:

- **"Online now" detection** — `last_login > last_logout` means the player is
  currently logged in, with no separate mcsrvstat.us call needed.
- **First-joined date** — when the player first joined CivMC (distinct from
  our registry `join_date`, which is when they joined Lambat).
- **Accurate activity metrics** — the daily activity loop now persists to the
  `activity_cache` DB table (previously only the in-memory cache was refreshed,
  so the `ACTIVE_CITIZENS` Prometheus gauge and `/report export` were stale).

Additional env vars (all have sensible defaults — only override for testing):
- `CIVINFO_API_BASE` — defaults to `https://api.civinfo.net`.
- `CIVINFO_MC_SERVER` — defaults to `play.civmc.net` (the full server address,
  matching Gjum's official frontend — not the short form `civmc`).
- `CIVINFO_FRONTEND_VERSION` — the `civinfo-version` header git hash, sent on
  every request to mirror the official frontend. Observational today; update
  if Gjum ever makes it required.

---

## Deployment

### Docker

```bash
docker build -t lambat-registry-bot .
docker run -d --env-file .env -p 10000:10000 lambat-registry-bot
```

The Dockerfile runs as a non-root user and installs `postgresql-client` for
backup/restore.

### Render / Railway / Fly.io

1. Connect your GitHub repo.
2. Set the build command to `pip install -r requirements.txt`.
3. Set the start command to `python main.py`.
4. Add all `.env` variables in the dashboard.
5. Add a PostgreSQL database add-on and set `DATABASE_URL` to the provided URL.

---

## Backup & Restore

- **Automatic**: a `pg_dump` backup runs daily at 02:00 UTC.
- **Manual**: `/data backup` (Council only). Optional `note` is sanitized to a
  filename-safe slug.
- **Restore**: `/data restore <filename>` (Council only). Creates an emergency
  backup before overwriting. All in-memory caches are invalidated after
  restore so the bot immediately reflects the restored data.
- Backups use `--no-owner --no-privileges` so they're portable across
  different Postgres users.

Backups are stored in `BACKUP_DIR` (default: `backups/`). Each `.sql` file has
a companion `.sql.meta` file with the type, note, and timestamp.

---

## Architecture

```
main.py                  — Bot entry point (LambatRegistryBot), setup_hook, /sync command, error handler
core/
  config.py              — Environment-based configuration (validated on import)
  database.py            — asyncpg connection pool + idempotent schema migrations
  constants.py           — Colors, limits, Lambat settlement/duchy emoji mappings
api/
  civinfo_api.py         — CivInfo activity client (per-entry TTL cache, auth-broken
                           graceful degradation)
services/
  backup.py              — pg_dump/psql wrapper (path-traversal-safe, timeout-protected,
                           off-site sink upload + retention pruning)
  backup_sinks.py        — Pluggable off-site backup destinations (local / S3 / GCS)
  role_manager.py        — Discord role assignment/removal (case-insensitive, Forbidden handling)
  charts.py              — matplotlib (Agg) chart rendering for /report trends
tasks/
  activity_monitor.py    — Daily activity check + monthly census report + snapshots
  uptime_monitor.py      — Edge-triggered CivMC outage/recovery alerts
cogs/
  citizen.py             — /citizen add, update, remove, list, dossier
  settlement.py          — /settlement add, remove, list
  reports.py             — /report census, stats, trends, export
  server.py              — /server status, ping (mcsrvstat.us)
  data.py                — /data backup, list_backups, restore
  help.py                — /help command
web/
  health.py              — Keep-alive + /healthz (honest liveness) + /metrics (Prometheus)
utils.py                 — PaginationView, date parsing/formatting helpers
scripts/                 — E2E testing toolkit (see "Quick E2E testing" above)
  preflight.py           — Pre-launch checks: token, guild, role hierarchy, channels, sync
  seed.py                — Idempotent test-data seeder (5 settlements + 6 citizens)
  smoke_check.sh         — /healthz + /metrics HTTP smoke check (Mac/Linux/Git Bash)
  smoke_check.ps1        — Same, native Windows PowerShell version
  E2E_CHECKLIST.md       — 25+ command walk-through, grouped by phase
docker-compose.yml       — Postgres 16 + bot, wired together with healthcheck
```

---

## Important notes

### Settlement roles are looked up by NAME

The bot finds the settlement role in Discord by matching the role **name** to
the settlement name in the registry. If you rename a role in Discord, the bot
won't find it. Keep settlement role names in sync with `/settlement` names.

### Scheduled tasks run at 02:00 UTC

The daily activity check and daily backup both run at 02:00 UTC. Adjust the
hour in `before_daily_check` / `before_daily_backup` if you need a different
time.

### `SETTLEMENT_TO_DUCHY` mapping

`tasks/activity_monitor.py` contains a hardcoded mapping of settlement names to
duchies (provinces) for the monthly census report. If you add a new settlement
that isn't in this map, it will show "Unknown" as its duchy. Add new entries
to the `SETTLEMENT_TO_DUCHY` dict as needed.

### IGN case-insensitivity

Minecraft usernames are case-insensitive. The `citizens.ign` and
`activity_cache.ign` columns use PostgreSQL's `CITEXT` type, so "Notch" and
"NOTCH" are treated as the same citizen. The bot validates IGNs against
`^[A-Za-z0-9_]{3,16}$` (Mojang's rules).

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Slash commands don't appear | Run `/sync` (owner only). If `GUILD_ID` is not set, commands may take up to 1 hour to propagate globally. |
| "Bot lacks permission to assign roles" | Move the bot's top role ABOVE the roles it manages (Citizen, Settler, settlement roles) in Server Settings → Roles. |
| Activity shows ⚪ for everyone | `CIVINFO_API_KEY` is not set or was rejected. Email minecraft.gjum@gmail.com for a key. The bot still works without it. |
| `/citizen add` says "IGN not found" | The IGN doesn't exist on CivMC (or CivInfo is down). Check the spelling. |
| Monthly report not posted | Check `MONTHLY_REPORT_CHANNEL_ID` is set and the bot can send messages there. The report runs on the 1st of each month at 02:00 UTC. |
| No outage alerts | Check `ALERT_CHANNEL_ID` is set. The monitor still runs (check logs) even if the channel is 0. |

---

## License

This project is for the Lambat nation on CivMC. Contact the repository owner
for usage details.
