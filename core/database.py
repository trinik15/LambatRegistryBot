import asyncio
import logging

import asyncpg

from core.config import Config

logger = logging.getLogger(__name__)

DATABASE_URL = Config.DATABASE_URL
_pool: asyncpg.Pool = None
_pool_lock = asyncio.Lock()  # 🔒 Lock to prevent race conditions during pool creation


async def get_pool() -> asyncpg.Pool:
    """
    Get or create the database connection pool in a thread-safe manner.
    """
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:  # Double-checked locking
                logger.info("Initializing database connection pool with min_size=2, max_size=10")
                _pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=2,
                    max_size=10,
                    command_timeout=10,  # ⏱️ Prevent indefinite hanging queries
                    max_inactive_connection_lifetime=3600,  # 🗑️ Clean up idle connections
                )
                logger.info("Database connection pool created successfully.")
    return _pool


async def close_pool():
    """
    Gracefully close the database connection pool.
    """
    global _pool
    if _pool:
        logger.info("Closing database connection pool...")
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed.")


async def init_db():
    """
    Initialize database tables and indexes.
    Call this during bot startup after the connection pool is ready.

    Includes idempotent migrations:
      - citizens.ign / activity_cache.ign: TEXT -> CITEXT (case-insensitive)
      - citizens.join_date: TEXT (DD/MM/YYYY) -> DATE
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:  # noqa: SIM117 — nested transaction reads cleaner
            async with conn.transaction():
                # citext extension must exist before any CITEXT column can be
                # created (fresh installs) or altered into (upgrades).
                await conn.execute("CREATE EXTENSION IF NOT EXISTS citext")

                # --- Tables (fresh install uses CITEXT/DATE directly) ---
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS settlements (
                        name CITEXT PRIMARY KEY,
                        duchy TEXT NOT NULL
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS citizens (
                        ign CITEXT PRIMARY KEY,
                        discord_id TEXT UNIQUE NOT NULL,
                        settlement CITEXT NOT NULL,
                        recruiter_ids TEXT NOT NULL,
                        address TEXT,
                        mailbox TEXT,
                        notes TEXT,
                        join_date DATE NOT NULL,
                        FOREIGN KEY (settlement) REFERENCES settlements(name) ON DELETE RESTRICT
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS activity_cache (
                        ign CITEXT PRIMARY KEY,
                        last_login TIMESTAMPTZ,
                        last_logout TIMESTAMPTZ,
                        first_joined TIMESTAMPTZ,
                        status TEXT,
                        is_online BOOLEAN DEFAULT FALSE,
                        last_checked TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        -- Proposal 1 (CivInfo graceful degradation): TRUE when the
                        -- row's last_login is stale because CivInfo auth is broken
                        -- (the daily loop couldn't refresh it). Set en masse by
                        -- tasks/activity_monitor.mark_all_stale(); cleared to FALSE
                        -- on the next successful fetch (_persist_activities upsert +
                        -- /citizen add). Readers (churn alerts, metrics) skip stale
                        -- rows so they don't act on aging data.
                        stale BOOLEAN DEFAULT FALSE,
                        FOREIGN KEY (ign) REFERENCES citizens(ign) ON DELETE CASCADE
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS monthly_snapshots (
                        id SERIAL PRIMARY KEY,
                        snapshot_date DATE NOT NULL,
                        duchy TEXT,
                        district TEXT,
                        total INTEGER NOT NULL,
                        active INTEGER NOT NULL,
                        notes TEXT,
                        UNIQUE(snapshot_date, duchy, district)
                    )
                """)
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_citizens_settlement ON citizens(settlement)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_citizens_discord ON citizens(discord_id)"
                )
                # Phase A (WS-3): indexes on activity_cache for the two common
                # query patterns — "who is active" (last_login DESC) and "who is
                # online right now" (is_online = TRUE, partial index).
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_cache_last_login "
                    "ON activity_cache(last_login DESC)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_cache_is_online "
                    "ON activity_cache(is_online) WHERE is_online = TRUE"
                )

                # --- Phase 2.1: audit_log ---
                # Append-only ledger of every registry mutation (citizen
                # add/update/remove, settlement add/remove, role-sync
                # discrepancies). Queried by /audit search and optionally
                # mirrored to AUDIT_CHANNEL_ID. JSONB details hold the
                # field-level diff so the full history is reconstructable.
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        actor_discord_id TEXT,
                        action TEXT NOT NULL,
                        target_ign TEXT,
                        details JSONB
                    )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC)")
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_discord_id)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_ign)"
                )

                # --- Phase 2.2: recruiters junction table ---
                # Normalises citizens.recruiter_ids (comma-separated TEXT) into
                # first-class queryable rows. Back-filled below from the legacy
                # column. New writes go to BOTH (dual-write) so the legacy
                # column stays a correct denormalised cache until a future phase
                # drops it.
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS recruiters (
                        ign CITEXT NOT NULL,
                        recruiter_discord_id TEXT NOT NULL,
                        recruited_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (ign, recruiter_discord_id),
                        FOREIGN KEY (ign) REFERENCES citizens(ign) ON DELETE CASCADE
                    )
                """)
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_recruiters_recruiter "
                    "ON recruiters(recruiter_discord_id)"
                )

                # --- Phase 2.4: guild_emojis ---
                # Runtime-configurable emoji mapping, seeded from the hardcoded
                # Emojis.PROVINCE / Emojis.DISTRICT dicts in core/constants.py.
                # Decouples reports from one guild's custom emoji IDs so a guild
                # migration (or a different nation reusing the bot) doesn't need
                # a code change + redeploy.
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS guild_emojis (
                        key TEXT PRIMARY KEY,
                        emoji_str TEXT NOT NULL
                    )
                """)

                # --- Phase 2.3: settlements.duchy column ---
                # Promotes the hardcoded SETTLEMENT_TO_DUCHY mapping into a DB
                # column so duchy membership is data, not code. Added as
                # nullable (safe for existing installs), back-filled from the
                # seed dict, then enforced NOT NULL in a second step only when
                # every row has a value.
                col_duchy = await conn.fetchrow(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'settlements' AND column_name = 'duchy'"
                )
                if not col_duchy:
                    await conn.execute("ALTER TABLE settlements ADD COLUMN duchy TEXT")
                    logger.info("Added settlements.duchy column (nullable).")

                # --- Idempotent migrations for existing TEXT-based installs ---

                # ign TEXT -> CITEXT (citizens + activity_cache, preserving FK)
                col_ign = await conn.fetchrow(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = 'citizens' AND column_name = 'ign'"
                )
                if col_ign and col_ign["udt_name"] == "text":
                    # Drop the FK temporarily so both columns can be altered.
                    await conn.execute(
                        "ALTER TABLE activity_cache DROP CONSTRAINT IF EXISTS activity_cache_ign_fkey"
                    )
                    await conn.execute(
                        "ALTER TABLE citizens ALTER COLUMN ign TYPE CITEXT USING ign::CITEXT"
                    )
                    await conn.execute(
                        "ALTER TABLE activity_cache ALTER COLUMN ign TYPE CITEXT USING ign::CITEXT"
                    )
                    # Recreate the FK with the same ON DELETE CASCADE rule.
                    await conn.execute(
                        "ALTER TABLE activity_cache "
                        "ADD CONSTRAINT activity_cache_ign_fkey "
                        "FOREIGN KEY (ign) REFERENCES citizens(ign) ON DELETE CASCADE"
                    )
                    logger.info(
                        "Migrated citizens.ign and activity_cache.ign to CITEXT (case-insensitive)."
                    )

                # --- Phase A (WS-3, fix B3): activity_cache TIMESTAMPTZ + new columns ---
                # last_login + last_checked were plain TIMESTAMP (all other
                # timestamp columns in the schema are TIMESTAMPTZ). The values
                # were always UTC (civinfo_api converts epoch-ms with tz=UTC),
                # so the migration just attaches the timezone marker — no data
                # shift. Also adds last_logout / first_joined / is_online columns
                # (new in Phase A) so the mc-accounts/full endpoint data can be
                # fully persisted. All idempotent — re-running is a no-op.
                ac_login_col = await conn.fetchrow(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'activity_cache' AND column_name = 'last_login'"
                )
                if ac_login_col and ac_login_col["data_type"] == "timestamp without time zone":
                    await conn.execute(
                        "ALTER TABLE activity_cache "
                        "ALTER COLUMN last_login TYPE TIMESTAMPTZ "
                        "USING last_login AT TIME ZONE 'UTC'"
                    )
                    await conn.execute(
                        "ALTER TABLE activity_cache "
                        "ALTER COLUMN last_checked TYPE TIMESTAMPTZ "
                        "USING last_checked AT TIME ZONE 'UTC'"
                    )
                    logger.info("Migrated activity_cache.last_login + last_checked to TIMESTAMPTZ.")

                # Add the new columns if they don't exist (idempotent —
                # ADD COLUMN IF NOT EXISTS is safe on Postgres 9.6+).
                await conn.execute(
                    "ALTER TABLE activity_cache ADD COLUMN IF NOT EXISTS last_logout TIMESTAMPTZ"
                )
                await conn.execute(
                    "ALTER TABLE activity_cache ADD COLUMN IF NOT EXISTS first_joined TIMESTAMPTZ"
                )
                await conn.execute(
                    "ALTER TABLE activity_cache ADD COLUMN IF NOT EXISTS is_online "
                    "BOOLEAN DEFAULT FALSE"
                )
                # Back-fill is_online for any existing rows where last_login is
                # present and last_logout is NULL or older (the mc-accounts/full
                # endpoint exposes this, but existing rows predate the column).
                await conn.execute(
                    "UPDATE activity_cache SET is_online = TRUE "
                    "WHERE last_login IS NOT NULL "
                    "AND (last_logout IS NULL OR last_login > last_logout)"
                )

                # --- Phase 4.6: monthly_snapshots.notes ---
                # Adds a free-text ``notes`` column so leadership can annotate
                # historical snapshots with context ("snapshot taken during the
                # Great Diamond Crisis week", "post-exodus census", etc.). The
                # monthly report auto-saves rows with notes=NULL; the new
                # ``/snapshot annotate`` command (cogs/snapshot.py) sets the
                # value for a given date. Idempotent — re-running is a no-op.
                await conn.execute(
                    "ALTER TABLE monthly_snapshots ADD COLUMN IF NOT EXISTS notes TEXT"
                )

                # --- Proposal 1: activity_cache.stale ---
                # Idempotent add for existing installs (fresh installs get it via
                # the CREATE TABLE above). Marks rows whose last_login couldn't be
                # refreshed because CivInfo auth was broken — readers skip stale
                # rows so they don't act on aging activity data.
                await conn.execute(
                    "ALTER TABLE activity_cache ADD COLUMN IF NOT EXISTS stale BOOLEAN DEFAULT FALSE"
                )

                # join_date TEXT (DD/MM/YYYY) -> DATE
                col_jd = await conn.fetchrow(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'citizens' AND column_name = 'join_date'"
                )
                if col_jd and col_jd["data_type"] == "text":
                    # to_date returns NULL for unparseable input; the column is
                    # NOT NULL so any NULL would abort the migration loudly
                    # (which is the safe outcome -- better than silent corruption).
                    await conn.execute(
                        "ALTER TABLE citizens ALTER COLUMN join_date TYPE DATE "
                        "USING to_date(join_date, 'DD/MM/YYYY')"
                    )
                    logger.info("Migrated citizens.join_date from TEXT to DATE.")

                # settlements.name + citizens.settlement: TEXT -> CITEXT
                # (case-insensitive settlement names + case-insensitive FK).
                # Without this, "New September" and "new september" would be
                # two different settlements, and the role lookup (which matches
                # by name) would silently fail for one of them.
                col_settlement_name = await conn.fetchrow(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = 'settlements' AND column_name = 'name'"
                )
                if col_settlement_name and col_settlement_name["udt_name"] == "text":
                    # Drop the FK temporarily so both columns can be altered.
                    await conn.execute(
                        "ALTER TABLE citizens DROP CONSTRAINT IF EXISTS citizens_settlement_fkey"
                    )
                    await conn.execute(
                        "ALTER TABLE settlements ALTER COLUMN name TYPE CITEXT USING name::CITEXT"
                    )
                    await conn.execute(
                        "ALTER TABLE citizens ALTER COLUMN settlement TYPE CITEXT USING settlement::CITEXT"
                    )
                    # Recreate the FK with the same ON DELETE RESTRICT rule.
                    await conn.execute(
                        "ALTER TABLE citizens "
                        "ADD CONSTRAINT citizens_settlement_fkey "
                        "FOREIGN KEY (settlement) REFERENCES settlements(name) ON DELETE RESTRICT"
                    )
                    logger.info(
                        "Migrated settlements.name and citizens.settlement to CITEXT (case-insensitive)."
                    )

                # --- Phase 2.2 back-fill: recruiters junction from recruiter_ids ---
                # Only inserts rows that don't already exist (ON CONFLICT DO
                # NOTHING), so it's safe to re-run. recruiter_ids is a comma-
                # separated list of Discord user IDs. We split on comma and
                # insert one junction row per recruiter. Empty/garbage entries
                # are skipped. recruited_at is unknown for back-filled rows, so
                # we use the citizen's join_date as the best available proxy.
                # Use a single SELECT + Python loop rather than unnest() so the
                # logic is easy to audit and doesn't depend on a regex.
                from core.constants import SETTLEMENT_TO_DUCHY as _S2D  # noqa: PLC0415

                citizen_rows = await conn.fetch(
                    "SELECT ign, recruiter_ids, join_date FROM citizens "
                    "WHERE recruiter_ids IS NOT NULL AND recruiter_ids <> ''"
                )
                backfilled_recruiters = 0
                for crow in citizen_rows:
                    raw = crow["recruiter_ids"] or ""
                    for rid in (s.strip() for s in raw.split(",")):
                        if not rid or not rid.isdigit():
                            continue
                        result = await conn.execute(
                            "INSERT INTO recruiters (ign, recruiter_discord_id, recruited_at) "
                            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                            crow["ign"],
                            rid,
                            crow["join_date"],
                        )
                        if result and result.endswith("1"):
                            backfilled_recruiters += 1
                if backfilled_recruiters:
                    logger.info(
                        f"Back-filled {backfilled_recruiters} recruiter rows from citizens.recruiter_ids."
                    )

                # --- Phase 2.3 back-fill: settlements.duchy from seed dict ---
                # Populate duchy for any settlement still NULL. A settlement not
                # in the seed dict defaults to its own name (it's its own
                # duchy). After back-fill, enforce NOT NULL so future inserts
                # can't silently create a duchy-less settlement.
                settlements_rows = await conn.fetch(
                    "SELECT name FROM settlements WHERE duchy IS NULL"
                )
                backfilled_duchies = 0
                for srow in settlements_rows:
                    name = srow["name"]
                    duchy = _S2D.get(name, name)  # unmapped → self-duchy
                    await conn.execute(
                        "UPDATE settlements SET duchy = $1 WHERE name = $2",
                        duchy,
                        name,
                    )
                    backfilled_duchies += 1
                if backfilled_duchies:
                    logger.info(f"Back-filled duchy for {backfilled_duchies} settlements.")

                # Enforce NOT NULL only after every existing row has a value.
                # This is safe because the back-fill above filled every NULL.
                nullable = await conn.fetchval(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'settlements' AND column_name = 'duchy'"
                )
                if nullable == "YES":
                    # Double-check no NULLs remain before tightening.
                    null_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM settlements WHERE duchy IS NULL"
                    )
                    if null_count == 0:
                        await conn.execute(
                            "ALTER TABLE settlements ALTER COLUMN duchy SET NOT NULL"
                        )
                        logger.info("settlements.duchy set to NOT NULL.")

                # --- Phase 3.4: citizen_applications ---
                # Self-service applications submitted via /apply. Council
                # approves/rejects via button interactions. An approved
                # application triggers the normal citizen_add path. Status is
                # one of: pending / approved / rejected. The applicant's Discord
                # ID + status are unique so a user can't have two pending apps.
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS citizen_applications (
                        id BIGSERIAL PRIMARY KEY,
                        ign CITEXT NOT NULL,
                        applicant_discord_id TEXT NOT NULL,
                        settlement CITEXT NOT NULL,
                        recruiter_discord_id TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        decided_at TIMESTAMPTZ,
                        decided_by_discord_id TEXT,
                        decision_note TEXT
                    )
                """)
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_applications_status "
                    "ON citizen_applications(status)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_applications_ign ON citizen_applications(ign)"
                )
                # Prevent a user from having two 'pending' applications at once.
                # A partial unique index is the cleanest way to express this.
                await conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_pending_per_user "
                    "ON citizen_applications(applicant_discord_id) WHERE status = 'pending'"
                )

                # --- Phase 3.2: trigram index for fast /citizen search ---
                # pg_trgm enables ILIKE '%query%' to use a GIN index instead of a
                # full table scan. Essential once the registry grows past a few
                # hundred citizens. Safe to CREATE IF NOT EXISTS.
                await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_citizens_ign_trgm "
                    "ON citizens USING GIN (ign gin_trgm_ops)"
                )

                # --- Phase 2.4 back-fill: seed guild_emojis from constants ---
                # Inserts the hardcoded emoji mapping into guild_emojis so the
                # DB-backed lookup has data on first run. ON CONFLICT DO NOTHING
                # preserves any runtime overrides set via /emoji set.
                from core.constants import Emojis as _Emojis  # noqa: PLC0415

                emoji_seeds: list[tuple[str, str]] = []
                for duchy, emoji in _Emojis.PROVINCE.items():
                    if emoji:
                        emoji_seeds.append((f"province:{duchy}", emoji))
                for district, emoji in _Emojis.DISTRICT.items():
                    if emoji:
                        emoji_seeds.append((f"district:{district}", emoji))
                if emoji_seeds:
                    await conn.executemany(
                        "INSERT INTO guild_emojis (key, emoji_str) VALUES ($1, $2) "
                        "ON CONFLICT (key) DO NOTHING",
                        emoji_seeds,
                    )
                    logger.info(f"Seeded {len(emoji_seeds)} guild_emoji rows.")

        logger.info("Database tables and indexes verified/created successfully.")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}", exc_info=True)
        raise


async def execute_query(
    query: str,
    params: tuple = (),
    fetch_one: bool = False,
    fetch_all: bool = False,
    commit: bool = True,
    connection=None,
):
    """
    Execute a database query with optional transaction control.
    If a connection is provided, the query runs within that connection's transaction.
    """
    pool = await get_pool()
    if connection:
        # Use existing connection (caller is managing transaction)
        try:
            if fetch_one:
                return await connection.fetchrow(query, *params)
            elif fetch_all:
                return await connection.fetch(query, *params)
            else:
                result = await connection.execute(query, *params)
                try:
                    return int(result.split()[-1])
                except (ValueError, IndexError):
                    return 0
        except Exception as e:
            logger.error(f"Database query failed: {e}", exc_info=True)
            raise
    else:
        # Acquire own connection
        async with pool.acquire() as conn:
            try:
                if commit:
                    async with conn.transaction():
                        return await _execute_on_conn(conn, query, params, fetch_one, fetch_all)
                else:
                    return await _execute_on_conn(conn, query, params, fetch_one, fetch_all)
            except Exception as e:
                logger.error(f"Database query failed: {e}", exc_info=True)
                raise


async def _execute_on_conn(conn, query: str, params: tuple, fetch_one: bool, fetch_all: bool):
    if fetch_one:
        return await conn.fetchrow(query, *params)
    elif fetch_all:
        return await conn.fetch(query, *params)
    else:
        result = await conn.execute(query, *params)
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0
