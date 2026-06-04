import discord
from discord import app_commands
from discord.ext import commands
from core import database as db
from api import civinfo_api
import utils
from core.config import Config
from datetime import datetime, timezone
import logging
import time
import asyncio
from typing import Optional, List, Dict, Any
from utils import PaginationView
from services import role_manager

logger = logging.getLogger(__name__)


class AutocompleteCache:
    """TTL-based cache for autocomplete results."""
    def __init__(self, ttl_seconds: int = 60):
        self.ttl = ttl_seconds
        self._citizen_cache: Dict[str, Any] = {"timestamp": 0, "names": []}
        self._settlement_cache: Dict[str, Any] = {"timestamp": 0, "names": []}

    async def get_citizen_names(self) -> List[str]:
        now = datetime.now(timezone.utc).timestamp()
        if now - self._citizen_cache["timestamp"] > self.ttl:
            rows = await db.execute_query("SELECT ign FROM citizens", fetch_all=True)
            self._citizen_cache["names"] = [r["ign"] for r in rows]
            self._citizen_cache["timestamp"] = now
        return self._citizen_cache["names"]

    async def get_settlement_names(self) -> List[str]:
        now = datetime.now(timezone.utc).timestamp()
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
            return await interaction.response.send_message("You didn't initiate this removal.", ephemeral=True)

        await interaction.response.defer()
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM citizens WHERE ign = $1", self.ign)
                await conn.execute("DELETE FROM activity_cache WHERE ign = $1", self.ign)

        member = interaction.guild.get_member(int(self.discord_id))
        if member:
            await role_manager.remove_all_citizen_roles(member, self.settlement)

        self.cog.autocomplete_cache.invalidate_citizen_cache()
        await interaction.followup.send(f"✅ Citizen `{self.ign}` has been removed.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message("You didn't initiate this removal.", ephemeral=True)
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
        user_role_ids = [r.id for r in interaction.user.roles]
        return any(role_id in Config.FULL_ACCESS_ROLE_IDS for role_id in user_role_ids)

    def has_view_access(self, interaction: discord.Interaction) -> bool:
        if self.has_full_access(interaction):
            return True
        role_ids = [r.id for r in interaction.user.roles]
        return Config.VIEW_ACCESS_ROLE_ID in role_ids

    def _skin_url(self, ign: str) -> str:
        """Get Minecraft skin URL with cache-busting timestamp."""
        return f"https://minotar.net/armor/bust/{ign}/100.png?t={int(time.time())}"

    @citizen_group.command(name="add", description="Register a new citizen")
    @app_commands.autocomplete(settlement=settlement_autocomplete)
    @app_commands.checks.cooldown(1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "citizen_add"))
    async def citizen_add(self, interaction: discord.Interaction,
                         ign: str,
                         discord_user: discord.Member,
                         settlement: str,
                         recruiter1: discord.Member,
                         address: str,
                         mailbox: str = "Not provided",
                         recruiter2: discord.Member = None,
                         recruiter3: discord.Member = None,
                         notes: str = "None"):

        if not self.has_full_access(interaction):
            return await interaction.response.send_message("❌ You need the Council role to use this command.", ephemeral=True)

        if len(ign) > 16:
            await interaction.response.send_message("❌ IGN must be at most 16 characters long (Minecraft username limit).", ephemeral=True)
            return

        await interaction.response.defer()

        recruiters = [str(recruiter1.id)]
        if recruiter2:
            recruiters.append(str(recruiter2.id))
        if recruiter3:
            recruiters.append(str(recruiter3.id))
        recruiter_ids = ",".join(recruiters)
        join_date = datetime.now().strftime("%d/%m/%Y")

        pool = await db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing_ign = await conn.fetchrow("SELECT discord_id, ign FROM citizens WHERE ign = $1", ign)
                if existing_ign:
                    await interaction.followup.send(
                        f"❌ IGN `{ign}` is already registered to <@{existing_ign['discord_id']}>. "
                        f"Use `/citizen info {ign}` to view their dossier.", ephemeral=True)
                    return

                existing_discord = await conn.fetchrow("SELECT ign FROM citizens WHERE discord_id = $1", str(discord_user.id))
                if existing_discord:
                    await interaction.followup.send(
                        f"❌ {discord_user.mention} is already linked to IGN `{existing_discord['ign']}`. "
                        f"Please choose a different Discord user or update that record instead.", ephemeral=True)
                    return

                settlement_row = await conn.fetchrow("SELECT name FROM settlements WHERE name = $1", settlement)
                if not settlement_row:
                    await interaction.followup.send(
                        f"❌ Settlement '{settlement}' does not exist. Use `/settlement add {settlement}` to create it first.",
                        ephemeral=True)
                    return

                status, emoji, last_login, status_text = await civinfo_api.get_player_activity(ign, self.bot.http_session)
                if status == "error":
                    await interaction.followup.send("❌ CivInfo API is currently unavailable. Please try again later.", ephemeral=True)
                    return
                if status == "not_found":
                    await interaction.followup.send("❌ IGN not found on CivInfo. Please check the name and try again.", ephemeral=True)
                    return

                await conn.execute(
                    "INSERT INTO citizens (ign, discord_id, settlement, recruiter_ids, address, mailbox, notes, join_date) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    ign, str(discord_user.id), settlement, recruiter_ids, address, mailbox, notes, join_date
                )

                if last_login:
                    await conn.execute(
                        "INSERT INTO activity_cache (ign, last_login, status) VALUES ($1, $2, $3) "
                        "ON CONFLICT (ign) DO UPDATE SET last_login = EXCLUDED.last_login, status = EXCLUDED.status, last_checked = CURRENT_TIMESTAMP",
                        ign, last_login, status
                    )

        try:
            await role_manager.assign_citizen_roles(discord_user, settlement)
        except Exception as e:
            logger.error(f"Failed to assign roles to {discord_user} for citizen {ign}: {e}")

        self.autocomplete_cache.invalidate_citizen_cache()
        self.autocomplete_cache.invalidate_settlement_cache()

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
        embed.add_field(name="Join Date", value=join_date, inline=True)
        embed.set_thumbnail(url=self._skin_url(ign))
        await interaction.followup.send(embed=embed)

    @citizen_group.command(name="update", description="Update citizen info")
    @app_commands.autocomplete(ign=citizen_autocomplete, settlement=settlement_autocomplete)
    @app_commands.checks.cooldown(1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "citizen_update"))
    async def citizen_update(self, interaction: discord.Interaction,
                            ign: str,
                            discord_user: discord.Member = None,
                            settlement: str = None,
                            address: str = None,
                            mailbox: str = None,
                            notes: str = None,
                            join_date: str = None,
                            recruiter1: discord.Member = None,
                            recruiter2: discord.Member = None,
                            recruiter3: discord.Member = None):

        if not self.has_full_access(interaction):
            return await interaction.response.send_message("❌ You need the Council role to use this command.", ephemeral=True)

        await interaction.response.defer()

        old_row = await db.execute_query("SELECT * FROM citizens WHERE ign = $1", (ign,), fetch_one=True)
        if not old_row:
            await interaction.followup.send(f"❌ No citizen with IGN `{ign}`. Use `/citizen list` to see all citizens.", ephemeral=True)
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
            conflict = await db.execute_query("SELECT ign FROM citizens WHERE discord_id = $1 AND ign != $2",
                                             (str(discord_user.id), ign), fetch_one=True)
            if conflict:
                await interaction.followup.send(f"❌ {discord_user.mention} is already linked to IGN `{conflict['ign']}`. "
                                               f"Please choose a different Discord user.", ephemeral=True)
                return
            updates.append(f"discord_id = ${len(params)+1}")
            params.append(str(discord_user.id))
            changes["Discord"] = (f"<@{old_discord_id}>", discord_user.mention)

        if change_settlement:
            settlement_exists = await db.execute_query("SELECT name FROM settlements WHERE name = $1", (settlement,), fetch_one=True)
            if not settlement_exists:
                await interaction.followup.send(f"❌ Settlement '{settlement}' not found. Use `/settlement list` to see available settlements.", ephemeral=True)
                return
            updates.append(f"settlement = ${len(params)+1}")
            params.append(settlement)
            changes["Settlement"] = (old_settlement, settlement)

        if address is not None and address != old_address:
            updates.append(f"address = ${len(params)+1}")
            params.append(address)
            changes["Address"] = (old_address, address)

        if mailbox is not None and mailbox != old_mailbox:
            updates.append(f"mailbox = ${len(params)+1}")
            params.append(mailbox)
            changes["Mailbox"] = (old_mailbox, mailbox)

        if notes is not None and notes != old_notes:
            updates.append(f"notes = ${len(params)+1}")
            params.append(notes)
            changes["Notes"] = (old_notes, notes)

        if join_date is not None:
            if not utils.is_valid_date(join_date):
                await interaction.followup.send("❌ Invalid date format. Please use DD/MM/YYYY (e.g., 25/12/2024).", ephemeral=True)
                return
            if join_date != old_join_date:
                updates.append(f"join_date = ${len(params)+1}")
                params.append(join_date)
                changes["Join Date"] = (old_join_date, join_date)

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
                updates.append(f"recruiter_ids = ${len(params)+1}")
                params.append(new_recruiter_str)
                old_recruiters_mentions = ", ".join([f"<@{rid}>" for rid in old_recruiter_ids.split(",") if rid])
                new_recruiters_mentions = ", ".join([f"<@{rid}>" for rid in new_recruiters])
                changes["Recruiters"] = (old_recruiters_mentions, new_recruiters_mentions)

        if not updates:
            await interaction.followup.send("ℹ️ No changes detected. Please specify at least one field to update (e.g., address, settlement, join_date, etc.).", ephemeral=True)
            return

        set_clause = ", ".join(updates)
        query = f"UPDATE citizens SET {set_clause} WHERE ign = ${len(params)+1}"
        params.append(ign)

        pool = await db.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(query, *params)

        civinfo_api.cache.cache.pop(ign, None)
        self.autocomplete_cache.invalidate_citizen_cache()

        guild = interaction.guild
        if change_user:
            old_member = guild.get_member(int(old_discord_id))
            if old_member:
                await role_manager.remove_all_citizen_roles(old_member, old_settlement)
            await role_manager.assign_citizen_roles(discord_user, settlement)
        elif change_settlement:
            member = guild.get_member(int(old_discord_id))
            if member:
                await role_manager.update_settlement_role(member, old_settlement, settlement)

        embed = discord.Embed(title=f"✅ Updated {ign}", color=0x43B581)
        for field, (old, new) in changes.items():
            embed.add_field(name=field, value=f"~~{old}~~ → **{new}**", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"Citizen {ign} updated by {interaction.user} (ID: {interaction.user.id}): {changes}")

    @citizen_group.command(name="remove", description="Remove a citizen")
    @app_commands.autocomplete(ign=citizen_autocomplete)
    @app_commands.checks.cooldown(1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "citizen_remove"))
    async def citizen_remove(self, interaction: discord.Interaction, ign: str):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message("❌ You need the Council role to use this command.", ephemeral=True)

        row = await db.execute_query("SELECT discord_id, settlement FROM citizens WHERE ign = $1", (ign,), fetch_one=True)
        if not row:
            await interaction.response.send_message(f"❌ No citizen with IGN `{ign}`. Use `/citizen list` to see all registered citizens.", ephemeral=True)
            return

        discord_id = row["discord_id"]
        settlement = row["settlement"]

        embed = discord.Embed(
            title="Confirm Citizen Removal",
            description=f"Are you sure you want to remove citizen **{ign}**?",
            color=0xff9900
        )
        embed.add_field(name="Discord User", value=f"<@{discord_id}>", inline=True)
        embed.add_field(name="Settlement", value=settlement, inline=True)
        embed.set_footer(text="This action cannot be undone.")

        view = CitizenRemoveConfirm(self, ign, discord_id, settlement, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @citizen_group.command(name="list", description="List all citizens by settlement")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "citizen_list"))
    async def citizen_list(self, interaction: discord.Interaction):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message("❌ You don't have permission to view the citizen list.", ephemeral=True)

        await interaction.response.defer()
        rows = await db.execute_query(
            "SELECT ign, settlement FROM citizens ORDER BY settlement, ign",
            fetch_all=True
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
            embed = discord.Embed(title=f"🏘️ {settlement}", color=0x7289DA)
            citizen_list = "\n".join(citizens)
            if len(citizen_list) > 1024:
                citizen_list = citizen_list[:1021] + "..."
            embed.add_field(name=f"Citizens ({len(citizens)})", value=citizen_list, inline=False)
            embeds.append(embed)

        view = PaginationView(embeds, interaction.user)
        await interaction.followup.send(embed=embeds[0], view=view)

    @citizen_group.command(name="dossier", description="Show detailed citizen information")
    @app_commands.autocomplete(ign=citizen_autocomplete)
    @app_commands.checks.cooldown(1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "citizen_dossier"))
    async def citizen_dossier(self, interaction: discord.Interaction, ign: str):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message("❌ You don't have permission to view citizen dossiers.", ephemeral=True)

        await interaction.response.defer()

        row = await db.execute_query(
            "SELECT * FROM citizens WHERE ign = $1",
            (ign,), fetch_one=True
        )
        if not row:
            await interaction.followup.send(f"❌ No citizen with IGN `{ign}`.")
            return

        status, emoji, last_login, status_text = await civinfo_api.get_player_activity(ign, self.bot.http_session)

        recruiter_ids = row["recruiter_ids"].split(",") if row["recruiter_ids"] else []
        recruiter_mentions = ", ".join([f"<@{rid}>" for rid in recruiter_ids if rid]) or "None"

        embed = discord.Embed(title=f"📄 Dossier: {ign}", color=0x7289DA)
        embed.add_field(name="Discord", value=f"<@{row['discord_id']}>", inline=True)
        embed.add_field(name="Settlement", value=row["settlement"], inline=True)
        embed.add_field(name="Join Date", value=row["join_date"], inline=True)
        embed.add_field(name="Address", value=row["address"], inline=False)
        embed.add_field(name="Mailbox", value=row["mailbox"], inline=True)
        embed.add_field(name="Recruiters", value=recruiter_mentions, inline=True)
        embed.add_field(name="Activity", value=f"{emoji} {status_text}", inline=True)
        if last_login:
            embed.add_field(name="Last Login", value=last_login, inline=True)
        if row["notes"] != "None":
            embed.add_field(name="Notes", value=row["notes"], inline=False)
        embed.set_thumbnail(url=self._skin_url(ign))

        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(CitizenCog(bot))
