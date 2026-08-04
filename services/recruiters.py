"""Recruiters junction-table operations (Phase 2.2).

The ``recruiters`` table normalises the legacy ``citizens.recruiter_ids``
comma-separated TEXT column into first-class queryable rows. This module is
the single place that knows how to read/write that table, so the cogs can stay
focused on Discord UX.

Dual-write policy
-----------------
Writes go to BOTH the junction table (source of truth) and the legacy
``recruiter_ids`` column (denormalised cache). This keeps the existing reads
(dossier, CSV export) working unchanged while new reads (``recruited-by``,
leaderboard) use the junction table. A future phase can drop the legacy column
once all reads are migrated.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _clean_recruiter_ids(recruiter_ids: list[str]) -> list[str]:
    """Dedupe + validate a list of Discord user ID strings.

    Strips whitespace, drops empty/non-numeric entries, and preserves the
    first-seen order. Exposed as a module-level function so it's unit-testable
    without a DB — the cleaning logic is the most regression-prone part
    (a bad ID here would corrupt the junction table).
    """
    seen: set[str] = set()
    clean: list[str] = []
    for rid in recruiter_ids:
        rid = str(rid).strip()
        if not rid or not rid.isdigit() or rid in seen:
            continue
        seen.add(rid)
        clean.append(rid)
    return clean


async def set_recruiters(ign: str, recruiter_ids: list[str], connection=None) -> None:
    """Replace the full recruiter set for an IGN.

    Deletes all existing junction rows for ``ign`` and inserts one row per
    ``recruiter_id``. Runs within ``connection``'s transaction if provided
    (so it's atomic with the citizen INSERT/UPDATE). Also keeps the legacy
    ``citizens.recruiter_ids`` column in sync (comma-joined).

    Deduplicates and ignores empty/non-numeric entries (defensive against a
    Discord snowflake that came through as "" somehow).
    """
    clean = _clean_recruiter_ids(recruiter_ids)
    legacy_str = ",".join(clean)

    if connection is not None:
        await connection.execute("DELETE FROM recruiters WHERE ign = $1", ign)
        if clean:
            rows = [(ign, rid) for rid in clean]
            await connection.executemany(
                "INSERT INTO recruiters (ign, recruiter_discord_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                rows,
            )
        await connection.execute(
            "UPDATE citizens SET recruiter_ids = $1 WHERE ign = $2", legacy_str, ign
        )
        return

    from core import database as db

    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM recruiters WHERE ign = $1", ign)
        if clean:
            rows = [(ign, rid) for rid in clean]
            await conn.executemany(
                "INSERT INTO recruiters (ign, recruiter_discord_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                rows,
            )
        await conn.execute("UPDATE citizens SET recruiter_ids = $1 WHERE ign = $2", legacy_str, ign)


async def get_recruiters(ign: str) -> list[str]:
    """Return the list of recruiter Discord IDs for an IGN (from the junction)."""
    from core import database as db

    rows = await db.execute_query(
        "SELECT recruiter_discord_id FROM recruiters WHERE ign = $1 ORDER BY recruited_at",
        (ign,),
        fetch_all=True,
    )
    return [r["recruiter_discord_id"] for r in rows] if rows else []


async def get_recruited_by(discord_id: str) -> list[dict[str, Any]]:
    """Return the IGNs + recruited_at for everyone this Discord user recruited."""
    from core import database as db

    rows = await db.execute_query(
        "SELECT ign, recruited_at FROM recruiters WHERE recruiter_discord_id = $1 "
        "ORDER BY recruited_at DESC",
        (discord_id,),
        fetch_all=True,
    )
    return [dict(r) for r in rows] if rows else []


async def leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    """Return the top recruiters by count.

    Each row: {recruiter_discord_id, count}. ``limit`` is capped at 50.
    """
    limit = max(1, min(limit, 50))
    from core import database as db

    rows = await db.execute_query(
        "SELECT recruiter_discord_id, COUNT(*) AS cnt "
        "FROM recruiters GROUP BY recruiter_discord_id "
        "ORDER BY cnt DESC, recruiter_discord_id LIMIT $1",
        (limit,),
        fetch_all=True,
    )
    return [dict(r) for r in rows] if rows else []
