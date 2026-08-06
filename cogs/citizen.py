import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import utils
from api import civinfo_api
from core import database as db
from core.config import Config
from core.constants import Limits
from services import audit, role_manager
from services import recruiters as recruiters_svc
from utils import PaginationView

logger = logging.getLogger(__name__)

# Minecraft username rules: 3-16 chars, alphanumeric + underscore.
_IGN_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


class AutocompleteCache:
    """TTL-based cache for autocomplete results."""

    def __init__(self, ttl_seconds: int = 60):
        self.ttl = ttl_seconds
        self._citizen_cache: dict[str, Any] = {"timestamp": 0, "names": []}
        self._settlement_cache: dict[str, Any] = {"timestamp": 0, "names": []}

    async def get_citizen_names(self) -> list[str]:
        now = datetime.now(UTC).timestamp()
        if now - self._citizen_cache["timestamp"] > self.ttl:
            rows = await db.execute_query("SELECT ign FROM citizens", fetch_all=True)
            self._citizen_cache["names"] = [r["ign"] for r in rows]
            self._citizen_cache["timestamp"] = now
        return self._citizen_cache["names"]

    async def get_settlement_names(self) -> list[str]:
        now = datetime.now(UTC).timestamp()
        if now - self._settlement_cache["timestamp"] > self.ttl:
            rows = await db.execute_query("SELECT name FROM settlements", fetch_all=True)
            self._settlement_cache["names"] = [r["name"] for r in rows]
            self._settlement_cache["timestamp"] = now
        return self._settlement_cache["names"]

    def invalidate_citizen_cache(self):
        """Invalidate the citizen cache."""
        self._citizen_cache["timestamp"] = 0

    def invalidate_settlement_cache(self):
        """Invalidate the settlement cache."""
        self._settlement_cache["timestamp"] = 0


