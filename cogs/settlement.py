import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import database as db
from core import emojis as emoji_db
from core.config import Config
from core.constants import Limits
from services import audit

logger = logging.getLogger(__name__)


class SettlementCog(commands.Cog):
    settlement_group = app_commands.Group(
        name="settlement", description="Settlement management commands"
    )

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
            rows = await db.execute_query(
                "SELECT name FROM settlements ORDER BY name", fetch_all=True
            )
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
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "settlement_add")
    )
    @app_commands.describe(
        name="Settlement name (2-100 chars)",
        duchy="The duchy/province this settlement belongs to (e.g. 'Lambat City', 'Florraine')",
    )
    async def settlement_add(self, interaction: discord.Interaction, name: str, duchy: str):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.", ephemeral=True
            )

        if len(name) > Limits.SETTLEMENT_NAME_MAX or len(name) < 2:
            await interaction.response.send_message(
                f"❌ Settlement name must be 2–{Limits.SETTLEMENT_NAME_MAX} characters.",
                ephemeral=True,
            )
            return
        if not duchy.strip():
            await interaction.response.send_message("❌ Duchy must not be empty.", ephemeral=True)
            return

        await interaction.response.defer()

        existing = await db.execute_query(
            "SELECT name FROM settlements WHERE name = $1", (name,), fetch_one=True
        )
        if existing:
            await interaction.followup.send(
                f"❌ Settlement `{name}` already exists.", ephemeral=True
            )
            return

        pool = await db.get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO settlements (name, duchy) VALUES ($1, $2)", name, duchy.strip()
            )
            # Phase 2.1: audit the add atomically.
            await audit.emit(
                audit.SETTLEMENT_ADD,
                interaction.user.id,
                None,
                {"name": name, "duchy": duchy.strip()},
                connection=conn,
            )

        self.invalidate_cache()

        # Phase 2.1: mirror to the audit channel.
        await audit.post_to_channel(
            self.bot,
            audit.SETTLEMENT_ADD,
            str(interaction.user.id),
            None,
            {"name": name, "duchy": duchy.strip()},
        )

        embed = discord.Embed(
            title="✅ Settlement Added",
            description=f"Settlement **{name}** ({duchy.strip()}) has been added to the registry.",
            color=0x43B581,
        )
        await interaction.followup.send(embed=embed)

    @settlement_group.command(name="remove", description="Remove a settlement from the registry")
    @app_commands.autocomplete(name=settlement_autocomplete)
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "settlement_remove")
    )
    async def settlement_remove(self, interaction: discord.Interaction, name: str):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.", ephemeral=True
            )

        await interaction.response.defer()

        # Check if settlement exists
        existing = await db.execute_query(
            "SELECT name, duchy FROM settlements WHERE name = $1", (name,), fetch_one=True
        )
        if not existing:
            await interaction.followup.send(
                f"❌ Settlement `{name}` does not exist.", ephemeral=True
            )
            return

        # Check if any citizens belong to this settlement
        citizens = await db.execute_query(
            "SELECT COUNT(*) FROM citizens WHERE settlement = $1", (name,), fetch_one=True
        )
        citizen_count = citizens[0] if citizens else 0

        if citizen_count > 0:
            await interaction.followup.send(
                f"❌ Cannot remove settlement `{name}` because it has {citizen_count} registered citizen(s). "
                f"Please reassign or remove those citizens first.",
                ephemeral=True,
            )
            return

        duchy = existing["duchy"]
        pool = await db.get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM settlements WHERE name = $1", name)
            # Phase 2.1: audit the removal atomically.
            await audit.emit(
                audit.SETTLEMENT_REMOVE,
                interaction.user.id,
                None,
                {"name": name, "duchy": duchy},
                connection=conn,
            )

        self.invalidate_cache()

        # Phase 2.1: mirror to the audit channel.
        await audit.post_to_channel(
            self.bot,
            audit.SETTLEMENT_REMOVE,
            str(interaction.user.id),
            None,
            {"name": name, "duchy": duchy},
        )

        embed = discord.Embed(
            title="✅ Settlement Removed",
            description=f"Settlement **{name}** has been removed from the registry.",
            color=0xED4245,
        )
        await interaction.followup.send(embed=embed)

    @settlement_group.command(name="list", description="List all settlements, grouped by duchy")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "settlement_list")
    )
    async def settlement_list(self, interaction: discord.Interaction):
        # Phase 2.3: read duchy from the DB (no longer from the hardcoded dict).
        rows = await db.execute_query(
            "SELECT name, duchy FROM settlements ORDER BY duchy, name", fetch_all=True
        )

        if not rows:
            await interaction.response.send_message(
                "No settlements registered yet.", ephemeral=True
            )
            return

        # Group by duchy.
        by_duchy: dict[str, list[str]] = {}
        for row in rows:
            by_duchy.setdefault(row["duchy"], []).append(row["name"])

        embed = discord.Embed(
            title="🏘️ Registered Settlements",
            description=f"**{len(rows)}** settlement(s) across **{len(by_duchy)}** duchy/ies.",
            color=0x7289DA,
        )
        for duchy, settlements in sorted(by_duchy.items()):
            # Phase 2.4: look up the duchy emoji from the DB-backed mapping.
            emoji = await emoji_db.get_province(duchy)
            header = f"{emoji} {duchy}".strip() if emoji else duchy
            value = "\n".join(f"• {s}" for s in settlements)
            if len(value) > 1020:
                value = value[:1017] + "..."
            embed.add_field(name=header, value=value, inline=False)

        embed.set_footer(text=f"Total: {len(rows)} settlements")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(SettlementCog(bot))
