"""Tests for cogs/snapshot.py — the pure embed-builder helpers (Phase 4.6).

The DB-touching command handlers require a live asyncpg pool (covered by the
integration CI). Here we cover the pure helpers that render the success
embeds — the pieces most likely to silently regress when a Discord embed
field limit changes or the note-truncation logic shifts.
"""

from datetime import date

import discord

from cogs.snapshot import (
    ANNOTATIONS_PER_PAGE,
    LIST_NOTE_TRUNCATE,
    _build_annotate_embed,
    _build_clear_embed,
    _build_list_embed,
    _format_list_line,
)

# ---------------------------------------------------------------------------
# _format_list_line — the per-row renderer for /snapshot list.
# ---------------------------------------------------------------------------


class TestFormatListLine:
    def test_short_note(self):
        line = _format_list_line("01/02/2026", "Post-exodus census")
        assert line == "• **01/02/2026** — Post-exodus census"

    def test_strips_whitespace(self):
        line = _format_list_line("01/02/2026", "  spaced note  ")
        assert line == "• **01/02/2026** — spaced note"

    def test_empty_note(self):
        # An empty note renders an empty tail after the dash.
        line = _format_list_line("01/02/2026", "")
        assert line == "• **01/02/2026** — "

    def test_none_note(self):
        # Defensive: DB rows might have NULL (filtered by the SQL, but be safe).
        line = _format_list_line("01/02/2026", None)  # type: ignore[arg-type]
        assert line == "• **01/02/2026** — "

    def test_long_note_truncated_with_ellipsis(self):
        long_note = "x" * (LIST_NOTE_TRUNCATE + 50)
        line = _format_list_line("01/02/2026", long_note)
        # Truncated to LIST_NOTE_TRUNCATE chars (117 + "...").
        assert len(line.split("— ", 1)[1]) == LIST_NOTE_TRUNCATE
        assert line.endswith("...")

    def test_exact_truncate_length_not_truncated(self):
        # A note exactly at the limit should NOT be truncated.
        note_at_limit = "x" * LIST_NOTE_TRUNCATE
        line = _format_list_line("01/02/2026", note_at_limit)
        assert not line.endswith("...")
        assert f"— {note_at_limit}" in line


# ---------------------------------------------------------------------------
# _build_annotate_embed — the success embed for /snapshot annotate.
# ---------------------------------------------------------------------------


class TestBuildAnnotateEmbed:
    def test_embed_structure(self):
        embed = _build_annotate_embed(3, "01/02/2026", "Great Diamond Crisis week")
        assert isinstance(embed, discord.Embed)
        assert embed.title == "📝 Snapshot Annotated"
        assert "3" in embed.description
        assert "01/02/2026" in embed.description
        # The note appears as a field.
        note_field = next(f for f in embed.fields if f.name == "Note")
        assert note_field.value == "Great Diamond Crisis week"
        assert "Audited" in embed.footer.text

    def test_long_note_truncated_to_field_limit(self):
        long_note = "x" * 2000  # exceeds Discord's 1024-char field limit
        embed = _build_annotate_embed(1, "01/02/2026", long_note)
        note_field = next(f for f in embed.fields if f.name == "Note")
        assert len(note_field.value) == 1024

    def test_zero_rows(self):
        # The handler refuses zero-row updates before building the embed, but
        # the helper itself should still render (defensive).
        embed = _build_annotate_embed(0, "01/02/2026", "test")
        assert "0" in embed.description


# ---------------------------------------------------------------------------
# _build_list_embed — the /snapshot list embed from DB rows.
# ---------------------------------------------------------------------------


class TestBuildListEmbed:
    def test_empty_rows_is_still_valid(self):
        # The handler short-circuits on empty rows, but the helper should
        # not crash if called with [].
        embed = _build_list_embed([])
        assert embed.title == "📝 Annotated Snapshots"
        assert "0 snapshot(s)" in embed.description

    def test_single_row(self):
        rows = [{"snapshot_date": date(2026, 2, 1), "notes": "First annotated census"}]
        embed = _build_list_embed(rows)
        field = embed.fields[0]
        assert "01/02/2026" in field.value
        assert "First annotated census" in field.value
        assert f"max {ANNOTATIONS_PER_PAGE}" in field.name

    def test_multiple_rows_joined_with_newlines(self):
        rows = [
            {"snapshot_date": date(2026, 2, 1), "notes": "Newer"},
            {"snapshot_date": date(2026, 1, 1), "notes": "Older"},
        ]
        embed = _build_list_embed(rows)
        field = embed.fields[0]
        assert "\n" in field.value
        assert "Newer" in field.value
        assert "Older" in field.value

    def test_row_with_none_notes_renders_empty(self):
        # Defensive — the SQL filters NULL notes, but the helper shouldn't crash.
        rows = [{"snapshot_date": date(2026, 2, 1), "notes": None}]
        embed = _build_list_embed(rows)
        field = embed.fields[0]
        assert "01/02/2026" in field.value

    def test_field_value_capped_at_1024(self):
        # Many rows with long notes should be capped at Discord's field limit.
        rows = [{"snapshot_date": date(2026, 2, 1), "notes": "x" * 200} for _ in range(50)]
        embed = _build_list_embed(rows)
        field = embed.fields[0]
        assert len(field.value) <= 1024


# ---------------------------------------------------------------------------
# _build_clear_embed — the success embed for /snapshot clear.
# ---------------------------------------------------------------------------


class TestBuildClearEmbed:
    def test_embed_structure(self):
        embed = _build_clear_embed(5, "01/02/2026")
        assert isinstance(embed, discord.Embed)
        assert embed.title == "🗑️ Snapshot Annotation Cleared"
        assert "5" in embed.description
        assert "01/02/2026" in embed.description
        assert "preserved" in embed.footer.text

    def test_embed_color_is_red(self):
        # Red (0xED4245) signals a destructive op (note removal).
        embed = _build_clear_embed(1, "01/02/2026")
        assert embed.color.value == 0xED4245
