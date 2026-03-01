import discord
from core.config import Config          # <-- MODIFICATO
from core import database as db         # <-- MODIFICATO
from api import civinfo_api             # <-- MODIFICATO (import corretto)
import logging
from typing import Optional

logger = logging.getLogger(__name__)

async def assign_citizen_roles(member: discord.Member, settlement: str):
    """Assign all citizen roles to a member."""
    guild = member.guild
    roles_to_add = []
    guest_role = guild.get_role(Config.GUEST_ROLE_ID)
    citizen_roles = [guild.get_role(rid) for rid in Config.CITIZEN_ROLE_IDS if guild.get_role(rid)]
    settler_role = guild.get_role(Config.SETTLER_ROLE_ID)
    settlement_role = discord.utils.get(guild.roles, name=settlement)

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

async def remove_all_citizen_roles(member: discord.Member, settlement: Optional[str] = None):
    """Remove all citizen roles (including settlement) and reassign guest."""
    guild = member.guild
    roles_to_remove = []
    citizen_roles = [guild.get_role(rid) for rid in Config.CITIZEN_ROLE_IDS if guild.get_role(rid)]
    settler_role = guild.get_role(Config.SETTLER_ROLE_ID)

    if citizen_roles:
        roles_to_remove.extend(citizen_roles)
    if settler_role:
        roles_to_remove.append(settler_role)
    if settlement:
        settlement_role = discord.utils.get(guild.roles, name=settlement)
        if settlement_role:
            roles_to_remove.append(settlement_role)

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove)

    guest_role = guild.get_role(Config.GUEST_ROLE_ID)
    if guest_role:
        await member.add_roles(guest_role)

async def update_settlement_role(member: discord.Member, old_settlement: str, new_settlement: str):
    """Update only the settlement role, leaving others untouched."""
    guild = member.guild
    old_role = discord.utils.get(guild.roles, name=old_settlement)
    new_role = discord.utils.get(guild.roles, name=new_settlement)

    if old_role and old_role in member.roles:
        await member.remove_roles(old_role)
    if new_role:
        await member.add_roles(new_role)

    guest_role = guild.get_role(Config.GUEST_ROLE_ID)
    if guest_role and guest_role in member.roles:
        await member.remove_roles(guest_role)

async def handle_user_change(guild: discord.Guild, old_discord_id: str, new_member: discord.Member,
                             old_settlement: str, new_settlement: Optional[str]):
    """Handle Discord user change: remove roles from old member, assign to new."""
    old_member = guild.get_member(int(old_discord_id))
    if old_member:
        await remove_all_citizen_roles(old_member, old_settlement)
        logger.info(f"Removed roles from old member {old_member} (ID {old_discord_id})")
    else:
        logger.warning(f"Old member with ID {old_discord_id} not found in server")

    target_settlement = new_settlement if new_settlement else old_settlement
    await assign_citizen_roles(new_member, target_settlement)
    logger.info(f"Assigned roles to new member {new_member}")

async def handle_settlement_change(guild: discord.Guild, member_id: str,
                                   old_settlement: str, new_settlement: str):
    """Handle settlement change for the same user."""
    member = guild.get_member(int(member_id))
    if member:
        await update_settlement_role(member, old_settlement, new_settlement)
        logger.info(f"Updated settlement role for {member} from {old_settlement} to {new_settlement}")
    else:
        logger.warning(f"Member with ID {member_id} not found in server, cannot update roles")
