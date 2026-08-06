"""Phase 4.6: snapshot annotations.

Council-only commands to annotate ``monthly_snapshots`` with free-text context
("post-exodus census", "snapshot taken during the Great Diamond Crisis week").
The monthly report auto-saves rows with ``notes=NULL``; this cog lets leadership
attach historical context after the fact, so future ``/report trends`` views
can surface the annotation alongside the numbers.

Commands:
  /snapshot annotate <date> <note>  — set or replace the note for a snapshot date
  /snapshot list                     — list all snapshots that have a note
  /snapshot clear <date>             — remove the note for a snapshot date

All mutations emit to ``audit_log`` (action=``snapshot.annotate``) so the change
is auditable like every other registry mutation.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core import database as db
from core.config import Config
from services import audit
from utils import format_date, parse_join_date

logger = logging.getLogger(__name__)

# How many annotated snapshots /list shows per page. Discord embeds cap a single
# field value at 1024 chars; 15 annotated rows is roughly the comfortable limit.
ANNOTATIONS_PER_PAGE = 15

# How long the per-row note can be in /snapshot list before truncation. The full
# note is visible via re-running /snapshot annotate (which echoes what's set).
LIST_NOTE_TRUNCATE = 120


def _format_list_line(date_str: str, note: str) -> str:
    """Render a single annotated-snapshot row for the /snapshot list embed.

    Pure helper (testable without Discord/DB). Truncates long notes with an
    ellipsis so the field value stays readable.
    """
    note = (note or "").strip()
    if len(note) > LIST_NOTE_TRUNCATE:
        note = note[: LIST_NOTE_TRUNCATE - 3] + "..."
    return f"• **{date_str}** — {note}"


def _build_annotate_embed(rows_updated: int, date_str: str, note: str) -> discord.Embed:
    """Build the success embed shown after /snapshot annotate.

    Pure helper — caller passes the already-formatted date string + the note
    + the row count returned by UPDATE ... WHERE snapshot_date = $1.
    """
    embed = discord.Embed(
        title="📝 Snapshot Annotated",
        description=(f"Set the note for **{rows_updated}** snapshot row(s) on `{date_str}`."),
        color=0x5865F2,
    )
    embed.add_field(name="Note", value=note[:1024], inline=False)
    embed.set_footer(text="Visible in /report trends • Audited")
    return embed


def _build_list_embed(rows: list[dict]) -> discord.Embed:
    """Build the /snapshot list embed from DB rows.

    Pure helper. Each row must have ``snapshot_date`` (date/str) + ``notes``
    (str). Rows are assumed already sorted newest-first by the SQL query.
    """
    embed = discord.Embed(
        title="📝 Annotated Snapshots",
        description=f"{len(rows)} snapshot(s) with historical context (newest first).",
        color=0x5865F2,
    )
    lines = [_format_list_line(format_date(r["snapshot_date"]), r["notes"] or "") for r in rows]
    embed.add_field(
        name=f"Recent annotations (max {ANNOTATIONS_PER_PAGE})",
        value="\n".join(lines)[:1024],
        inline=False,
    )
    embed.set_footer(text="Use /snapshot annotate <date> <note> to edit")
    return embed


def _build_clear_embed(rows_updated: int, date_str: str) -> discord.Embed:
    """Build the success embed shown after /snapshot clear."""
    embed = discord.Embed(
        title="🗑️ Snapshot Annotation Cleared",
        description=(f"Removed the note from **{rows_updated}** snapshot row(s) on `{date_str}`."),
        color=0xED4245,
    )
    embed.set_footer(text="Snapshot data preserved • Audited")
    return embed


class SnapshotCog(commands.Cog):
    """Council-only snapshot annotation commands (Phase 4.6)."""

    def __init__(self, bot):
        self.bot = bot

    def has_full_access(self, interaction: discord.Interaction) -> bool:
        """Mirror CitizenCog's Council-permission check."""
        if interaction.user.id == Config.OWNER_ID:
            return True
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        user_role_ids = [r.id for r in interaction.user.roles]
        return any(role_id in Config.FULL_ACCESS_ROLE_IDS for role_id in user_role_ids)

    snapshot_group = app_commands.Group(
        name="snapshot",
        description="Annotate monthly census snapshots with historical context (Council)",
    )

    @snapshot_group.command(
        name="annotate", description="Set or replace the note on a monthly snapshot"
    )
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "snapshot_annotate")
    )
    @app_commands.describe(
        date="Snapshot date, DD/MM/YYYY (e.g. 01/02/2026 = 1 February 2026)",
        note="Free-text annotation (max 500 chars). Replaces any existing note.",
    )
    async def snapshot_annotate(self, interaction: discord.Interaction, date: str, note: str):
        """Attach a free-text note to every snapshot row for a given date.

        The monthly report saves one row per (duchy, district) per date, so
        "annotating a snapshot" really means setting ``notes`` on every row
        with ``snapshot_date = $1``. This keeps the annotation visible
        regardless of which duchy/district the report filters on.
        """
        if not self.has_full_access(interaction):
            await interaction.response.send_message(
                "❌ You need the Council role to annotate snapshots.", ephemeral=True
            )
            return

        note = note.strip()
        if not note:
            await interaction.response.send_message(
                "❌ Note cannot be empty. Use `/snapshot clear <date>` to remove a note.",
                ephemeral=True,
            )
            return
        if len(note) > 500:
            await interaction.response.send_message(
                "❌ Note is too long (max 500 characters).", ephemeral=True
            )
            return

        snapshot_date = parse_join_date(date)
        if snapshot_date is None:
            await interaction.response.send_message(
                "❌ Invalid date format. Use DD/MM/YYYY (e.g. 01/02/2026).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # UPDATE every row for this date in one shot. If no rows exist, the
        # monthly report hasn't run for that date yet — we refuse rather than
        # silently no-op'ing, so the council knows nothing changed.
        rows_updated = await db.execute_query(
            "UPDATE monthly_snapshots SET notes = $1 WHERE snapshot_date = $2",
            (note, snapshot_date),
        )

        if rows_updated == 0:
            await interaction.followup.send(
                f"❌ No snapshots found for `{format_date(snapshot_date)}`. "
                "Snapshots are created automatically on the 1st of each month by the "
                "daily activity check.",
                ephemeral=True,
            )
            return

        # Audit-log the annotation (target_ign=None — this isn't a citizen op).
        await audit.emit(
            audit.SNAPSHOT_ANNOTATE,
            str(interaction.user.id),
            None,
            {"date": format_date(snapshot_date), "note": note},
        )

        embed = _build_annotate_embed(rows_updated, format_date(snapshot_date), note)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @snapshot_group.command(name="list", description="List all snapshots that have an annotation")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "snapshot_list")
    )
    async def snapshot_list(self, interaction: discord.Interaction):
        """List every snapshot date that has a non-NULL note.

        Shows the date + note (truncated) for each annotated snapshot, newest
        first. Useful for leadership to recall the historical context at a
        glance during planning sessions.
        """
        if not self.has_full_access(interaction):
            await interaction.response.send_message(
                "❌ You need the Council role to list snapshot annotations.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        rows = await db.execute_query(
            "SELECT DISTINCT snapshot_date, notes FROM monthly_snapshots "
            "WHERE notes IS NOT NULL ORDER BY snapshot_date DESC LIMIT $1",
            (ANNOTATIONS_PER_PAGE,),
            fetch_all=True,
        )

        if not rows:
            await interaction.followup.send(
                "No snapshots have been annotated yet. Use `/snapshot annotate "
                "<date> <note>` to add context to a past census.",
                ephemeral=True,
            )
            return

        embed = _build_list_embed([dict(r) for r in rows])
        await interaction.followup.send(embed=embed, ephemeral=True)

    @snapshot_group.command(name="clear", description="Remove the note from a monthly snapshot")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_MEDIUM, key=lambda i: (i.user.id, "snapshot_clear")
    )
    @app_commands.describe(
        date="Snapshot date, DD/MM/YYYY (e.g. 01/02/2026 = 1 February 2026)",
    )
    async def snapshot_clear(self, interaction: discord.Interaction, date: str):
        """Clear the note on every snapshot row for a given date.

        Sets ``notes = NULL`` for all rows with ``snapshot_date = $1``. The
        snapshot data (total/active counts) is preserved — only the annotation
        is removed.
        """
        if not self.has_full_access(interaction):
            await interaction.response.send_message(
                "❌ You need the Council role to clear snapshot annotations.",
                ephemeral=True,
            )
            return

        snapshot_date = parse_join_date(date)
        if snapshot_date is None:
            await interaction.response.send_message(
                "❌ Invalid date format. Use DD/MM/YYYY (e.g. 01/02/2026).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        rows_updated = await db.execute_query(
            "UPDATE monthly_snapshots SET notes = NULL WHERE snapshot_date = $1 "
            "AND notes IS NOT NULL",
            (snapshot_date,),
        )

        if rows_updated == 0:
            await interaction.followup.send(
                f"No annotations found on `{format_date(snapshot_date)}` (nothing to clear).",
                ephemeral=True,
            )
            return

        await audit.emit(
            audit.SNAPSHOT_ANNOTATE,
            str(interaction.user.id),
            None,
            {"date": format_date(snapshot_date), "note": None, "action": "clear"},
        )

        embed = _build_clear_embed(rows_updated, format_date(snapshot_date))
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SnapshotCog(bot))