class CitizenRemoveConfirm(discord.ui.View):
    def __init__(self, cog, ign, discord_id, settlement, requester_id):
        super().__init__(timeout=60)
        self.cog = cog
        self.ign = ign
        self.discord_id = discord_id
        self.settlement = settlement
        self.requester_id = requester_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message(
                "You didn't initiate this removal.", ephemeral=True
            )

        await interaction.response.defer()
        pool = await db.get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM citizens WHERE ign = $1", self.ign)
            await conn.execute("DELETE FROM activity_cache WHERE ign = $1", self.ign)
            # recruiters junction cascades on delete, no manual cleanup needed.
            # Phase 2.1: audit the removal atomically with the delete.
            await audit.emit(
                audit.CITIZEN_REMOVE,
                interaction.user.id,
                self.ign,
                {"discord_id": self.discord_id, "settlement": self.settlement},
                connection=conn,
            )

        # Phase 2.1: mirror to the audit channel (best-effort, post-commit).
        await audit.post_to_channel(
            interaction.client,
            audit.CITIZEN_REMOVE,
            str(interaction.user.id),
            self.ign,
            {"discord_id": self.discord_id, "settlement": self.settlement},
        )
        # Phase 3.7: also mirror to the governance channel (wider council).
        await audit.post_to_governance_channel(
            interaction.client,
            audit.CITIZEN_REMOVE,
            str(interaction.user.id),
            self.ign,
            {"discord_id": self.discord_id, "settlement": self.settlement},
        )

        # Role removal happens after the DB delete; surface failures honestly.
        role_warning = None
        member = interaction.guild.get_member(int(self.discord_id))
        if member:
            try:
                await role_manager.remove_all_citizen_roles(member, self.settlement)
            except Exception as e:
                role_warning = str(e) or e.__class__.__name__
                logger.error(
                    f"Failed to remove roles from {member} for deleted citizen {self.ign}: {e}",
                    exc_info=True,
                )

        self.cog.autocomplete_cache.invalidate_citizen_cache()
        if role_warning:
            await interaction.followup.send(
                f"✅ Citizen `{self.ign}` has been removed from the registry.\n"
                f"⚠️ Could not automatically strip Discord roles from <@{self.discord_id}>: `{role_warning}`. "
                f"Please remove them manually.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Citizen `{self.ign}` has been removed.", ephemeral=True
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message(
                "You didn't initiate this removal.", ephemeral=True
            )
        await interaction.response.send_message("Removal cancelled.", ephemeral=True)
        self.stop()


class CitizenCog(commands.Cog):
    citizen_group = app_commands.Group(name="citizen", description="Citizen management commands")

    def __init__(self, bot):
        self.bot = bot
        self.autocomplete_cache = AutocompleteCache()

    async def citizen_autocomplete(self, interaction: discord.Interaction, current: str):
        names = await self.autocomplete_cache.get_citizen_names()
        filtered = [name for name in names if current.lower() in name.lower()]
        return [app_commands.Choice(name=name, value=name) for name in filtered[:25]]

    async def settlement_autocomplete(self, interaction: discord.Interaction, current: str):
        names = await self.autocomplete_cache.get_settlement_names()
        filtered = [name for name in names if current.lower() in name.lower()]
        return [app_commands.Choice(name=name, value=name) for name in filtered[:25]]

    def has_full_access(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == Config.OWNER_ID:
            return True
        # DM context: interaction.user is a User, not a Member, so it has no
        # .roles — deny rather than crash with AttributeError.
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        user_role_ids = [r.id for r in interaction.user.roles]
        return any(role_id in Config.FULL_ACCESS_ROLE_IDS for role_id in user_role_ids)

    def has_view_access(self, interaction: discord.Interaction) -> bool:
        if self.has_full_access(interaction):
            return True
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        role_ids = [r.id for r in interaction.user.roles]
        return Config.VIEW_ACCESS_ROLE_ID in role_ids

    def _skin_url(self, ign: str) -> str:
        """Get Minecraft skin URL with cache-busting timestamp."""
        return f"https://minotar.net/armor/bust/{ign}/100.png?t={int(time.time())}"

    @citizen_group.command(name="add", description="Register a new citizen")
    @app_commands.autocomplete(settlement=settlement_autocomplete)
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "citizen_add")
    )
    async def citizen_add(
        self,
        interaction: discord.Interaction,
        ign: str,
        discord_user: discord.Member,
        settlement: str,
        recruiter1: discord.Member,
        address: str,
        mailbox: str = "Not provided",
        recruiter2: discord.Member = None,
        recruiter3: discord.Member = None,
        notes: str = "None",
    ):

        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.", ephemeral=True
            )

        # Validate IGN format (Minecraft username rules: 3-16 chars, [A-Za-z0-9_])
        if not _IGN_PATTERN.match(ign) or not (3 <= len(ign) <= Limits.IGN_MAX_LENGTH):
            await interaction.response.send_message(
                "❌ IGN must be 3–16 characters and may only contain letters, numbers, and underscores.",
                ephemeral=True,
            )
            return

        # Enforce field length limits up front so we never hand Discord an
        # embed field >1024 chars (which raises an opaque HTTP error) or store
        # unbounded text in the DB.
        if len(address) > Limits.ADDRESS_MAX:
            await interaction.response.send_message(
                f"❌ Address must be at most {Limits.ADDRESS_MAX} characters (got {len(address)}).",
                ephemeral=True,
            )
            return
        if len(mailbox) > Limits.MAILBOX_MAX:
            await interaction.response.send_message(
                f"❌ Mailbox must be at most {Limits.MAILBOX_MAX} characters (got {len(mailbox)}).",
                ephemeral=True,
            )
            return
        if len(notes) > Limits.NOTES_MAX:
            await interaction.response.send_message(
                f"❌ Notes must be at most {Limits.NOTES_MAX} characters (got {len(notes)}).",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        recruiters = [str(recruiter1.id)]
        if recruiter2:
            recruiters.append(str(recruiter2.id))
        if recruiter3:
            recruiters.append(str(recruiter3.id))
        recruiter_ids = ",".join(recruiters)
        join_date_obj = datetime.now(UTC).date()
        join_date_display = join_date_obj.strftime("%d/%m/%Y")

        pool = await db.get_pool()
        async with pool.acquire() as conn:  # noqa: SIM117 — nested transaction reads cleaner
            async with conn.transaction():
                existing_ign = await conn.fetchrow(
                    "SELECT discord_id, ign FROM citizens WHERE ign = $1", ign
                )
                if existing_ign:
                    await interaction.followup.send(
                        f"❌ IGN `{ign}` is already registered to <@{existing_ign['discord_id']}>. "
                        f"Use `/citizen info {ign}` to view their dossier.",
                        ephemeral=True,
                    )
                    return

                existing_discord = await conn.fetchrow(
                    "SELECT ign FROM citizens WHERE discord_id = $1", str(discord_user.id)
                )
                if existing_discord:
                    await interaction.followup.send(
                        f"❌ {discord_user.mention} is already linked to IGN `{existing_discord['ign']}`. "
                        f"Please choose a different Discord user or update that record instead.",
                        ephemeral=True,
                    )
                    return

                settlement_row = await conn.fetchrow(
                    "SELECT name FROM settlements WHERE name = $1", settlement
                )
                if not settlement_row:
                    await interaction.followup.send(
                        f"❌ Settlement '{settlement}' does not exist. Use `/settlement add {settlement}` to create it first.",
                        ephemeral=True,
                    )
                    return

                # CivInfo lookup validates that the IGN actually exists on CivMC
                # and seeds the activity cache. We only HARD-block on
                # "not_found" (the IGN doesn't exist — likely a typo). If the
                # API itself is down or auth-broken ("error"), we still
                # register the citizen (the registry is the source of truth,
                # not CivInfo) and surface a warning so council knows the
                # activity wasn't verified.
                civinfo_warning = None
                pa = await civinfo_api.get_player_activity(ign, self.bot.http_session)
                if pa.status == "error":
                    civinfo_warning = pa.status_text or "CivInfo unavailable"
                elif pa.status == "not_found":
                    await interaction.followup.send(
                        "❌ IGN not found on CivInfo. Please check the name and try again.",
                        ephemeral=True,
                    )
                    return

                await conn.execute(
                    "INSERT INTO citizens (ign, discord_id, settlement, recruiter_ids, address, mailbox, notes, join_date) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    ign,
                    str(discord_user.id),
                    settlement,
                    recruiter_ids,
                    address,
                    mailbox,
                    notes,
                    join_date_obj,
                )

                # Phase 2.2: dual-write recruiters into the junction table
                # (source of truth). recruiter_ids stays as a denormalised cache.
                await recruiters_svc.set_recruiters(ign, recruiters, connection=conn)

                if pa.last_login:
                    await conn.execute(
                        "INSERT INTO activity_cache "
                        "(ign, last_login, last_logout, first_joined, status, is_online) "
                        "VALUES ($1, $2, $3, $4, $5, $6) "
                        "ON CONFLICT (ign) DO UPDATE SET "
                        "last_login = EXCLUDED.last_login, "
                        "last_logout = EXCLUDED.last_logout, "
                        "first_joined = EXCLUDED.first_joined, "
                        "status = EXCLUDED.status, "
                        "is_online = EXCLUDED.is_online, "
                        "last_checked = CURRENT_TIMESTAMP, "
                        "stale = FALSE",
                        ign,
                        pa.last_login,
                        pa.last_logout,
                        pa.first_joined,
                        pa.status,
                        pa.is_online,
                    )

                # Phase 2.1: audit the add inside the transaction so it's
                # atomic — either the citizen + audit row commit together,
                # or neither does.
                await audit.emit(
                    audit.CITIZEN_ADD,
                    interaction.user.id,
                    ign,
                    {
                        "discord_id": str(discord_user.id),
                        "settlement": settlement,
                        "recruiters": recruiters,
                        "join_date": join_date_obj.isoformat(),
                    },
                    connection=conn,
                )

        # Role assignment happens after the DB commit (the registry is the
        # source of truth). If Discord role assignment fails we must NOT claim
        # full success — surface the partial failure honestly so council can fix it.
        role_error = None
        try:
            await role_manager.assign_citizen_roles(discord_user, settlement)
        except Exception as e:
            role_error = str(e) or e.__class__.__name__
            logger.error(
                f"Failed to assign roles to {discord_user} for citizen {ign}: {e}", exc_info=True
            )

        self.autocomplete_cache.invalidate_citizen_cache()
        self.autocomplete_cache.invalidate_settlement_cache()

        # Phase 2.1: mirror the audit event to the (optional) audit channel.
        # Best-effort: a Discord failure here must not corrupt the success path.
        await audit.post_to_channel(
            self.bot,
            audit.CITIZEN_ADD,
            str(interaction.user.id),
            ign,
            {
                "discord_id": str(discord_user.id),
                "settlement": settlement,
                "recruiters": recruiters,
            },
        )
        # Phase 3.7: also mirror to the governance channel (wider council).
        await audit.post_to_governance_channel(
            self.bot,
            audit.CITIZEN_ADD,
            str(interaction.user.id),
            ign,
            {
                "discord_id": str(discord_user.id),
                "settlement": settlement,
                "recruiters": recruiters,
            },
        )

        if role_error or civinfo_warning:
            embed = discord.Embed(
                title="⚠️ Citizen Registered (with warnings)",
                description=(
                    f"`{ign}` was saved to the registry."
                    + (" Discord roles could not be assigned automatically." if role_error else "")
                    + (" CivInfo activity could not be verified." if civinfo_warning else "")
                ),
                color=0xFF9900,
            )
        else:
            embed = discord.Embed(title="✅ Citizen Registered", color=0x43B581)
        embed.add_field(name="IGN", value=ign, inline=True)
        embed.add_field(name="Discord", value=discord_user.mention, inline=True)
        embed.add_field(name="Settlement", value=settlement, inline=True)
        embed.add_field(name="Recruiter 1", value=recruiter1.mention, inline=True)
        if recruiter2:
            embed.add_field(name="Recruiter 2", value=recruiter2.mention, inline=True)
        if recruiter3:
            embed.add_field(name="Recruiter 3", value=recruiter3.mention, inline=True)
        embed.add_field(name="Address", value=address, inline=False)
        embed.add_field(name="Mailbox", value=mailbox, inline=True)
        embed.add_field(name="Notes", value=notes, inline=False)
        embed.add_field(name="Join Date", value=join_date_display, inline=True)
        if role_error:
            embed.add_field(
                name="⚠️ Action Required",
                value=(
                    f"Discord role assignment failed:\n`{role_error}`\n\n"
                    f"Please assign the citizen/settlement roles to {discord_user.mention} manually, "
                    f"or check that the bot has the **Manage Roles** permission and its role is above "
                    f"the roles it needs to assign."
                ),
                inline=False,
            )
        if civinfo_warning:
            embed.add_field(
                name="ℹ️ Activity Unverified",
                value=(
                    f"CivInfo could not verify this player's activity (`{civinfo_warning}`). "
                    f"The citizen is registered, but their activity status will show as Unknown "
                    f"until CivInfo is available again."
                ),
                inline=False,
            )
        embed.set_thumbnail(url=self._skin_url(ign))
        await interaction.followup.send(embed=embed)

    @citizen_group.command(name="update", description="Update citizen info")
    @app_commands.autocomplete(ign=citizen_autocomplete, settlement=settlement_autocomplete)
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "citizen_update")
    )
    async def citizen_update(
        self,
        interaction: discord.Interaction,
        ign: str,
        discord_user: discord.Member = None,
        settlement: str = None,
        address: str = None,
        mailbox: str = None,
        notes: str = None,
        join_date: str = None,
        recruiter1: discord.Member = None,
        recruiter2: discord.Member = None,
        recruiter3: discord.Member = None,
    ):

        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.", ephemeral=True
            )

        await interaction.response.defer()

        old_row = await db.execute_query(
            "SELECT * FROM citizens WHERE ign = $1", (ign,), fetch_one=True
        )
        if not old_row:
            await interaction.followup.send(
                f"❌ No citizen with IGN `{ign}`. Use `/citizen list` to see all citizens.",
                ephemeral=True,
            )
            return

        # Validate field lengths for any provided values (post-defer, so use followup).
        if address is not None and len(address) > Limits.ADDRESS_MAX:
            await interaction.followup.send(
                f"❌ Address must be at most {Limits.ADDRESS_MAX} characters (got {len(address)}).",
                ephemeral=True,
            )
            return
        if mailbox is not None and len(mailbox) > Limits.MAILBOX_MAX:
            await interaction.followup.send(
                f"❌ Mailbox must be at most {Limits.MAILBOX_MAX} characters (got {len(mailbox)}).",
                ephemeral=True,
            )
            return
        if notes is not None and len(notes) > Limits.NOTES_MAX:
            await interaction.followup.send(
                f"❌ Notes must be at most {Limits.NOTES_MAX} characters (got {len(notes)}).",
                ephemeral=True,
            )
            return

        changes = {}
        old_discord_id = old_row["discord_id"]
        old_settlement = old_row["settlement"]
        old_join_date = old_row["join_date"]
        old_address = old_row["address"]
        old_mailbox = old_row["mailbox"]
        old_notes = old_row["notes"]
        old_recruiter_ids = old_row["recruiter_ids"]

        updates = []
        params = []

        change_user = discord_user and str(discord_user.id) != old_discord_id
        change_settlement = settlement and settlement != old_settlement

        if change_user:
            conflict = await db.execute_query(
                "SELECT ign FROM citizens WHERE discord_id = $1 AND ign != $2",
                (str(discord_user.id), ign),
                fetch_one=True,
            )
            if conflict:
                await interaction.followup.send(
                    f"❌ {discord_user.mention} is already linked to IGN `{conflict['ign']}`. "
                    f"Please choose a different Discord user.",
                    ephemeral=True,
                )
                return
            updates.append(f"discord_id = ${len(params) + 1}")
            params.append(str(discord_user.id))
            changes["Discord"] = (f"<@{old_discord_id}>", discord_user.mention)

        if change_settlement:
            settlement_exists = await db.execute_query(
                "SELECT name FROM settlements WHERE name = $1", (settlement,), fetch_one=True
            )
            if not settlement_exists:
                await interaction.followup.send(
                    f"❌ Settlement '{settlement}' not found. Use `/settlement list` to see available settlements.",
                    ephemeral=True,
                )
                return
            updates.append(f"settlement = ${len(params) + 1}")
            params.append(settlement)
            changes["Settlement"] = (old_settlement, settlement)

        if address is not None and address != old_address:
            updates.append(f"address = ${len(params) + 1}")
            params.append(address)
            changes["Address"] = (old_address, address)

        if mailbox is not None and mailbox != old_mailbox:
            updates.append(f"mailbox = ${len(params) + 1}")
            params.append(mailbox)
            changes["Mailbox"] = (old_mailbox, mailbox)

        if notes is not None and notes != old_notes:
            updates.append(f"notes = ${len(params) + 1}")
            params.append(notes)
            changes["Notes"] = (old_notes, notes)

        if join_date is not None:
            if not utils.is_valid_date(join_date):
                await interaction.followup.send(
                    "❌ Invalid join date. Please use DD/MM/YYYY (e.g., 25/12/2024). "
                    "The date must be in the past and no earlier than 01/01/2022 (CivMC launch).",
                    ephemeral=True,
                )
                return
            new_join_date_obj = utils.parse_join_date(join_date)
            if new_join_date_obj is None:
                await interaction.followup.send(
                    "❌ Invalid join date. Please use DD/MM/YYYY (e.g., 25/12/2024). "
                    "The date must be real, in the past, and on/after 01/01/2022.",
                    ephemeral=True,
                )
                return
            # old_join_date is a DATE object after migration; normalise for compare
            old_jd_obj = (
                utils.parse_join_date(old_join_date)
                if not hasattr(old_join_date, "year")
                else old_join_date
            )
            if old_jd_obj is None or new_join_date_obj != old_jd_obj:
                updates.append(f"join_date = ${len(params) + 1}")
                params.append(new_join_date_obj)
                changes["Join Date"] = (utils.format_date(old_join_date), join_date)

        # Track the new recruiter list separately so we can sync the junction
        # table after the UPDATE. None means recruiters weren't touched.
        new_recruiters_list: list[str] | None = None
        if any([recruiter1, recruiter2, recruiter3]):
            new_recruiters = []
            if recruiter1:
                new_recruiters.append(str(recruiter1.id))
            if recruiter2:
                new_recruiters.append(str(recruiter2.id))
            if recruiter3:
                new_recruiters.append(str(recruiter3.id))
            new_recruiter_str = ",".join(new_recruiters)
            if new_recruiter_str != old_recruiter_ids:
                updates.append(f"recruiter_ids = ${len(params) + 1}")
                params.append(new_recruiter_str)
                old_recruiters_mentions = ", ".join(
                    [f"<@{rid}>" for rid in old_recruiter_ids.split(",") if rid]
                )
                new_recruiters_mentions = ", ".join([f"<@{rid}>" for rid in new_recruiters])
                changes["Recruiters"] = (old_recruiters_mentions, new_recruiters_mentions)
                new_recruiters_list = new_recruiters

        if not updates:
            await interaction.followup.send(
                "ℹ️ No changes detected. Please specify at least one field to update (e.g., address, settlement, join_date, etc.).",
                ephemeral=True,
            )
            return

        set_clause = ", ".join(updates)
        query = f"UPDATE citizens SET {set_clause} WHERE ign = ${len(params) + 1}"
        params.append(ign)

        pool = await db.get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(query, *params)
            # Phase 2.2: sync the recruiters junction table if recruiters changed.
            if new_recruiters_list is not None:
                await recruiters_svc.set_recruiters(ign, new_recruiters_list, connection=conn)
            # Phase 2.1: audit the update inside the transaction (atomic).
            # Serialise the changes dict (tuples → lists) for JSONB.
            audit_changes = {k: list(v) for k, v in changes.items()}
            await audit.emit(
                audit.CITIZEN_UPDATE,
                interaction.user.id,
                ign,
                {"changes": audit_changes},
                connection=conn,
            )

        # Phase A (WS-3, fix B4): use the public invalidate() method instead
        # of reaching into the cache's internal dict.
        civinfo_api.cache.invalidate(ign)
        self.autocomplete_cache.invalidate_citizen_cache()

        # Phase 2.1: mirror to the audit channel (best-effort, post-commit).
        await audit.post_to_channel(
            self.bot,
            audit.CITIZEN_UPDATE,
            str(interaction.user.id),
            ign,
            {"changes": {k: list(v) for k, v in changes.items()}},
        )
        # Phase 3.7: also mirror to the governance channel (wider council).
        await audit.post_to_governance_channel(
            self.bot,
            audit.CITIZEN_UPDATE,
            str(interaction.user.id),
            ign,
            {"changes": {k: list(v) for k, v in changes.items()}},
        )

        # Role changes happen after the DB commit. Collect any failures so we
        # can surface them honestly instead of silently leaving stale roles.
        role_warnings = []
        guild = interaction.guild
        if change_user:
            try:
                old_member = guild.get_member(int(old_discord_id))
                if old_member:
                    await role_manager.remove_all_citizen_roles(old_member, old_settlement)
                # If only the Discord user changed (not the settlement), the
                # new user must still receive their EXISTING settlement role —
                # `settlement` is None in that case, so fall back to old_settlement.
                effective_settlement = settlement if change_settlement else old_settlement
                await role_manager.assign_citizen_roles(discord_user, effective_settlement)
            except Exception as e:
                role_warnings.append(f"Discord user/role swap failed: `{e}`")
                logger.error(f"Failed to swap roles for {ign} (user change): {e}", exc_info=True)
        elif change_settlement:
            try:
                member = guild.get_member(int(old_discord_id))
                if member:
                    await role_manager.update_settlement_role(member, old_settlement, settlement)
            except Exception as e:
                role_warnings.append(f"Settlement role update failed: `{e}`")
                logger.error(f"Failed to update settlement role for {ign}: {e}", exc_info=True)

        embed = discord.Embed(title=f"✅ Updated {ign}", color=0x43B581)
        for field, (old, new) in changes.items():
            embed.add_field(name=field, value=f"~~{old}~~ → **{new}**", inline=False)
        if role_warnings:
            embed.color = 0xFF9900
            embed.add_field(
                name="⚠️ Role Warnings",
                value=(
                    "The registry was updated, but one or more Discord role changes failed:\n"
                    + "\n".join(role_warnings)
                    + "\nPlease adjust roles manually if needed."
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(
            f"Citizen {ign} updated by {interaction.user} (ID: {interaction.user.id}): {changes}"
        )

    @citizen_group.command(name="remove", description="Remove a citizen")
    @app_commands.autocomplete(ign=citizen_autocomplete)
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "citizen_remove")
    )
    async def citizen_remove(self, interaction: discord.Interaction, ign: str):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.", ephemeral=True
            )

        row = await db.execute_query(
            "SELECT discord_id, settlement FROM citizens WHERE ign = $1", (ign,), fetch_one=True
        )
        if not row:
            await interaction.response.send_message(
                f"❌ No citizen with IGN `{ign}`. Use `/citizen list` to see all registered citizens.",
                ephemeral=True,
            )
            return

        discord_id = row["discord_id"]
        settlement = row["settlement"]

        embed = discord.Embed(
            title="Confirm Citizen Removal",
            description=f"Are you sure you want to remove citizen **{ign}**?",
            color=0xFF9900,
        )
        embed.add_field(name="Discord User", value=f"<@{discord_id}>", inline=True)
        embed.add_field(name="Settlement", value=settlement, inline=True)
        embed.set_footer(text="This action cannot be undone.")

        view = CitizenRemoveConfirm(self, ign, discord_id, settlement, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @citizen_group.command(name="list", description="List all citizens by settlement")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "citizen_list")
    )
    async def citizen_list(self, interaction: discord.Interaction):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message(
                "❌ You don't have permission to view the citizen list.", ephemeral=True
            )

        await interaction.response.defer()
        rows = await db.execute_query(
            "SELECT ign, settlement FROM citizens ORDER BY settlement, ign", fetch_all=True
        )

        if not rows:
            await interaction.followup.send("No citizens registered yet.")
            return

        settlements = {}
        for row in rows:
            settlement = row["settlement"]
            if settlement not in settlements:
                settlements[settlement] = []
            settlements[settlement].append(row["ign"])

        embeds = []
        for settlement, citizens in settlements.items():
            # Chunk citizens so each embed field stays under Discord's 1024-char
            # limit WITHOUT slicing mid-name (old code did citizen_list[:1021]
            # which cut usernames in half and silently dropped people).
            chunks = []
            current = []
            current_len = 0
            for name in citizens:
                entry_len = len(name) + 1  # +1 for the joining newline
                if current and current_len + entry_len > 1000:
                    chunks.append(current)
                    current = [name]
                    current_len = entry_len
                else:
                    current.append(name)
                    current_len += entry_len
            if current:
                chunks.append(current)
            if not chunks:
                chunks = [[]]

            num_chunks = len(chunks)
            for idx, chunk in enumerate(chunks, start=1):
                part_label = f" ({idx}/{num_chunks})" if num_chunks > 1 else ""
                embed = discord.Embed(title=f"🏘️ {settlement}{part_label}", color=0x7289DA)
                value = "\n".join(chunk) if chunk else "None"
                embed.add_field(
                    name=f"Citizens ({len(citizens)} total, showing {len(chunk)})",
                    value=value,
                    inline=False,
                )
                embeds.append(embed)

        view = PaginationView(embeds, interaction.user)
        await interaction.followup.send(embed=embeds[0], view=view)
        view.message = await interaction.original_response()

    @citizen_group.command(name="dossier", description="Show detailed citizen information")
    @app_commands.autocomplete(ign=citizen_autocomplete)
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "citizen_dossier")
    )
    async def citizen_dossier(self, interaction: discord.Interaction, ign: str):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message(
                "❌ You don't have permission to view citizen dossiers.", ephemeral=True
            )

        await interaction.response.defer()

        row = await db.execute_query(
            "SELECT * FROM citizens WHERE ign = $1", (ign,), fetch_one=True
        )
        if not row:
            await interaction.followup.send(f"❌ No citizen with IGN `{ign}`.")
            return

        pa = await civinfo_api.get_player_activity(ign, self.bot.http_session)

        recruiter_ids = row["recruiter_ids"].split(",") if row["recruiter_ids"] else []
        recruiter_mentions = ", ".join([f"<@{rid}>" for rid in recruiter_ids if rid]) or "None"

        embed = discord.Embed(title=f"📄 Dossier: {ign}", color=0x7289DA)
        embed.add_field(name="Discord", value=f"<@{row['discord_id']}>", inline=True)
        embed.add_field(name="Settlement", value=row["settlement"], inline=True)
        embed.add_field(name="Join Date", value=utils.format_date(row["join_date"]), inline=True)
        embed.add_field(name="Address", value=row["address"], inline=False)
        embed.add_field(name="Mailbox", value=row["mailbox"], inline=True)
        embed.add_field(name="Recruiters", value=recruiter_mentions, inline=True)
        embed.add_field(name="Activity", value=f"{pa.emoji} {pa.status_text}", inline=True)
        if pa.last_login:
            embed.add_field(
                name="Last Login",
                value=utils.format_date(pa.last_login, "%Y-%m-%d %H:%M"),
                inline=True,
            )
        if row["notes"] != "None":
            embed.add_field(name="Notes", value=row["notes"], inline=False)
        embed.set_thumbnail(url=self._skin_url(ign))

        await interaction.followup.send(embed=embed)

    @citizen_group.command(
        name="recruited-by", description="Show all citizens recruited by a Discord user"
    )
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "citizen_recruited_by")
    )
    async def citizen_recruited_by(
        self, interaction: discord.Interaction, recruiter: discord.Member
    ):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message(
                "❌ You don't have permission to view recruiter data.", ephemeral=True
            )

        await interaction.response.defer()

        # Read from the recruiters junction table (Phase 2.2 source of truth).
        recruited = await recruiters_svc.get_recruited_by(str(recruiter.id))
        if not recruited:
            await interaction.followup.send(
                f"ℹ️ {recruiter.mention} has not recruited any registered citizens.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🎯 Citizens Recruited by {recruiter.display_name}",
            description=f"**{len(recruited)}** citizen(s) recruited.",
            color=0x7289DA,
        )
        lines = []
        for r in recruited:
            ts = r["recruited_at"]
            date_str = utils.format_date(ts, "%Y-%m-%d") if ts else "—"
            lines.append(f"• **{r['ign']}** — {date_str}")
        # Chunk into fields of ~30 lines to stay under the 1024-char limit.
        value = "\n".join(lines)
        if len(value) > 1020:
            value = value[:1017] + "..."
        embed.add_field(name="Recruits", value=value, inline=False)
        embed.set_footer(text="Sourced from the recruiters junction table")
        await interaction.followup.send(embed=embed)

    @citizen_group.command(
        name="search", description="Search citizens by IGN, settlement, or Discord ID"
    )
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "citizen_search")
    )
    @app_commands.describe(
        query="Search term (IGN, settlement name, or Discord user ID)",
    )
    async def citizen_search(self, interaction: discord.Interaction, query: str):
        """Phase 3.2: full-text-ish search across the registry.

        Searches IGN (ILIKE, trigram-indexed), settlement name, and Discord ID.
        Results are paginated via PaginationView.
        """
        if not self.has_view_access(interaction):
            return await interaction.response.send_message(
                "❌ You don't have permission to search citizens.", ephemeral=True
            )

        q = query.strip()
        if not q or len(q) < 2:
            await interaction.response.send_message(
                "❌ Search query must be at least 2 characters.", ephemeral=True
            )
            return

        await interaction.response.defer()

        rows = await _search_citizens(q)
        if not rows:
            await interaction.followup.send(f"No citizens found matching `{q}`.", ephemeral=True)
            return

        embeds = _build_search_results_embeds(q, rows)
        view = PaginationView(embeds, interaction.user, timeout=180)
        await interaction.followup.send(embed=embeds[0], view=view)
        view.message = await interaction.original_response()

    @citizen_group.command(name="import", description="Bulk import citizens from a CSV file")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_CRITICAL, key=lambda i: (i.user.id, "citizen_import")
    )
    @app_commands.describe(
        file="CSV file with columns: IGN, Discord ID, Settlement, Join Date, Address, Mailbox, Recruiter IDs, Notes"
    )
    async def citizen_import(self, interaction: discord.Interaction, file: discord.Attachment):
        """Phase 3.1: bulk CSV import with dry-run preview + confirm button."""
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.", ephemeral=True
            )

        if not file.filename.lower().endswith(".csv"):
            await interaction.response.send_message(
                "❌ The file must be a .csv file.", ephemeral=True
            )
            return

        if file.size > 1_000_000:  # 1MB cap
            await interaction.response.send_message("❌ File too large (max 1MB).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            data = await file.read()
        except Exception as e:
            logger.error(f"Failed to read CSV attachment: {e}", exc_info=True)
            await interaction.followup.send("❌ Could not read the file.", ephemeral=True)
            return

        # Fetch known settlements + existing IGNs for validation.
        settlement_rows = await db.execute_query("SELECT name FROM settlements", fetch_all=True)
        known_settlements = [r["name"] for r in settlement_rows] if settlement_rows else []

        existing_rows = await db.execute_query("SELECT ign FROM citizens", fetch_all=True)
        existing_igns = [r["ign"] for r in existing_rows] if existing_rows else []

        # Parse + validate (pure function, no DB calls).
        from services import csv_import

        result = csv_import.parse_csv(data, known_settlements, existing_igns)

        if result.total == 0:
            await interaction.followup.send(
                "❌ The CSV file is empty or has no valid rows.", ephemeral=True
            )
            return

        embed = _build_import_preview_embed(result)
        view = ConfirmImportView(result, interaction.user)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


class ConfirmImportView(discord.ui.View):
    """Confirm/Cancel buttons for the CSV import dry-run (Phase 3.1)."""

    def __init__(self, result, user: discord.User | discord.Member):
        super().__init__(timeout=300)
        self.result = result
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "Only the person who started the import can confirm it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm Import", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        imported = 0
        skipped = 0
        errors: list[str] = []

        for row in self.result.rows:
            if not row.is_valid:
                skipped += 1
                continue
            try:
                await _import_single_citizen(row)
                imported += 1
            except Exception as e:
                logger.error(f"Failed to import row {row.line} ({row.ign}): {e}", exc_info=True)
                errors.append(f"Line {row.line} ({row.ign}): {e}")
                skipped += 1

        embed = discord.Embed(
            title="✅ Import Complete",
            description=(
                f"**{imported}** citizen(s) imported successfully.\n**{skipped}** row(s) skipped."
            ),
            color=0x43B581 if not errors else 0xFFCC00,
        )
        if errors:
            error_text = "\n".join(errors[:10])
            if len(errors) > 10:
                error_text += f"\n*...and {len(errors) - 10} more errors*"
            embed.add_field(name="Errors", value=error_text[:1024], inline=False)

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="❌ Import Cancelled",
            description="No changes were made to the registry.",
            color=0xED4245,
        )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_original_response(embed=embed, view=self)


