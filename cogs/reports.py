import discord
from discord import app_commands
from discord.ext import commands
from core import database as db
from core.config import Config
from api import civinfo_api
from tasks.activity_monitor import _fetch_activities
import logging
import csv
import io
from typing import Optional, List, Dict, Any
from collections import defaultdict
import utils

logger = logging.getLogger(__name__)

# Discord caps a single embed field value at 1024 chars. We show at most 20
# citizens per field (each line ~30 chars) and cap the whole report at 25
# embeds/pages — generous enough for a typical nation's settlements, and the
# PaginationView navigates them one embed at a time (so Discord's 10-embeds-
# per-message limit never applies). If we exceed the cap we say so honestly.
CITIZENS_PER_FIELD = 20
MAX_EMBEDS = 25


class ReportsCog(commands.Cog):
    reports_group = app_commands.Group(name="report", description="Population and activity reports")

    def __init__(self, bot):
        self.bot = bot

    def has_view_access(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == Config.OWNER_ID:
            return True
        # DM context: no roles to check — deny (owner is handled above).
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
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
        settlements_data = defaultdict(list)
        for row in rows:
            settlements_data[row["settlement"]].append(row)

        # Fetch activity for EVERY citizen in one concurrent batch (was a
        # sequential await per citizen — slow enough to risk Discord's
        # interaction token expiry on big registries).
        all_igns = [row["ign"] for row in rows]
        activities = await _fetch_activities(all_igns, self.bot.http_session)

        # If the CivInfo API is auth-broken, every entry will be ⚪ "API Auth
        # Required". Rather than showing a census full of ⚪ and implying
        # nobody is active, we annotate the report honestly.
        auth_broken = civinfo_api.is_auth_broken()

        embeds = []
        total_citizens = len(rows)
        shown_citizens = 0
        truncated = False

        for settlement_name, citizens in settlements_data.items():
            if len(embeds) >= MAX_EMBEDS:
                truncated = True
                break

            # Activity summary is computed once for the whole settlement.
            activity_data = {}
            for citizen in citizens:
                ign = citizen["ign"]
                status, emoji, last_login, status_text = activities.get(
                    ign, ("error", "⚪", None, "Error"))
                activity_data[ign] = {"emoji": emoji, "status": status_text, "raw_status": status}

            # civinfo_api returns emoji 🟢 (active <30d), 🟠 (semi 30-60d),
            # 🔴 (inactive >60d), ⚪ (unknown/error). The status_text is never
            # the literal "Active", so we must key off the emoji — not the text.
            active_count = sum(1 for d in activity_data.values() if d["emoji"] == "🟢")
            semi_count = sum(1 for d in activity_data.values() if d["emoji"] == "🟠")
            inactive_count = sum(1 for d in activity_data.values() if d["emoji"] == "🔴")
            unknown_count = len(citizens) - active_count - semi_count - inactive_count

            if auth_broken:
                activity_field = (
                    f"⚠️ **Activity data unavailable**\n"
                    f"CivInfo API auth required — contact an admin.\n"
                    f"_(counts below are unreliable)_\n"
                    f"🟢 Active: {active_count}\n"
                    f"🟠 Semi-Active: {semi_count}\n"
                    f"🔴 Inactive: {inactive_count}\n"
                    f"⚪ Unknown: {unknown_count}"
                )
            else:
                activity_field = (
                    f"🟢 Active: {active_count}\n"
                    f"🟠 Semi-Active: {semi_count}\n"
                    f"🔴 Inactive: {inactive_count}\n"
                    f"⚪ Unknown: {unknown_count}"
                )

            # Chunk citizens so we actually show all of them instead of lying
            # about the count (old code broke after 20 but reported len(citizens)).
            chunks = [citizens[i:i + CITIZENS_PER_FIELD]
                      for i in range(0, len(citizens), CITIZENS_PER_FIELD)] or [[]]
            num_chunks = len(chunks)

            for idx, chunk in enumerate(chunks, start=1):
                if len(embeds) >= MAX_EMBEDS:
                    truncated = True
                    break

                part_label = f" ({idx}/{num_chunks})" if num_chunks > 1 else ""
                embed = discord.Embed(
                    title=f"{title} - {settlement_name}{part_label}",
                    color=0x7289DA
                )
                if chunk:
                    citizen_lines = []
                    for citizen in chunk:
                        ign = citizen["ign"]
                        activity = activity_data[ign]
                        citizen_lines.append(f"{activity['emoji']} **{ign}**")
                    embed.add_field(
                        name=f"Citizens ({len(citizens)} total, showing {len(chunk)})",
                        value="\n".join(citizen_lines),
                        inline=True
                    )
                    embed.add_field(name="Activity", value=activity_field, inline=True)
                    shown_citizens += len(chunk)
                else:
                    embed.add_field(name="Citizens", value="None", inline=True)

                embed.set_footer(
                    text=f"Total citizens: {total_citizens} | Listed so far: {shown_citizens}/{total_citizens}"
                )
                embeds.append(embed)

        if truncated:
            # Annotate the final embed so the user knows data was omitted,
            # rather than silently dropping settlements.
            last = embeds[-1]
            note = last.footer.text or ""
            last.set_footer(text=f"{note} | ⚠️ Report truncated ({MAX_EMBEDS}-page limit) — refine with /report census <settlement>")

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
        # join_date is a real DATE column (migrated from TEXT), so we can
        # compare chronologically instead of lexicographically.
        recent_result = await db.execute_query(
            "SELECT COUNT(*) FROM citizens WHERE join_date >= CURRENT_DATE - INTERVAL '30 days'",
            fetch_one=True
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

    @reports_group.command(name="trends", description="Show historical population trend charts")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_SLOW, key=lambda i: (i.user.id, "report_trends"))
    async def report_trends(self, interaction: discord.Interaction):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message("❌ You don't have permission to view trends.", ephemeral=True)

        await interaction.response.defer()

        # Fetch all duchy-level snapshots (district IS NULL) for the national
        # totals + active/inactive panels.
        snapshots = await db.execute_query(
            "SELECT snapshot_date, duchy, district, total, active "
            "FROM monthly_snapshots WHERE district IS NULL "
            "ORDER BY snapshot_date",
            fetch_all=True
        )

        if not snapshots:
            await interaction.followup.send(
                "No monthly snapshots found yet. Snapshots are generated on the 1st of each month by the daily activity check.",
                ephemeral=True
            )
            return

        # Fetch the top settlements' history for the optional third panel.
        # "Top" = the settlements with the highest current population.
        top_settlement_names = await db.execute_query(
            "SELECT settlement, COUNT(*) as cnt FROM citizens "
            "GROUP BY settlement ORDER BY cnt DESC LIMIT 8",
            fetch_all=True
        )
        top_names = [r["settlement"] for r in top_settlement_names] if top_settlement_names else []

        top_settlements = []
        if top_names:
            # Build a parameterized IN-clause ($1, $2, ...)
            placeholders = ", ".join(f"${i+1}" for i in range(len(top_names)))
            top_settlements = await db.execute_query(
                f"SELECT snapshot_date, district, total FROM monthly_snapshots "
                f"WHERE district IN ({placeholders}) "
                f"ORDER BY snapshot_date",
                tuple(top_names),
                fetch_all=True
            )

        # Render the chart in an executor (matplotlib is sync / CPU-bound).
        import asyncio
        import services.charts as charts

        try:
            png_bytes = await asyncio.get_event_loop().run_in_executor(
                None,
                charts.render_population_trends,
                snapshots,
                top_settlements if top_settlements else None
            )
        except Exception as e:
            logger.error(f"Failed to render trends chart: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Could not render the chart: `{e}`", ephemeral=True
            )
            return

        if not png_bytes:
            await interaction.followup.send(
                "Not enough data to render a trend chart yet.", ephemeral=True
            )
            return

        # Build a summary embed alongside the chart.
        dates = sorted(set(s["snapshot_date"] for s in snapshots))
        first_date = dates[0]
        last_date = dates[-1]

        # National totals for the most recent snapshot.
        latest_snapshots = [s for s in snapshots if s["snapshot_date"] == last_date]
        latest_total = sum(s["total"] for s in latest_snapshots)
        latest_active = sum(s["active"] for s in latest_snapshots)

        # Earliest snapshot for growth calc.
        earliest = [s for s in snapshots if s["snapshot_date"] == first_date]
        earliest_total = sum(s["total"] for s in earliest) if earliest else 0

        embed = discord.Embed(
            title="📈 Population Trends",
            description=(
                f"Monthly snapshots from **{utils.format_date(first_date)}** "
                f"to **{utils.format_date(last_date)}** ({len(dates)} data points)"
            ),
            color=0x3BAD4C
        )
        embed.add_field(name="Current Total", value=str(latest_total), inline=True)
        embed.add_field(name="Current Active", value=str(latest_active), inline=True)

        if earliest_total and earliest_total > 0:
            growth = latest_total - earliest_total
            pct = round(growth / earliest_total * 100, 1)
            sign = "+" if growth >= 0 else ""
            embed.add_field(
                name=f"Growth (since {utils.format_date(first_date)})",
                value=f"{sign}{growth} ({sign}{pct}%)",
                inline=True
            )

        embed.add_field(
            name="Active Rate",
            value=f"{round(latest_active/latest_total*100, 1)}%" if latest_total else "N/A",
            inline=True
        )

        embed.set_image(url="attachment://trends.png")
        embed.set_footer(text="Charts rendered from monthly_snapshots • Generated by /report trends")

        file = discord.File(io.BytesIO(png_bytes), filename="trends.png")
        await interaction.followup.send(embed=embed, file=file)

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
                utils.format_date(row["join_date"]),
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
