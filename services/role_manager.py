import asyncio
import logging
from contextlib import asynccontextmanager

import discord

from core.config import Config

logger = logging.getLogger(__name__)


@asynccontextmanager
async def rate_limit_guard(
    bot: discord.Client,
    *,
    max_wait: float = 30.0,
    poll: float = 0.5,
):
    """Wait for the Discord gateway rate-limit to clear before a role op.

    Phase 4.3: discord.py's ``is_ws_ratelimited()`` returns True when the
    gateway has hit a global rate limit (e.g. during a bulk role assignment
    that fires many ``add_roles`` / ``remove_roles`` HTTP calls in quick
    succession). Calling role APIs while the gateway is rate-limited causes
    discord.py to enqueue the calls — and if the queue grows past Discord's
    per-route burst, subsequent calls raise ``HTTPException(429)`` which the
    role callers bubble up as a failed citizen add / update / sync.

    This guard is the shared back-off valve: bulk callers (the weekly
    ``role_sync`` loop, CSV import, and the citizen add/update paths) wrap
    each role op in ``async with role_manager.rate_limit_guard(self.bot):``
    so the bot politely waits instead of tripping 429s.

    Args:
        bot: the bot/client (must expose ``is_ws_ratelimited()`` —
            ``commands.Bot`` / ``discord.Client`` both do).
        max_wait: the longest we'll wait before proceeding anyway (with a
            warning). 30s default — long enough to clear a typical global
            limit (Discord's are usually 5-10s), short enough that a bulk
            sync of 200 citizens doesn't take an hour.
        poll: how often to re-check. 0.5s balances responsiveness against
            event-loop churn.

    Yields:
        None — the context body runs once the gateway is (probably) clear.
    """
    waited = 0.0
    # is_ws_ratelimited is a sync method on discord.Client; guard against
    # test mocks that don't implement it.
    is_rate_limited = getattr(bot, "is_ws_ratelimited", None)
    while callable(is_rate_limited) and is_rate_limited():
        if waited >= max_wait:
            logger.warning(
                "Discord gateway still rate-limited after %.1fs; proceeding to "
                "avoid stalling the role op (may raise HTTP 429).",
                waited,
            )
            break
        await asyncio.sleep(poll)
        waited += poll
    if waited > 0:
        logger.info("Waited %.1fs for Discord gateway rate-limit to clear.", waited)
    yield


def _find_role_by_name(guild: discord.Guild, name: str) -> discord.Role | None:
    """Case-insensitive role lookup by name.

    Settlement names are CITEXT (case-insensitive) in the DB, so the Discord
    role lookup must also be case-insensitive — otherwise a settlement added
    as "New September" never gets its role assigned if the Discord role is
    "new september" (or vice versa). Returns None if no match.
    """
    if not name:
        return None
    target = name.lower()
    return discord.utils.find(lambda r: r.name.lower() == target, guild.roles)


async def assign_citizen_roles(member: discord.Member, settlement: str):
    """Assign all citizen roles to a member with error handling."""
    try:
        guild = member.guild
        roles_to_add = []

        guest_role = guild.get_role(Config.GUEST_ROLE_ID)
        citizen_roles: list[discord.Role] = [
            r for rid in Config.CITIZEN_ROLE_IDS if (r := guild.get_role(rid)) is not None
        ]
        settler_role = guild.get_role(Config.SETTLER_ROLE_ID)
        settlement_role = _find_role_by_name(guild, settlement)

        if guest_role:
            await member.remove_roles(guest_role)
        if citizen_roles:
            roles_to_add.extend(citizen_roles)
        if settler_role:
            roles_to_add.append(settler_role)
        if settlement_role:
            roles_to_add.append(settlement_role)

        if roles_to_add:
            await member.add_roles(*roles_to_add)
        logger.info(f"Assigned {len(roles_to_add)} roles to {member}")
    except discord.Forbidden:
        logger.error(
            f"Bot lacks permission to assign roles to {member.mention} in guild {guild.name}",
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(f"Failed to assign citizen roles to {member}: {e}", exc_info=True)
        raise


async def remove_all_citizen_roles(member: discord.Member, settlement: str | None = None):
    """Remove all citizen roles and reassign guest role with error handling."""
    try:
        guild = member.guild
        roles_to_remove = []

        citizen_roles: list[discord.Role] = [
            r for rid in Config.CITIZEN_ROLE_IDS if (r := guild.get_role(rid)) is not None
        ]
        settler_role = guild.get_role(Config.SETTLER_ROLE_ID)

        if citizen_roles:
            roles_to_remove.extend(citizen_roles)
        if settler_role:
            roles_to_remove.append(settler_role)
        if settlement:
            settlement_role = _find_role_by_name(guild, settlement)
            if settlement_role:
                roles_to_remove.append(settlement_role)

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)

        guest_role = guild.get_role(Config.GUEST_ROLE_ID)
        if guest_role:
            await member.add_roles(guest_role)

        logger.info(f"Removed {len(roles_to_remove)} roles from {member}")
    except discord.Forbidden:
        logger.error(
            f"Bot lacks permission to remove roles from {member.mention} in guild {guild.name}",
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(f"Failed to remove citizen roles from {member}: {e}", exc_info=True)
        raise


async def update_settlement_role(member: discord.Member, old_settlement: str, new_settlement: str):
    """Update settlement role for a member with error handling."""
    try:
        guild = member.guild
        old_role = _find_role_by_name(guild, old_settlement)
        new_role = _find_role_by_name(guild, new_settlement)

        if old_role and old_role in member.roles:
            await member.remove_roles(old_role)
        if new_role:
            await member.add_roles(new_role)

        guest_role = guild.get_role(Config.GUEST_ROLE_ID)
        if guest_role and guest_role in member.roles:
            await member.remove_roles(guest_role)

        logger.info(
            f"Updated settlement role for {member} from {old_settlement} to {new_settlement}"
        )
    except discord.Forbidden:
        logger.error(
            f"Bot lacks permission to update roles for {member.mention} in guild {guild.name}",
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(f"Failed to update settlement role for {member}: {e}", exc_info=True)
        raise


async def handle_user_change(
    guild: discord.Guild,
    old_discord_id: str,
    new_discord_member: discord.Member,
    old_settlement: str,
    new_settlement: str,
):
    """Handle Discord user change for a citizen, moving roles appropriately."""
    try:
        # Remove roles from old user
        old_member = guild.get_member(int(old_discord_id))
        if old_member:
            await remove_all_citizen_roles(old_member, old_settlement)

        # Assign roles to new user
        await assign_citizen_roles(new_discord_member, new_settlement)
    except Exception as e:
        logger.error(f"Failed to handle user change: {e}", exc_info=True)
        raise


async def handle_settlement_change(
    guild: discord.Guild, discord_id: str, old_settlement: str, new_settlement: str
):
    """Handle settlement change for a citizen, updating roles accordingly."""
    try:
        member = guild.get_member(int(discord_id))
        if member:
            await update_settlement_role(member, old_settlement, new_settlement)
    except Exception as e:
        logger.error(f"Failed to handle settlement change: {e}", exc_info=True)
        raise
