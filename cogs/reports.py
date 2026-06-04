import discord
from discord import app_commands
from discord.ext import commands
from core import database as db
from api import civinfo_api
from core.config import Config
import logging
import csv
import io
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import utils

logger = logging.getLogger(__name__)


class ReportsCog(commands.Cog):
    reports_group = app_commands.Group(name="report", description="Population and activity reports")

    def __init__(self, bot):
        self.bot = bot

    def has_view_access(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == Config.OWNER_ID:
            return True
        role_ids = [r.id for r in interaction.user.roles]
        return Config.VIEW_ACCESS_ROLE_ID in role_ids

    async def settlement_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await db.execute_query("SELECT name FROM settlements ORDER BY name", fetch_all=True)
        names = [row["name"] for row in rows]
        filtered = [name for name in names if current.lower() in name.lower()]
        return [app_commands.Choice(name=name, value=name) for name in filtered[:25]]

    @reports_group.command(name="census", description="Generate population census report")
    @app_commands.autocomplete(settlement=settlement_autocomplete)
    @app_commands.checks.cooldown(1, Config.COOLDOWN_SLOW, key=lambda i: (i.user.id, "report_census"))
    async def report_census(self, interaction: discord.Interaction, settlement: Optional[str] = None):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message("❌ You don't have permission to view reports.", ephemeral=True)

        await interaction.response.defer()

        # Build query based on settlement filter
        if settlement:
            rows = await db.execute_query(
                "SELECT ign, settlement, discord_id, join_date FROM citizens WHERE settlement = $1 ORDER BY ign",
                (settlement,), fetch_all=True
            )
            title = f"📊 Census Report: {settlement}"
        else:
            rows = await db.execute_query(
                "SELECT ign, settlement, discord_id, join_date FROM citizens ORDER BY settlement, ign",
                fetch_all=True
            )
            title = "📊 National Census Report"

        if not rows:
            await interaction.followup.send("No citizens found matching the criteria.", ephemeral=True)
            return

        # Group by settlement for display
        from collections import defaultdict
        settlements_data = defaultdict(list)
        for row in rows:
            settlements_data[row["settlement"]].append(row)

        # Create paginated embeds
        embeds = []
        total_citizens = len(rows)
        processed = 0

        for settlement_name, citizens in settlements_data.items():
            # Fetch activity status for each citizen (this is expensive - do in batches)
            activity_data = {}
            for citizen in citizens:
                ign = citizen["ign"]
                status, emoji, last_login, status_text = await civinfo_api.get_player_activity(ign, self.bot.http_session)
                activity_data[ign] = {"emoji": emoji, "status": status_text}

            active_count = sum(1 for data in activity_data.values() if data["status"] == "Active")

            embed = discord.Embed(
                title=f"{title} - {settlement_name}",
                color=0x7289DA
            )

            citizen_lines = []
            for citizen in citizens:
                ign = citizen["ign"]
                activity = activity_data[ign]
                citizen_lines.append(f"{activity['emoji']} **{ign}**")
                if len(citizen_lines) == 20:  # Limit per page
                    break

            if citizen_lines:
                embed.add_field(
                    name=f"Citizens ({len(citizens)})",
                    value="\n".join(citizen_lines),
                    inline=True
                )
                embed.add_field(
                    name="Activity",
                    value=f"🟢 Active: {active_count}\n🔴 Inactive: {len(citizens) - active_count}",
                    inline=True
                )
            else:
                embed.add_field(name="Citizens", value="None", inline=True)

            processed += len(citizens)
            embed.set_footer(text=f"Total citizens: {total_citizens} | Showing {processed}/{total_citizens}")
            embeds.append(embed)

            if len(embeds) >= 25:  # Discord embed limit per message
                break

        view = utils.PaginationView(embeds, interaction.user, timeout=300)
        await interaction.followup.send(embed=embeds[0], view=view)

    @reports_group.command(name="stats", description="Show population statistics")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "report_stats"))
    async def report_stats(self, interaction: discord.Interaction):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message("❌ You don't have permission to view statistics.", ephemeral=True)

        await interaction.response.defer()

        # Get total citizens
        total_result = await db.execute_query("SELECT COUNT(*) FROM citizens", fetch_one=True)
        total_citizens = total_result[0] if total_result else 0

        # Get citizens by settlement
        settlement_stats = await db.execute_query(
            "SELECT settlement, COUNT(*) FROM citizens GROUP BY settlement ORDER BY COUNT(*) DESC",
            fetch_all=True
        )

        # Get total settlements
        settlement_count = await db.execute_query("SELECT COUNT(*) FROM settlements", fetch_one=True)
        total_settlements = settlement_count[0] if settlement_count else 0

        # Get recent joins (last 30 days)
        recent_cutoff = (datetime.now() - timedelta(days=30)).strftime("%d/%m/%Y")
        recent_result = await db.execute_query(
            "SELECT COUNT(*) FROM citizens WHERE join_date >= $1",
            (recent_cutoff,), fetch_one=True
        )
        recent_joins = recent_result[0] if recent_result else 0

        embed = discord.Embed(
            title="📈 Population Statistics",
            color=0x7289DA
        )
        embed.add_field(name="Total Citizens", value=str(total_citizens), inline=True)
        embed.add_field(name="Total Settlements", value=str(total_settlements), inline=True)
        embed.add_field(name="Recent Joins (30d)", value=str(recent_joins), inline=True)

        if settlement_stats:
            stats_text = "\n".join(f"• {row['settlement']}: {row['count']}" for row in settlement_stats[:10])
            if len(settlement_stats) > 10:
                stats_text += f"\n*and {len(settlement_stats) - 10} more settlements...*"
            embed.add_field(name="Top Settlements", value=stats_text, inline=False)

        await interaction.followup.send(embed=embed)

    @reports_group.command(name="export", description="Export citizen data as CSV")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_SLOW, key=lambda i: (i.user.id, "report_export"))
    async def report_export(self, interaction: discord.Interaction):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message("❌ You don't have permission to export data.", ephemeral=True)

        await interaction.response.defer()

        rows = await db.execute_query(
            "SELECT ign, discord_id, settlement, join_date, address, mailbox FROM citizens ORDER BY settlement, ign",
            fetch_all=True
        )

        if not rows:
            await interaction.followup.send("No data to export.", ephemeral=True)
            return

        # Create CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["IGN", "Discord ID", "Settlement", "Join Date", "Address", "Mailbox"])

        for row in rows:
            writer.writerow([
                row["ign"],
                row["discord_id"],
                row["settlement"],
                row["join_date"],
                row.get("address", ""),
                row.get("mailbox", "")
            ])

        output.seek(0)
        file = discord.File(io.BytesIO(output.getvalue().encode()), filename="citizens_export.csv")

        embed = discord.Embed(
            title="📎 Data Export",
            description=f"Exported {len(rows)} citizens to CSV.",
            color=0x43B581
        )
        await interaction.followup.send(embed=embed, file=file)


async def setup(bot):
    await bot.add_cog(ReportsCog(bot))
