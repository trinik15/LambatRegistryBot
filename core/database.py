import os
import logging
import asyncpg

from core.config import Config

logger = logging.getLogger(__name__)

DATABASE_URL = Config.DATABASE_URL

_pool: asyncpg.Pool = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _pool

async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

async def execute_query(query: str, params: tuple = (), fetch_one: bool = False, fetch_all: bool = False, commit: bool = True):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if fetch_one:
            return await conn.fetchrow(query, *params)
        elif fetch_all:
            return await conn.fetch(query, *params)
        else:
            result = await conn.execute(query, *params)
            try:
                return int(result.split()[-1])
            except:
                return 0

async def init_db():
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
    await execute_query("CREATE INDEX IF NOT EXISTS idx_citizens_settlement ON citizens(settlement)")
    await execute_query("CREATE INDEX IF NOT EXISTS idx_citizens_discord ON citizens(discord_id)")
    logger.info("Database PostgreSQL inizializzato.")

async def reset_db():
    """
    Cancella tutti i dati dalle tabelle in ordine corretto per rispettare le foreign key.
    L'operazione è eseguita in una transazione atomica.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM activity_cache;")
            await conn.execute("DELETE FROM citizens;")
            await conn.execute("DELETE FROM settlements;")
    logger.info("Database resettato: tutte le tabelle sono state svuotate.")
