import discord
from discord import app_commands
from discord.ext import commands
from core.config import Config          # <-- MODIFICATO

class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show help")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 Pavia National Registry",
            description="Official bot for tracking citizens and settlements.",
            color=0x5865F2
        )
        embed.add_field(
            name="Citizen Commands",
            value=(
                "`/citizen add` – Register a new citizen\n"          # <-- MODIFICATO
                "`/citizen info` – View citizen dossier\n"          # <-- MODIFICATO
                "`/citizen update` – Update citizen info\n"         # <-- MODIFICATO
                "`/citizen remove` – Remove a citizen\n"            # <-- MODIFICATO
                "`/citizen list` – List all citizens"               # <-- MODIFICATO
            ),
            inline=False
        )
        embed.add_field(
            name="Settlement Commands",
            value=(
                "`/settlement add` – Add a settlement\n"            # <-- MODIFICATO
                "`/settlement remove` – Remove a settlement (must be empty)\n" # <-- MODIFICATO
                "`/settlement list` – List all settlements"         # <-- MODIFICATO
            ),
            inline=False
        )
        embed.add_field(
            name="Reports",
            value=(
                "`/report census` – Live population report\n"       # <-- MODIFICATO
                "`/report stats` – Population analytics\n"          # <-- MODIFICATO
                "`/report export` – Download CSV"                   # <-- MODIFICATO
            ),
            inline=False
        )
        embed.add_field(
            name="Data Management",
            value=(
                "`/data backup` – Create a manual backup\n"         # <-- MODIFICATO
                "`/data list` – List all backups\n"                 # <-- MODIFICATO
                "`/data restore` – Restore a backup (owner only)"   # <-- MODIFICATO
            ),
            inline=False
        )
        embed.set_footer(text="Activity: 🟢 <30d, 🟠 30-60d, 🔴 >60d, ⚪ Unknown")
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(HelpCog(bot))
