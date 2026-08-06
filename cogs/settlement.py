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
        # Phase 3.7: also mirror to the governance channel (wider council).
        await audit.post_to_governance_channel(
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
        # Phase 3.7: also mirror to the governance channel (wider council).
        await audit.post_to_governance_channel(
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

    @settlement_group.command(name="info", description="Show a dashboard for a single settlement")
    @app_commands.autocomplete(name=settlement_autocomplete)
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "settlement_info")
    )
    async def settlement_info(self, interaction: discord.Interaction, name: str):
        """Phase 3.3: single-settlement dashboard embed.

        Shows total citizens, activity breakdown, growth since last snapshot,
        top recruiters, and a paginated member list.
        """
        if not self.has_full_access(interaction) and interaction.user.id != Config.OWNER_ID:
            # Settlement info is council-only (contains recruiter + activity data).
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.", ephemeral=True
            )

        await interaction.response.defer()

        # 1. Settlement exists?
        settlement = await db.execute_query(
            "SELECT name, duchy FROM settlements WHERE name = $1", (name,), fetch_one=True
        )
        if not settlement:
            await interaction.followup.send(
                f"❌ Settlement `{name}` does not exist.", ephemeral=True
            )
            return

        # 2. Citizens in this settlement.
        citizens = await db.execute_query(
            "SELECT ign, discord_id, join_date FROM citizens WHERE settlement = $1 ORDER BY ign",
            (name,),
            fetch_all=True,
        )
        total = len(citizens) if citizens else 0

        # 3. Activity breakdown (batch fetch via the existing CivInfo helper).
        from tasks.activity_monitor import _fetch_activities

        activities: dict = {}
        if citizens:
            igns = [c["ign"] for c in citizens]
            activities = await _fetch_activities(igns, self.bot.http_session)

        active_count = sum(
            1 for pa in activities.values() if pa.emoji == "🟢"
        )
        semi_count = sum(1 for pa in activities.values() if pa.emoji == "🟠")
        inactive_count = sum(1 for pa in activities.values() if pa.emoji == "🔴")
        active_rate = round(active_count / total * 100, 1) if total else 0.0

        # 4. Growth since last snapshot.
        snapshots = await db.execute_query(
            "SELECT snapshot_date, total, active FROM monthly_snapshots "
            "WHERE district = $1 ORDER BY snapshot_date",
            (name,),
            fetch_all=True,
        )
        growth_text = _compute_growth_text(snapshots)

        # 5. Top recruiters for this settlement (from the recruiters junction).
        top_recruiters = await db.execute_query(
            "SELECT r.recruiter_discord_id, COUNT(*) as cnt "
            "FROM recruiters r JOIN citizens c ON r.ign = c.ign "
            "WHERE c.settlement = $1 "
            "GROUP BY r.recruiter_discord_id ORDER BY cnt DESC LIMIT 5",
            (name,),
            fetch_all=True,
        )

        # 6. Build the dashboard embed.
        duchy_name = settlement["duchy"]
        emoji = await emoji_db.get_province(duchy_name)
        header = f"{emoji} {name}".strip() if emoji else name

        embed = discord.Embed(
            title=f"🏘️ {header} — Settlement Dashboard",
            description=f"Duchy: **{duchy_name}**",
            color=0x7289DA,
        )
        embed.add_field(name="Total Citizens", value=str(total), inline=True)
        embed.add_field(
            name="Active Rate",
            value=f"{active_rate}%" if total else "N/A",
            inline=True,
        )
        embed.add_field(name="Growth", value=growth_text, inline=True)

        # Activity breakdown.
        if total:
            unknown = total - active_count - semi_count - inactive_count
            embed.add_field(
                name="Activity Breakdown",
                value=(
                    f"🟢 Active: {active_count}\n"
                    f"🟠 Semi-Active: {semi_count}\n"
                    f"🔴 Inactive: {inactive_count}\n"
                    f"⚪ Unknown: {unknown}"
                ),
                inline=True,
            )

        # Top recruiters.
        if top_recruiters:
            lines = []
            for idx, row in enumerate(top_recruiters, start=1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
                lines.append(f"{medal} <@{row['recruiter_discord_id']}> — **{row['cnt']}**")
            embed.add_field(name="Top Recruiters", value="\n".join(lines)[:1024], inline=True)

        # Member list (first 20, paginated if more).
        if citizens:
            member_lines = [f"• {c['ign']}" for c in citizens[:20]]
            member_value = "\n".join(member_lines)
            if total > 20:
                member_value += f"\n*...and {total - 20} more — use /citizen list*"
            embed.add_field(
                name=f"Members ({total})",
                value=member_value[:1024],
                inline=False,
            )

        embed.set_footer(text=f"Settlement: {name} • Data: registry + CivInfo")
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Pure helpers (testable without Discord / DB)
# ---------------------------------------------------------------------------


def _compute_growth_text(snapshots: list[dict] | None) -> str:
    """Compute a human-readable growth string from monthly snapshots.

    Returns 'N/A' if no snapshots, '+N (last M months)' if growth exists,
    or a 'no change' note if the count is flat.
    """
    if not snapshots or len(snapshots) < 1:
        return "N/A"
    if len(snapshots) == 1:
        return f"+{snapshots[0]['total']} (first snapshot)"
    first = snapshots[0]
    last = snapshots[-1]
    diff = last["total"] - first["total"]
    months = len(snapshots)
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff} (last {months} months)"


async def setup(bot):
    await bot.add_cog(SettlementCog(bot))