async def _import_single_citizen(row) -> None:
    """Import a single validated CSV row into the DB (Phase 3.1)."""
    from services import recruiters as recruiters_svc
    from utils import parse_join_date

    join_date = parse_join_date(row.join_date) if row.join_date else None
    if join_date is None:
        from datetime import date

        join_date = date.today()

    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO citizens (ign, discord_id, settlement, recruiter_ids, "
            "address, mailbox, notes, join_date) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            row.ign,
            row.discord_id,
            row.settlement,
            row.recruiter_ids or "",
            row.address or None,
            row.mailbox or None,
            row.notes or None,
            join_date,
        )
        if row.recruiter_ids:
            recruiter_list = [
                r.strip() for r in row.recruiter_ids.split(",") if r.strip().isdigit()
            ]
            await recruiters_svc.set_recruiters(row.ign, recruiter_list, connection=conn)


def _build_import_preview_embed(result) -> discord.Embed:
    """Build the dry-run preview embed showing valid/invalid rows."""
    color = 0x43B581 if result.invalid_count == 0 else 0xFFCC00
    embed = discord.Embed(
        title="📋 CSV Import Preview (Dry Run)",
        description=(
            f"**{result.total}** rows parsed — "
            f"✅ **{result.valid_count}** valid, "
            f"❌ **{result.invalid_count}** invalid."
        ),
        color=color,
    )

    invalid_rows = [r for r in result.rows if not r.is_valid]
    if invalid_rows:
        lines = []
        for row in invalid_rows[:10]:
            errors = "; ".join(row.errors)
            lines.append(f"• Line {row.line}: {errors}")
        if len(invalid_rows) > 10:
            lines.append(f"*...and {len(invalid_rows) - 10} more invalid rows*")
        embed.add_field(name="❌ Invalid Rows", value="\n".join(lines)[:1024], inline=False)

    valid_rows = [r for r in result.rows if r.is_valid]
    if valid_rows:
        lines = []
        for row in valid_rows[:10]:
            lines.append(f"• **{row.ign}** — {row.settlement}")
        if len(valid_rows) > 10:
            lines.append(f"*...and {len(valid_rows) - 10} more valid rows*")
        embed.add_field(name="✅ Valid Rows (preview)", value="\n".join(lines)[:1024], inline=False)

    if result.duplicate_igns_in_csv:
        dup_text = ", ".join(result.duplicate_igns_in_csv[:10])
        if len(result.duplicate_igns_in_csv) > 10:
            dup_text += f" *...and {len(result.duplicate_igns_in_csv) - 10} more*"
        embed.add_field(
            name="⚠️ Existing IGNs (will be skipped)", value=dup_text[:1024], inline=False
        )

    embed.set_footer(text="Click ✅ Confirm Import to proceed, or ✖️ Cancel to abort.")
    return embed


