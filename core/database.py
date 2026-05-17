import os
import logging
import asyncpg

from core.config import Config

logger = logging.getLogger(__name__)

DATABASE_URL = Config.DATABASE_URL

_pool: asyncpg.Pool = None

async def get_pool() -> asyncpg.Pool:
    """Get or create the database connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _pool

async def close_pool():
    """Close the database connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

async def execute_query(query: str, params: tuple = (), fetch_one: bool = False, fetch_all: bool = False, commit: bool = True):
    """Execute a database query with error handling."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            if fetch_one:
                return await conn.fetchrow(query, *params)
            elif fetch_all:
                return await conn.fetch(query, *params)
            else:
                result = await conn.execute(query, *params)
                try:
                    return int(result.split()[-1])
                except (ValueError, IndexError) as e:
                    logger.warning(f"Could not parse execute result: {result}")
                    return 0
        except Exception as e:
            logger.error(f"Database query failed: {e}", exc_info=True)
            raise

async def init_db():
    """Initialize database tables."""
    try:
        await execute_query("""
            CREATE TABLE IF NOT EXISTS settlements (
                name TEXT PRIMARY KEY
            )
        """)
        await execute_query("""
            CREATE TABLE IF NOT EXISTS citizens (
                ign TEXT PRIMARY KEY,
                discord_id TEXT UNIQUE NOT NULL,
                settlement TEXT NOT NULL,
                recruiter_ids TEXT NOT NULL,
                address TEXT,
                mailbox TEXT,
                notes TEXT,
                join_date TEXT NOT NULL,
                FOREIGN KEY (settlement) REFERENCES settlements(name) ON DELETE RESTRICT
            )
        """)
        await execute_query("""
            CREATE TABLE IF NOT EXISTS activity_cache (
                ign TEXT PRIMARY KEY,
                last_login TIMESTAMP,
                status TEXT,
                last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ign) REFERENCES citizens(ign) ON DELETE CASCADE
            )
        """)
        await execute_query("""
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
        await execute_query("CREATE INDEX IF NOT EXISTS idx_citizens_settlement ON citizens(settlement)")
        await execute_query("CREATE INDEX IF NOT EXISTS idx_citizens_discord ON citizens(discord_id)")
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}", exc_info=True)
        raise

async def reset_db():
    """
    Reset the entire database: deletes all data from tables in correct order.
    Operation is executed in an atomic transaction.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute("DELETE FROM activity_cache;")
                await conn.execute("DELETE FROM citizens;")
                await conn.execute("DELETE FROM settlements;")
                await conn.execute("DELETE FROM monthly_snapshots;")
            logger.info("Database reset: all tables cleared.")
        except Exception as e:
            logger.error(f"Database reset failed: {e}", exc_info=True)
            raise
