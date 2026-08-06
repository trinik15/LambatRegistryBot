import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.config import Config
from core.i18n import tr

logger = logging.getLogger(__name__)


class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show help and available commands")
    async def help(self, interaction: discord.Interaction):
        # Phase 4.5: all user-facing strings are looked up via tr(), which
        # reads from locales/{en,fil}.json. The default lang is Config.LOCALE
        # (env var LOCALE); individual calls can override, but /help uses the
        # configured default so a Filipino-themed deployment just sets
        # LOCALE=fil and gets the translated embed.
        embed = discord.Embed(
            title=tr("help.title"),
            description=tr("help.description"),
            color=0x5865F2,
        )
        embed.add_field(
            name=tr("help.section.citizen"),
            value=(
                f"{tr('help.citizen.add')}\n"
                f"{tr('help.citizen.update')}\n"
                f"{tr('help.citizen.remove')}\n"
                f"{tr('help.citizen.list')}\n"
                f"{tr('help.citizen.dossier')}"
            ),
            inline=False,
        )
        embed.add_field(
            name=tr("help.section.settlement"),
            value=(
                f"{tr('help.settlement.add')}\n"
                f"{tr('help.settlement.remove')}\n"
                f"{tr('help.settlement.list')}"
            ),
            inline=False,
        )
        embed.add_field(
            name=tr("help.section.reports"),
            value=(
                f"{tr('help.report.census')}\n"
                f"{tr('help.report.stats')}\n"
                f"{tr('help.report.trends')}\n"
                f"{tr('help.report.export')}"
            ),
            inline=False,
        )
        embed.add_field(
            name=tr("help.section.server"),
            value=(f"{tr('help.server.status')}\n{tr('help.server.ping')}"),
            inline=False,
        )
        embed.add_field(
            name=tr("help.section.data"),
            value=(
                f"{tr('help.data.backup')}\n"
                f"{tr('help.data.list_backups')}\n"
                f"{tr('help.data.restore')}"
            ),
            inline=False,
        )
        embed.add_field(
            name=tr("help.section.owner"),
            value=tr("help.owner.sync"),
            inline=False,
        )
        embed.add_field(
            name="Activity Legend",
            value=tr("help.activity_legend"),
            inline=False,
        )
        embed.set_footer(text=tr("help.footer"))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="sync", description="Re-sync slash commands to this server (owner only)"
    )
    async def sync(self, interaction: discord.Interaction):
        """Owner-only manual command sync.

        Useful after deploying new commands or removing old ones. If GUILD_ID
        is set, syncs to that guild (instant); otherwise syncs globally.
        """
        if interaction.user.id != Config.OWNER_ID:
            await interaction.response.send_message(
                "❌ This command is restricted to the bot owner.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if Config.GUILD_ID:
                guild = discord.Object(id=Config.GUILD_ID)
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
                await interaction.followup.send(
                    f"✅ Synced {len(synced)} commands to guild {Config.GUILD_ID}.", ephemeral=True
                )
            else:
                synced = await self.bot.tree.sync()
                await interaction.followup.send(
                    f"✅ Synced {len(synced)} commands globally (may take up to 1h to appear).",
                    ephemeral=True,
                )
        except Exception as e:
            logger.error(f"Manual sync failed: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ Sync failed. Check the bot logs for details.", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(HelpCog(bot))
