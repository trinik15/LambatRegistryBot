import os
import logging
import asyncpg
import asyncio
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
                logger.info(f"Initializing database connection pool with min_size=2, max_size=10")
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
        async with pool.acquire() as conn:
            async with conn.transaction():
                # citext extension must exist before any CITEXT column can be
                # created (fresh installs) or altered into (upgrades).
                await conn.execute("CREATE EXTENSION IF NOT EXISTS citext")

                # --- Tables (fresh install uses CITEXT/DATE directly) ---
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS settlements (
                        name CITEXT PRIMARY KEY
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
                        last_login TIMESTAMP,
                        status TEXT,
                        last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
                        UNIQUE(snapshot_date, duchy, district)
                    )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_citizens_settlement ON citizens(settlement)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_citizens_discord ON citizens(discord_id)")

                # --- Idempotent migrations for existing TEXT-based installs ---

                # ign TEXT -> CITEXT (citizens + activity_cache, preserving FK)
                col_ign = await conn.fetchrow(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = 'citizens' AND column_name = 'ign'"
                )
                if col_ign and col_ign['udt_name'] == 'text':
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
                    logger.info("Migrated citizens.ign and activity_cache.ign to CITEXT (case-insensitive).")

                # join_date TEXT (DD/MM/YYYY) -> DATE
                col_jd = await conn.fetchrow(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'citizens' AND column_name = 'join_date'"
                )
                if col_jd and col_jd['data_type'] == 'text':
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
                if col_settlement_name and col_settlement_name['udt_name'] == 'text':
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
                    logger.info("Migrated settlements.name and citizens.settlement to CITEXT (case-insensitive).")

        logger.info("Database tables and indexes verified/created successfully.")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}", exc_info=True)
        raise


async def execute_query(query: str, params: tuple = (), fetch_one: bool = False,
                        fetch_all: bool = False, commit: bool = True, connection=None):
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
