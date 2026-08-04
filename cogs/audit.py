"""Audit log search cog (Phase 2.1).

Exposes ``/audit search`` to Council members with filters by actor, action,
target IGN, and date range. Results are paginated via ``PaginationView``.
The full JSONB details are rendered compactly per entry.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import utils
from core.config import Config
from services import audit

logger = logging.getLogger(__name__)


def _action_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=a.replace(".", " "), value=a) for a in audit.ALL_ACTIONS]


class AuditCog(commands.Cog):
    audit_group = app_commands.Group(
        name="audit", description="Search the registry audit log (Council only)"
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

    @audit_group.command(name="search", description="Search the audit log")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_SLOW, key=lambda i: (i.user.id, "audit_search")
    )
    async def audit_search(
        self,
        interaction: discord.Interaction,
        actor: discord.Member | None = None,
        action: str | None = None,
        target: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to search the audit log.", ephemeral=True
            )

        # Validate + coerce filters.
        since_date = None
        until_date = None
        if since:
            if not utils.is_valid_date(since):
                return await interaction.response.send_message(
                    "❌ `since` must be a real past date in DD/MM/YYYY format.", ephemeral=True
                )
            since_date = utils.parse_join_date(since)
        if until:
            if not utils.is_valid_date(until):
                return await interaction.response.send_message(
                    "❌ `until` must be a real past date in DD/MM/YYYY format.", ephemeral=True
                )
            until_date = utils.parse_join_date(until)

        if limit < 1 or limit > 200:
            return await interaction.response.send_message(
                "❌ `limit` must be between 1 and 200.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        rows = await audit.search(
            actor_discord_id=str(actor.id) if actor else None,
            action=action,
            target_ign=target if target else None,
            since=since_date,
            until=until_date,
            limit=limit,
        )

        if not rows:
            await interaction.followup.send("No audit entries match those filters.", ephemeral=True)
            return

        embeds = _build_audit_embeds(rows, limit)
        view = utils.PaginationView(embeds, interaction.user, timeout=300)
        await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @audit_search.autocomplete("action")
    async def action_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = _action_choices()
        filtered = [c for c in choices if current.lower() in c.name.lower()]
        return filtered[:25]


def _build_audit_embeds(rows: list[dict], requested_limit: int) -> list[discord.Embed]:
    """Build paginated embeds for audit search results.

    Each embed shows up to 8 entries (keeps each field under Discord's 1024-char
    limit with room for the details summary). A trailing summary embed lists
    the filter + count.
    """
    embeds: list[discord.Embed] = []
    per_page = 8
    for i in range(0, len(rows), per_page):
        chunk = rows[i : i + per_page]
        embed = discord.Embed(
            title="📜 Audit Log",
            description=f"Showing {i + 1}–{i + len(chunk)} of {len(rows)} entries",
            color=0x7289DA,
        )
        for r in chunk:
            ts = r["ts"]
            if isinstance(ts, str):
                ts_str = ts
            else:
                ts_str = ts.strftime("%Y-%m-%d %H:%M UTC")
            actor_id = r["actor_discord_id"]
            actor_str = f"<@{actor_id}>" if actor_id else "—"
            target_str = r["target_ign"] or "—"
            details_str = _format_details(r["details"])
            label = f"`{r['action']}` • {ts_str}"
            value = f"**Actor:** {actor_str}\n**Target:** `{target_str}`\n{details_str}"
            embed.add_field(name=label, value=value, inline=False)
        embeds.append(embed)

    embeds[-1].set_footer(
        text=f"{len(rows)} of {requested_limit} requested • Newest first • /audit search"
    )
    return embeds


def _format_details(details) -> str:
    """Render a JSONB details value as a compact string."""
    if details is None:
        return ""
    # asyncpg returns JSONB as a string (JSON text) unless a codec is registered.
    if isinstance(details, str):
        import json

        try:
            details = json.loads(details)
        except (json.JSONDecodeError, ValueError):
            return f"**Details:** {details}"
    if not isinstance(details, dict):
        return f"**Details:** {details}"
    # Reuse the audit summariser for a consistent look with the channel mirror.
    return audit._summarise_details(details)  # noqa: SLF001 — same package intent


async def setup(bot):
    await bot.add_cog(AuditCog(bot))