# ---------------------------------------------------------------------------
# Pure helpers (testable without Discord / DB)
# ---------------------------------------------------------------------------


async def _search_citizens(query: str) -> list[dict]:
    """Search citizens by IGN, settlement, or Discord ID.

    Uses ILIKE for partial case-insensitive matching (the trigram index
    added in Phase 3.2 makes this fast). When the query looks like a Discord
    ID (all digits), also matches discord_id and recruiter_discord_id.

    .. note::

        The Discord-ID path queries **our own** ``citizens.discord_id``
        column (populated at ``/citizen add`` time), NOT CivInfo. CivInfo's
        ``mc-accounts/full`` endpoint does not expose Discord UIDs — the
        Discord↔MC link lives in Gjum's Kira bridge (``users`` table), which
        is not exposed via any civinfo API endpoint (confirmed 2025-08 via
        ``civmc.netlify.app`` bundle analysis + ``Gjum/Kira`` source; see
        ``ROADMAP.md`` §8.1 and ``README.md`` "Why can't we look up
        citizens by Discord UID?"). Don't try to resolve an unknown Discord
        ID via CivInfo — it can't be done. This path only finds citizens
        already registered with us.
    """
    pattern = f"%{query}%"
    is_numeric = query.isdigit()

    if is_numeric:
        # Discord-ID path: queries citizens.discord_id + recruiters junction.
        # CivInfo cannot resolve a Discord UID → IGN (see docstring above).
        rows = await db.execute_query(
            "SELECT DISTINCT c.ign, c.discord_id, c.settlement, c.join_date "
            "FROM citizens c "
            "LEFT JOIN recruiters r ON r.ign = c.ign "
            "WHERE c.ign ILIKE $1 OR c.settlement ILIKE $1 "
            "OR c.discord_id = $2 OR r.recruiter_discord_id = $2 "
            "ORDER BY c.ign LIMIT 100",
            (pattern, query),
            fetch_all=True,
        )
    else:
        rows = await db.execute_query(
            "SELECT ign, discord_id, settlement, join_date FROM citizens "
            "WHERE ign ILIKE $1 OR settlement ILIKE $1 "
            "ORDER BY ign LIMIT 100",
            (pattern,),
            fetch_all=True,
        )
    return [dict(r) for r in rows] if rows else []


def _build_search_results_embeds(query: str, rows: list[dict]) -> list[discord.Embed]:
    """Build paginated embeds for search results (max 10 per page)."""
    embeds: list[discord.Embed] = []
    per_page = 10
    total = len(rows)

    for i in range(0, total, per_page):
        chunk = rows[i : i + per_page]
        page_num = i // per_page + 1
        total_pages = (total + per_page - 1) // per_page

        embed = discord.Embed(
            title=f"🔍 Search: '{query}'",
            description=f"**{total}** result(s) found.",
            color=0x7289DA,
        )
        lines = []
        for row in chunk:
            join_str = utils.format_date(row["join_date"]) if row["join_date"] else "—"
            lines.append(
                f"• **{row['ign']}** — {row['settlement']} "
                f"(<@{row['discord_id']}>, joined {join_str})"
            )
        embed.add_field(
            name=f"Results (page {page_num}/{total_pages})",
            value="\n".join(lines)[:1024],
            inline=False,
        )
        embed.set_footer(text=f"Page {page_num}/{total_pages} • {total} citizens")
        embeds.append(embed)

    return embeds


async def setup(bot):
    await bot.add_cog(CitizenCog(bot))
