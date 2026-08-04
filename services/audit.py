"""Audit log emitter + search (Phase 2.1).

Every registry mutation (citizen add/update/remove, settlement add/remove,
role-sync discrepancy) is recorded in the ``audit_log`` table. The record is
the authoritative history; an optional Discord channel mirror gives the wider
council a read-only view.

Design notes
------------
* **Transaction-aware.** ``emit`` accepts an optional ``connection`` so the
  audit row is written *inside* the caller's transaction — the mutation and its
  audit record commit atomically (or roll back together). When no connection is
  passed, ``emit`` acquires its own. This mirrors ``database.execute_query``'s
  connection-passthrough pattern and is what makes the audit log trustworthy:
  you can never have a mutation without a matching audit row, or vice versa.
* **JSONB details.** Field-level diffs go into a JSONB column so the full
  history is reconstructable without schema changes. asyncpg doesn't natively
  encode Python dicts to JSONB, so we ``json.dumps`` and cast with ``::jsonb``.
* **Fire-and-forget channel mirror.``** ``post_to_channel`` is best-effort:
  a Discord outage or missing permission must never roll back the mutation
  (the DB record is the source of truth). It is awaited by callers *after* the
  transaction commits.
"""

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

import discord

from core.config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action vocabulary.
# Centralised so typos are caught at the call site (and /audit search can
# offer a stable list of filters). Keep this in sync with the values used by
# the emitters in cogs/ and tasks/.
# ---------------------------------------------------------------------------
CITIZEN_ADD = "citizen.add"
CITIZEN_UPDATE = "citizen.update"
CITIZEN_REMOVE = "citizen.remove"
SETTLEMENT_ADD = "settlement.add"
SETTLEMENT_REMOVE = "settlement.remove"
ROLE_SYNC_DISCREPANCY = "role_sync.discrepancy"
ROLE_SYNC_FIXED = "role_sync.fixed"
EMOJI_SET = "emoji.set"

ALL_ACTIONS = (
    CITIZEN_ADD,
    CITIZEN_UPDATE,
    CITIZEN_REMOVE,
    SETTLEMENT_ADD,
    SETTLEMENT_REMOVE,
    ROLE_SYNC_DISCREPANCY,
    ROLE_SYNC_FIXED,
    EMOJI_SET,
)


async def emit(
    action: str,
    actor_discord_id: str | int | None,
    target_ign: str | None,
    details: dict[str, Any] | None = None,
    connection=None,
) -> int | None:
    """Record an audit entry. Returns the inserted row id (or None on failure).

    If ``connection`` is provided, the insert runs within that connection's
    transaction (atomic with the mutation). Otherwise a fresh connection is
    acquired and the insert runs in its own transaction.

    Failures are logged but never raised — an audit-log write failure must NOT
    abort a mutation the user already made (the DB row is the source of truth;
    the audit log is observability). If atomicity is required, the caller must
    pass a connection so the audit insert shares the caller's transaction.
    """
    actor = str(actor_discord_id) if actor_discord_id is not None else None
    details_json = json.dumps(details, default=_json_default) if details else None

    try:
        if connection is not None:
            row_id = await connection.fetchval(
                "INSERT INTO audit_log (actor_discord_id, action, target_ign, details) "
                "VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
                actor,
                action,
                target_ign,
                details_json,
            )
            return row_id
        from core import database as db

        pool = await db.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO audit_log (actor_discord_id, action, target_ign, details) "
                "VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
                actor,
                action,
                target_ign,
                details_json,
            )
    except Exception as e:  # noqa: BLE001 — audit must never break a mutation
        logger.error(
            f"Failed to emit audit entry (action={action}, target={target_ign}): {e}",
            exc_info=True,
        )
        return None


