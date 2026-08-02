import discord
from discord import app_commands
from discord.ext import commands
from core import database as db
from core.config import Config
import logging

logger = logging.getLogger(__name__)


class SettlementCog(commands.Cog):
    settlement_group = app_commands.Group(name="settlement", description="Settlement management commands")

    def __init__(self, bot):
        self.bot = bot
        self._settlement_cache = None
        self._cache_timestamp = 0
        self._cache_ttl = 60

    def has_full_access(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == Config.OWNER_ID:
            return True
        # DM context: interaction.user has no .roles — deny rather than crash.
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        user_role_ids = [r.id for r in interaction.user.roles]
        return any(role_id in Config.FULL_ACCESS_ROLE_IDS for role_id in user_role_ids)

    async def get_settlement_names(self):
        """Get cached settlement names."""
        import time
        now = time.time()
        if self._settlement_cache is None or now - self._cache_timestamp > self._cache_ttl:
            rows = await db.execute_query("SELECT name FROM settlements ORDER BY name", fetch_all=True)
            self._settlement_cache = [row["name"] for row in rows]
            self._cache_timestamp = now
        return self._settlement_cache

    async def settlement_autocomplete(self, interaction: discord.Interaction, current: str):
        names = await self.get_settlement_names()
        filtered = [name for name in names if current.lower() in name.lower()]
        return [app_commands.Choice(name=name, value=name) for name in filtered[:25]]

    def invalidate_cache(self):
        """Invalidate the settlement name cache."""
        self._settlement_cache = None
        self._cache_timestamp = 0

    @settlement_group.command(name="add", description="Add a new settlement to the registry")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "settlement_add"))
    async def settlement_add(self, interaction: discord.Interaction, name: str):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message("❌ You need the Council role to use this command.", ephemeral=True)

        from core.constants import Limits
        if len(name) > Limits.SETTLEMENT_NAME_MAX or len(name) < 2:
            await interaction.response.send_message(
                f"❌ Settlement name must be 2–{Limits.SETTLEMENT_NAME_MAX} characters.",
                ephemeral=True)
            return

        await interaction.response.defer()

        existing = await db.execute_query("SELECT name FROM settlements WHERE name = $1", (name,), fetch_one=True)
        if existing:
            await interaction.followup.send(f"❌ Settlement `{name}` already exists.", ephemeral=True)
            return

        await db.execute_query("INSERT INTO settlements (name) VALUES ($1)", (name,))
        self.invalidate_cache()

        embed = discord.Embed(
            title="✅ Settlement Added",
            description=f"Settlement **{name}** has been added to the registry.",
            color=0x43B581
        )
        await interaction.followup.send(embed=embed)

    @settlement_group.command(name="remove", description="Remove a settlement from the registry")
    @app_commands.autocomplete(name=settlement_autocomplete)
    @app_commands.checks.cooldown(1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "settlement_remove"))
    async def settlement_remove(self, interaction: discord.Interaction, name: str):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message("❌ You need the Council role to use this command.", ephemeral=True)

        await interaction.response.defer()

        # Check if settlement exists
        existing = await db.execute_query("SELECT name FROM settlements WHERE name = $1", (name,), fetch_one=True)
        if not existing:
            await interaction.followup.send(f"❌ Settlement `{name}` does not exist.", ephemeral=True)
            return

        # Check if any citizens belong to this settlement
        citizens = await db.execute_query("SELECT COUNT(*) FROM citizens WHERE settlement = $1", (name,), fetch_one=True)
        citizen_count = citizens[0] if citizens else 0

        if citizen_count > 0:
            await interaction.followup.send(
                f"❌ Cannot remove settlement `{name}` because it has {citizen_count} registered citizen(s). "
                f"Please reassign or remove those citizens first.",
                ephemeral=True
            )
            return

        await db.execute_query("DELETE FROM settlements WHERE name = $1", (name,))
        self.invalidate_cache()

        embed = discord.Embed(
            title="✅ Settlement Removed",
            description=f"Settlement **{name}** has been removed from the registry.",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed)

    @settlement_group.command(name="list", description="List all settlements")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "settlement_list"))
    async def settlement_list(self, interaction: discord.Interaction):
        rows = await db.execute_query("SELECT name FROM settlements ORDER BY name", fetch_all=True)

        if not rows:
            await interaction.response.send_message("No settlements registered yet.", ephemeral=True)
            return

        settlements = [row["name"] for row in rows]
        embed = discord.Embed(
            title="🏘️ Registered Settlements",
            description="\n".join(f"• {name}" for name in settlements),
            color=0x7289DA
        )
        embed.set_footer(text=f"Total: {len(settlements)} settlements")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(SettlementCog(bot))
