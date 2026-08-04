import discord
from core.config import Config
from core import database as db
from api import civinfo_api
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _find_role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
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
        citizen_roles = [guild.get_role(rid) for rid in Config.CITIZEN_ROLE_IDS if guild.get_role(rid)]
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
        logger.error(f"Bot lacks permission to assign roles to {member.mention} in guild {guild.name}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Failed to assign citizen roles to {member}: {e}", exc_info=True)
        raise

async def remove_all_citizen_roles(member: discord.Member, settlement: Optional[str] = None):
    """Remove all citizen roles and reassign guest role with error handling."""
    try:
        guild = member.guild
        roles_to_remove = []

        citizen_roles = [guild.get_role(rid) for rid in Config.CITIZEN_ROLE_IDS if guild.get_role(rid)]
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
        logger.error(f"Bot lacks permission to remove roles from {member.mention} in guild {guild.name}", exc_info=True)
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

        logger.info(f"Updated settlement role for {member} from {old_settlement} to {new_settlement}")
    except discord.Forbidden:
        logger.error(f"Bot lacks permission to update roles for {member.mention} in guild {guild.name}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Failed to update settlement role for {member}: {e}", exc_info=True)
        raise

async def handle_user_change(guild: discord.Guild, old_discord_id: str, new_discord_member: discord.Member,
                             old_settlement: str, new_settlement: str):
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

async def handle_settlement_change(guild: discord.Guild, discord_id: str,
                                   old_settlement: str, new_settlement: str):
    """Handle settlement change for a citizen, updating roles accordingly."""
    try:
        member = guild.get_member(int(discord_id))
        if member:
            await update_settlement_role(member, old_settlement, new_settlement)
    except Exception as e:
        logger.error(f"Failed to handle settlement change: {e}", exc_info=True)
        raise