async def search(
    *,
    actor_discord_id: str | None = None,
    action: str | None = None,
    target_ign: str | None = None,
    since: date | datetime | None = None,
    until: date | datetime | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search the audit log. Returns rows newest-first.

    All filters are optional and AND-combined. ``limit`` is capped at 200 to
    protect against a pathological query dragging the bot down.
    """
    limit = max(1, min(limit, 200))
    conditions: list[str] = []
    params: list[Any] = []
    if actor_discord_id:
        params.append(str(actor_discord_id))
        conditions.append(f"actor_discord_id = ${len(params)}")
    if action:
        params.append(action)
        conditions.append(f"action = ${len(params)}")
    if target_ign:
        params.append(target_ign)
        conditions.append(f"target_ign = ${len(params)}")
    if since:
        params.append(_to_dt(since))
        conditions.append(f"ts >= ${len(params)}")
    if until:
        params.append(_to_dt(until))
        conditions.append(f"ts < ${len(params)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    query = (
        f"SELECT id, ts, actor_discord_id, action, target_ign, details "
        f"FROM audit_log {where} ORDER BY ts DESC, id DESC LIMIT ${len(params)}"
    )

    from core import database as db

    rows = await db.execute_query(query, tuple(params), fetch_all=True)
    return [dict(r) for r in rows] if rows else []


async def post_to_channel(
    bot,
    action: str,
    actor_discord_id: str | None,
    target_ign: str | None,
    details: dict[str, Any] | None,
) -> None:
    """Post a concise audit embed to AUDIT_CHANNEL_ID (best-effort).

    Called by emitters AFTER the DB transaction commits. Never raises — a
    Discord failure here must not corrupt the caller's flow.
    """
    if not Config.AUDIT_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(Config.AUDIT_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(Config.AUDIT_CHANNEL_ID)
    except discord.NotFound:
        logger.warning(f"AUDIT_CHANNEL_ID {Config.AUDIT_CHANNEL_ID} not found.")
        return
    except discord.Forbidden:
        logger.warning("Bot lacks permission to view AUDIT_CHANNEL_ID.")
        return
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not fetch audit channel: {e}")
        return

    if channel is None:
        return

    embed = _build_embed(action, actor_discord_id, target_ign, details)
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        logger.warning("Bot lacks permission to send in the audit channel.")
    except discord.HTTPException as e:
        logger.warning(f"Failed to post audit message to Discord: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """JSON serialiser fallback for date/datetime/set values in details."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def _to_dt(value: date | datetime) -> datetime:
    """Coerce a date/datetime to a timezone-aware datetime for TIMESTAMPTZ compare."""
    if isinstance(value, datetime):
        return value
    # date → datetime at midnight UTC
    from datetime import UTC
    from datetime import datetime as _dt

    return _dt(value.year, value.month, value.day, tzinfo=UTC)


def _build_embed(
    action: str,
    actor_discord_id: str | None,
    target_ign: str | None,
    details: dict[str, Any] | None,
) -> discord.Embed:
    """Build the concise embed posted to the audit channel.

    Intentionally short — the full JSONB diff lives in the DB for /audit search.
    The channel mirror is a glanceable notification, not a data dump.
    """
    color = _action_color(action)
    embed = discord.Embed(title=f"📜 Audit: {_action_label(action)}", color=color)
    embed.timestamp = datetime.now(UTC)

    actor_str = f"<@{actor_discord_id}>" if actor_discord_id else "—"
    target_str = target_ign or "—"
    embed.add_field(name="Actor", value=actor_str, inline=True)
    embed.add_field(name="Target", value=f"`{target_str}`", inline=True)

    if details:
        # Render a compact summary of the details. For citizen.update this is
        # the field list; for citizen.add it's the settlement/recruiters, etc.
        summary = _summarise_details(details)
        if summary:
            # Truncate to 1024 (Discord field-value cap) with an ellipsis.
            if len(summary) > 1020:
                summary = summary[:1017] + "..."
            embed.add_field(name="Details", value=summary, inline=False)

    return embed


def _action_label(action: str) -> str:
    return {
        CITIZEN_ADD: "Citizen added",
        CITIZEN_UPDATE: "Citizen updated",
        CITIZEN_REMOVE: "Citizen removed",
        SETTLEMENT_ADD: "Settlement added",
        SETTLEMENT_REMOVE: "Settlement removed",
        ROLE_SYNC_DISCREPANCY: "Role discrepancy",
        ROLE_SYNC_FIXED: "Role auto-fixed",
        EMOJI_SET: "Emoji updated",
    }.get(action, action)


def _action_color(action: str) -> int:
    """Green for creates, orange for updates/removes, blue for ops, grey for sync."""
    if action in (CITIZEN_ADD, SETTLEMENT_ADD, EMOJI_SET):
        return 0x43B581  # green
    if action in (CITIZEN_UPDATE,):
        return 0x5865F2  # blurple
    if action in (CITIZEN_REMOVE, SETTLEMENT_REMOVE, ROLE_SYNC_DISCREPANCY):
        return 0xFF9900  # orange
    if action == ROLE_SYNC_FIXED:
        return 0x57F287  # light green
    return 0x7289DA  # default grey-blue


def _summarise_details(details: dict[str, Any]) -> str:
    """Render a compact, human-readable summary of a details dict.

    Special-cases the common shapes emitted by the cogs:
      * {"changes": {field: [old, new], ...}}  → field: old → new
      * {"settlement": str, "recruiters": [...], "discord_id": str}
      * {"name": str, "duchy": str}
      * {"role": str, "issue": str, "member": str}
    Falls back to a generic key=value listing for anything else.
    """
    if "changes" in details and isinstance(details["changes"], dict):
        lines: list[str] = []
        for field, val in details["changes"].items():
            if isinstance(val, (list, tuple)) and len(val) == 2:
                lines.append(f"• **{field}**: {val[0]} → {val[1]}")
            else:
                lines.append(f"• **{field}**: {val}")
        return "\n".join(lines)
    if "settlement" in details:
        parts = [f"Settlement: {details['settlement']}"]
        if "discord_id" in details:
            parts.append(f"Discord: <@{details['discord_id']}>")
        if "recruiters" in details and details["recruiters"]:
            rids = details["recruiters"]
            if isinstance(rids, list) and rids:
                parts.append("Recruiters: " + ", ".join(f"<@{r}>" for r in rids))
        return "\n".join(parts)
    if "name" in details:
        parts = [f"Name: {details['name']}"]
        if "duchy" in details:
            parts.append(f"Duchy: {details['duchy']}")
        return "\n".join(parts)
    if "role" in details or "issue" in details:
        parts = []
        if "member" in details:
            parts.append(f"Member: <@{details['member']}>")
        if "role" in details:
            parts.append(f"Role: {details['role']}")
        if "issue" in details:
            parts.append(f"Issue: {details['issue']}")
        return "\n".join(parts)
    # Generic fallback.
    return "\n".join(f"• **{k}**: {v}" for k, v in details.items())
