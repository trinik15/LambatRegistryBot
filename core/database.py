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
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Create tables and indexes as before...
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS settlements (
                        name TEXT PRIMARY KEY
                    )
                """)
                await conn.execute("""
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
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS activity_cache (
                        ign TEXT PRIMARY KEY,
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


async def reset_db():
    """
    Reset the entire database: deletes all data from tables in correct order.
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
