import discord
from discord import app_commands
from discord.ext import commands
from core import database as db
from api import civinfo_api
import utils
from core.config import Config
import io
import csv
from collections import Counter
import asyncio
import logging
from datetime import datetime
from utils import PaginationView

logger = logging.getLogger(__name__)

class ReportsCog(commands.Cog):
    report_group = app_commands.Group(name="report", description="Reports and statistics")

    def __init__(self, bot):
        self.bot = bot

    async def settlement_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await db.execute_query(
            "SELECT name FROM settlements WHERE name LIKE $1 LIMIT 25",
            (f"{current}%",),
            fetch_all=True
        )
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

    def has_full_access(self, interaction):
        if interaction.user.id == Config.OWNER_ID:
            return True
        role_ids = [r.id for r in interaction.user.roles]
        return Config.FULL_ACCESS_ROLE_ID in role_ids

    def has_view_access(self, interaction):
        if self.has_full_access(interaction):
            return True
        role_ids = [r.id for r in interaction.user.roles]
        return Config.VIEW_ACCESS_ROLE_ID in role_ids

    @report_group.command(name="census", description="Live population report")
    @app_commands.autocomplete(settlement=settlement_autocomplete)
    async def census(self, interaction: discord.Interaction, settlement: str = None):
        if not self.has_view_access(interaction):
            await interaction.response.send_message(
                "❌ You need the **Nobility** role or higher to view the census.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        # Limita concorrenza con semaforo globale
        async with self.bot.command_semaphore:
            query = "SELECT ign, settlement, address, discord_id, join_date FROM citizens"
            params = []
            if settlement:
                query += " WHERE settlement = $1"
                params.append(settlement)
            query += " ORDER BY settlement, ign"

            rows = await db.execute_query(query, params, fetch_all=True)
            if not rows:
                await interaction.followup.send(
                    "ℹ️ No citizens registered yet. Use `/citizen add` to register the first citizen.",
                    ephemeral=True
                )
                return

            data = {}
            semaphore = asyncio.Semaphore(10)  # Limita le chiamate API concorrenti

            async def get_entry(r):
                async with semaphore:
                    status, emoji, _, _ = await civinfo_api.get_player_activity(r["ign"], self.bot.http_session)
                return r["settlement"], f"{emoji} **{r['ign']}** (<@{r['discord_id']}>) — {r['address']} | Joined {r['join_date']}"

            tasks = [get_entry(r) for r in rows]
            results = await asyncio.gather(*tasks)

            for settle, entry in results:
                data.setdefault(settle, []).append(entry)

            embeds = []
            settlements = list(data.items())
            items_per_page = 5
            total_pages = (len(settlements) + items_per_page - 1) // items_per_page
            for i in range(0, len(settlements), items_per_page):
                embed = discord.Embed(title="📖 National Census", color=0xED4245)
                for settle, entries in settlements[i:i+items_per_page]:
                    value = "\n".join(entries[:8])
                    if len(entries) > 8:
                        value += f"\n*...and {len(entries)-8} more*"
                    embed.add_field(name=f"📍 {settle} ({len(entries)})", value=value[:1024], inline=False)
                embed.set_footer(text=f"Page {i//items_per_page + 1}/{total_pages} • Total: {len(rows)} citizens")
                embeds.append(embed)

            if len(embeds) == 1:
                await interaction.followup.send(embed=embeds[0])
            else:
                view = PaginationView(embeds, interaction.user.id)
                await interaction.followup.send(embed=embeds[0], view=view)

    @report_group.command(name="stats", description="Population analytics")
    @app_commands.autocomplete(settlement=settlement_autocomplete)
    async def stats(self, interaction: discord.Interaction, settlement: str = None):
        if not self.has_view_access(interaction):
            await interaction.response.send_message(
                "❌ You need the **Nobility** role or higher to view statistics.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        async with self.bot.command_semaphore:
            query = "SELECT ign, recruiter_ids, join_date FROM citizens"
            params = []
            if settlement:
                query += " WHERE settlement = $1"
                params.append(settlement)

            rows = await db.execute_query(query, params, fetch_all=True)
            if not rows:
                await interaction.followup.send(
                    "ℹ️ No citizens registered yet. Statistics will appear once you add citizens with `/citizen add`.",
                    ephemeral=True
                )
                return

            semaphore = asyncio.Semaphore(10)
            recruiters = []
            new_7d = new_30d = 0
            active = semi = inactive = 0
            now = datetime.now()

            async def process_row(r):
                nonlocal active, semi, inactive, new_7d, new_30d
                async with semaphore:
                    status, emoji, _, _ = await civinfo_api.get_player_activity(r["ign"], self.bot.http_session)
                if emoji == "🟢":
                    active += 1
                elif emoji == "🟠":
                    semi += 1
                else:
                    inactive += 1

                recruiters.extend([rid for rid in r["recruiter_ids"].split(",") if rid])

                join_date = datetime.strptime(r["join_date"], "%d/%m/%Y")
                days = (now - join_date).days
                if days <= 7:
                    new_7d += 1
                if days <= 30:
                    new_30d += 1

            await asyncio.gather(*[process_row(r) for r in rows])

            total = len(rows)
            embed = discord.Embed(title="📊 Statistics", color=0x57F287)
            embed.add_field(name="Population", value=f"Total: {total}\n🟢 {active}\n🟠 {semi}\n🔴 {inactive}", inline=True)
            embed.add_field(name="Recruitment", value=f"Last 7d: {new_7d}\nLast 30d: {new_30d}", inline=True)

            top = Counter(recruiters).most_common(5)
            if top:
                lines = [f"{i+1}. <@{rid}> – {count}" for i, (rid, count) in enumerate(top)]
                embed.add_field(name="Top Recruiters", value="\n".join(lines), inline=False)

            await interaction.followup.send(embed=embed)

    @report_group.command(name="export", description="Export registry as CSV")
    async def export(self, interaction: discord.Interaction):
        if not self.has_view_access(interaction):
            await interaction.response.send_message(
                "❌ You need the **Nobility** role or higher to export data.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        # L'export è un'operazione leggera, quindi non lo mettiamo nel semaforo globale
        rows = await db.execute_query("SELECT * FROM citizens", fetch_all=True)
        if not rows:
            await interaction.followup.send(
                "ℹ️ No data to export. Add citizens with `/citizen add` first.",
                ephemeral=True
            )
            return

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['IGN', 'DiscordID', 'Settlement', 'Recruiters', 'Address', 'Mailbox', 'Notes', 'JoinDate'])
        for r in rows:
            writer.writerow([r["ign"], r["discord_id"], r["settlement"], r["recruiter_ids"],
                             r["address"], r["mailbox"], r["notes"], r["join_date"]])
        output.seek(0)
        file = discord.File(io.BytesIO(output.getvalue().encode()), filename="pavia_registry.csv")
        await interaction.followup.send("📊 Registry exported.", file=file)

async def setup(bot):
    await bot.add_cog(ReportsCog(bot))
