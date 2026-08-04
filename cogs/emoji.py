"""Emoji management cog (Phase 2.4).

Exposes ``/emoji set`` (Council) and ``/emoji list`` for runtime management of
the guild_emojis mapping. This decouples reports/monthly-report rendering from
one guild's hardcoded custom-emoji IDs — a guild migration or a different
nation reusing the bot only needs a Discord command, not a code change.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import emojis as emoji_db
from core.config import Config
from services import audit

logger = logging.getLogger(__name__)


class EmojiCog(commands.Cog):
    emoji_group = app_commands.Group(
        name="emoji", description="Manage the guild emoji mapping (Council only)"
    )

    def __init__(self, bot):
        self.bot = bot

    def has_full_access(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == Config.OWNER_ID:
            return True
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        user_role_ids = [r.id for r in interaction.user.roles]
        return any(role_id in Config.FULL_ACCESS_ROLE_IDS for role_id in user_role_ids)

    @emoji_group.command(name="set", description="Set or update a guild emoji mapping")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "emoji_set"))
    @app_commands.describe(
        namespace="Which emoji set to update",
        name="The settlement/duchy name (e.g. 'Lambat City' or 'New September')",
        emoji_str="The emoji string, e.g. <:LCity:1410036718123483276> or 🌻",
    )
    async def emoji_set(
        self,
        interaction: discord.Interaction,
        namespace: str,
        name: str,
        emoji_str: str,
    ):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to manage emojis.", ephemeral=True
            )

        namespace = namespace.strip().lower()
        if namespace not in ("province", "district"):
            return await interaction.response.send_message(
                "❌ `namespace` must be `province` or `district`.", ephemeral=True
            )
        if not name.strip():
            return await interaction.response.send_message(
                "❌ `name` must not be empty.", ephemeral=True
            )
        if not emoji_str.strip():
            return await interaction.response.send_message(
                "❌ `emoji_str` must not be empty.", ephemeral=True
            )

        key = f"{namespace}:{name.strip()}"
        await interaction.response.defer(ephemeral=True)
        try:
            await emoji_db.set_emoji(key, emoji_str.strip())
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        # Audit the change.
        await audit.emit(
            audit.EMOJI_SET,
            interaction.user.id,
            None,
            {"key": key, "emoji_str": emoji_str.strip()},
        )
        await audit.post_to_channel(
            self.bot,
            audit.EMOJI_SET,
            str(interaction.user.id),
            None,
            {"key": key, "emoji_str": emoji_str.strip()},
        )

        embed = discord.Embed(
            title="✅ Emoji Updated",
            description=f"Set `{key}` → {emoji_str}",
            color=0x43B581,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @emoji_set.autocomplete("namespace")
    async def namespace_autocomplete(self, interaction: discord.Interaction, current: str):
        opts = ["province", "district"]
        return [app_commands.Choice(name=o, value=o) for o in opts if current.lower() in o.lower()][
            :25
        ]

    @emoji_group.command(name="list", description="List all guild emoji mappings")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "emoji_list"))
    async def emoji_list(self, interaction: discord.Interaction):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to view emoji mappings.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        rows = await emoji_db.list_all()
        if not rows:
            await interaction.followup.send("No emoji mappings configured yet.", ephemeral=True)
            return

        # Split into province + district sections for readability.
        provinces = [r for r in rows if r["key"].startswith("province:")]
        districts = [r for r in rows if r["key"].startswith("district:")]

        embed = discord.Embed(title="🎨 Guild Emoji Mappings", color=0x7289DA)

        if provinces:
            prov_lines = "\n".join(
                f"• `{r['key'].split(':', 1)[1]}` → {r['emoji_str']}" for r in provinces
            )
            # Cap at 1024 chars (Discord field-value limit).
            if len(prov_lines) > 1020:
                prov_lines = prov_lines[:1017] + "..."
            embed.add_field(name="Provinces / Duchies", value=prov_lines, inline=False)

        if districts:
            dist_lines = "\n".join(
                f"• `{r['key'].split(':', 1)[1]}` → {r['emoji_str']}" for r in districts
            )
            if len(dist_lines) > 1020:
                dist_lines = dist_lines[:1017] + "..."
            embed.add_field(name="Districts / Settlements", value=dist_lines, inline=False)

        embed.set_footer(text=f"{len(rows)} mappings total")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(EmojiCog(bot))
