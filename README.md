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
main.py                  — Bot entry point, setup_hook, /sync command, error handler
core/
  config.py              — Environment-based configuration (validated on import)
  database.py            — asyncpg connection pool + idempotent schema migrations
  constants.py           — Colors, limits, Lambat settlement/duchy emoji mappings
api/
  civinfo_api.py         — CivInfo activity client (per-entry TTL cache, auth-broken
                           graceful degradation)
services/
  backup.py              — pg_dump/psql wrapper (path-traversal-safe, timeout-protected)
  role_manager.py        — Discord role assignment/removal (with Forbidden handling)
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
  http_keepalive.py      — Tiny HTTP server for host liveness checks
utils.py                 — PaginationView, date parsing/formatting helpers
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
