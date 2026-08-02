import discord
from discord import app_commands
from discord.ext import commands
from core.config import Config
import logging

logger = logging.getLogger(__name__)


class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show help and available commands")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 Lambat National Registry",
            description=(
                "Official bot for tracking citizens, settlements, and population "
                "trends for the Lambat nation on CivMC."
            ),
            color=0x5865F2
        )
        embed.add_field(
            name="👤 Citizen Commands",
            value=(
                "`/citizen add` – Register a new citizen *(Council only)*\n"
                "`/citizen update` – Update citizen info *(Council only)*\n"
                "`/citizen remove` – Remove a citizen *(Council only)*\n"
                "`/citizen list` – List all citizens by settlement\n"
                "`/citizen dossier` – View a citizen's full dossier"
            ),
            inline=False
        )
        embed.add_field(
            name="🏘️ Settlement Commands",
            value=(
                "`/settlement add` – Add a settlement *(Council only)*\n"
                "`/settlement remove` – Remove an empty settlement *(Council only)*\n"
                "`/settlement list` – List all settlements"
            ),
            inline=False
        )
        embed.add_field(
            name="📊 Reports",
            value=(
                "`/report census` – Live population & activity report\n"
                "`/report stats` – Quick population statistics\n"
                "`/report trends` – Historical population trend charts\n"
                "`/report export` – Download citizen data as CSV"
            ),
            inline=False
        )
        embed.add_field(
            name="🖥️ CivMC Server",
            value=(
                "`/server status` – Live server status, player count, MOTD\n"
                "`/server ping` – Quick one-line server check"
            ),
            inline=False
        )
        embed.add_field(
            name="💾 Data Management *(Council only)*",
            value=(
                "`/data backup` – Create a manual database backup\n"
                "`/data list_backups` – List all available backups\n"
                "`/data restore` – Restore the database from a backup"
            ),
            inline=False
        )
        embed.add_field(
            name="⚙️ Owner",
            value="`/sync` – Re-sync slash commands to this server *(owner only)*",
            inline=False
        )
        embed.add_field(
            name="Activity Legend",
            value="🟢 Active (<30d)  •  🟠 Semi-Active (30-60d)  •  🔴 Inactive (>60d)  •  ⚪ Unknown",
            inline=False
        )
        embed.set_footer(text="Lambat Registry Bot • Report issues to your nation leadership")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="sync", description="Re-sync slash commands to this server (owner only)")
    async def sync(self, interaction: discord.Interaction):
        """Owner-only manual command sync.

        Useful after deploying new commands or removing old ones. If GUILD_ID
        is set, syncs to that guild (instant); otherwise syncs globally.
        """
        if interaction.user.id != Config.OWNER_ID:
            await interaction.response.send_message(
                "❌ This command is restricted to the bot owner.",
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if Config.GUILD_ID:
                guild = discord.Object(id=Config.GUILD_ID)
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
                await interaction.followup.send(
                    f"✅ Synced {len(synced)} commands to guild {Config.GUILD_ID}.",
                    ephemeral=True
                )
            else:
                synced = await self.bot.tree.sync()
                await interaction.followup.send(
                    f"✅ Synced {len(synced)} commands globally (may take up to 1h to appear).",
                    ephemeral=True
                )
        except Exception as e:
            logger.error(f"Manual sync failed: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ Sync failed. Check the bot logs for details.",
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(HelpCog(bot))
