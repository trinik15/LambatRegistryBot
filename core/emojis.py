"""DB-backed, runtime-configurable emoji lookup (Phase 2.4).

The hardcoded ``Emojis.PROVINCE`` / ``Emojis.DISTRICT`` dicts in
``core/constants.py`` were tied to one Discord guild's custom emoji IDs. This
module moves the mapping into the ``guild_emojis`` table so it can be changed
at runtime via ``/emoji set`` without a code change + redeploy.

Lookup path
-----------
1. An in-process dict cache (``_cache``) is populated lazily on first access
   from the ``guild_emojis`` table.
2. ``get(key)`` reads from the cache; ``get_province(duchy)`` and
   ``get_district(settlement)`` are thin wrappers that build the key.
3. ``set_emoji(key, emoji_str)`` upserts into the DB and refreshes the cache
   entry in place (so the new value is visible immediately, without a full
   reload).
4. ``invalidate()`` clears the cache so the next access reloads from the DB —
   used by tests and as a safety hatch.

The cache is per-process, which is correct for this single-process bot. A
multi-process deployment would need a shared cache (Redis) or a per-request
DB hit; that's out of scope for Phase 2.

Keys use a ``namespace:name`` convention (``province:Lambat City``,
``district:New September``) so province and district namespaces never collide.
"""

import logging
from typing import Any

from core.constants import Emojis

logger = logging.getLogger(__name__)

# Module-level cache. Initialised to None (not loaded); becomes a dict after
# the first _ensure_loaded() call. Set back to None by invalidate().
_cache: dict[str, str] | None = None

# Seed dicts (from constants) used only by seed_if_missing() to populate the
# DB the first time the bot starts on a fresh install where init_db() already
# seeded but we want a defensive double-check.
_SEED_PROVINCE = Emojis.PROVINCE
_SEED_DISTRICT = Emojis.DISTRICT


async def _ensure_loaded() -> dict[str, str]:
    """Lazily load the emoji cache from the DB on first access."""
    global _cache
    if _cache is not None:
        return _cache
    from core import database as db

    _cache = {}
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, emoji_str FROM guild_emojis")
        for row in rows:
            _cache[row["key"]] = row["emoji_str"]
        logger.debug(f"Loaded {len(_cache)} emoji entries from guild_emojis.")
    except Exception as e:  # noqa: BLE001 — a cache load failure must not crash the bot
        logger.error(f"Failed to load guild_emojis cache: {e}", exc_info=True)
        _cache = {}
    return _cache


def invalidate() -> None:
    """Clear the cache so the next lookup reloads from the DB.

    Called by ``/emoji set`` (after the DB write) and by tests.
    """
    global _cache
    _cache = None


async def get(key: str) -> str:
    """Return the emoji string for ``key`` (e.g. ``province:Lambat City``).

    Returns ``""`` if the key is not in the DB (no emoji configured). Never
    raises — a DB failure degrades to empty string so reports still render.
    """
    cache = await _ensure_loaded()
    return cache.get(key, "")


async def get_province(duchy: str) -> str:
    """Emoji for a duchy/province. Empty string if none configured."""
    if not duchy:
        return ""
    return await get(f"province:{duchy}")


async def get_district(settlement: str) -> str:
    """Emoji for a district/settlement. Empty string if none configured."""
    if not settlement:
        return ""
    return await get(f"district:{settlement}")


async def set_emoji(key: str, emoji_str: str) -> None:
    """Upsert an emoji mapping and refresh the in-process cache.

    Validates the key namespace (must start with ``province:`` or ``district:``)
    so a typo can't create a mapping that no lookup path will ever read.
    """
    if not key or ":" not in key:
        raise ValueError(
            "Emoji key must be in 'namespace:name' form (e.g. 'province:Lambat City')."
        )
    namespace = key.split(":", 1)[0]
    if namespace not in ("province", "district"):
        raise ValueError(f"Unknown emoji namespace {namespace!r}; use 'province:' or 'district:'.")
    if not emoji_str or not emoji_str.strip():
        raise ValueError("emoji_str must not be empty.")

    from core import database as db

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO guild_emojis (key, emoji_str) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET emoji_str = EXCLUDED.emoji_str",
            key,
            emoji_str,
        )

    # Refresh the cache in place so the new value is visible immediately
    # without a full reload.
    cache = await _ensure_loaded()
    cache[key] = emoji_str
    logger.info(f"Emoji set: {key} = {emoji_str}")


async def list_all() -> list[dict[str, Any]]:
    """Return all emoji mappings as a list of {key, emoji_str} dicts."""
    cache = await _ensure_loaded()
    return [{"key": k, "emoji_str": v} for k, v in sorted(cache.items())]


async def seed_if_missing() -> int:
    """Defensive: ensure the seed emoji rows exist in the DB.

    ``init_db()`` already seeds on first run, but this is a cheap safety net
    for environments where the migration was partially applied. Returns the
    number of rows inserted. Idempotent (ON CONFLICT DO NOTHING).
    """
    from core import database as db

    seeds: list[tuple[str, str]] = []
    for duchy, emoji in _SEED_PROVINCE.items():
        if emoji:
            seeds.append((f"province:{duchy}", emoji))
    for district, emoji in _SEED_DISTRICT.items():
        if emoji:
            seeds.append((f"district:{district}", emoji))
    if not seeds:
        return 0
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO guild_emojis (key, emoji_str) VALUES ($1, $2) "
            "ON CONFLICT (key) DO NOTHING",
            seeds,
        )
    invalidate()
    return len(seeds)
