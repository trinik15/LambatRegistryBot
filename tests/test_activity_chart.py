"""Tests for /report activity + export — Phase 3.5/3.6 + Phase A (WS-3, fix B1).

Phase A update: ``_activity_label`` now accepts either a ``PlayerActivity`` or
a raw status string (for the DB-LEFT-JOIN path in report_export). The mapping
was fixed — it used to expect 'active'/'semi'/'inactive' but the API client
returns 'ok'/'not_found'/'error', so live-fetch labels always fell through to
"Unknown". Now the label is derived from the emoji (the bucket signal) when
status is 'ok'.
"""

from datetime import date

import services.charts as charts
from cogs.reports import _activity_label


def _pa(status="ok", emoji="🟢", **kw):
    """Helper: build a PlayerActivity with sensible defaults."""
    from api.civinfo_api import PlayerActivity

    return PlayerActivity(status=status, emoji=emoji, last_login=None, status_text="x", **kw)


class TestActivityLabelPlayerActivity:
    """_activity_label maps a PlayerActivity to a CSV label (Phase A path)."""

    def test_ok_active_emoji(self):
        """status=ok + emoji=🟢 → 'Active'."""
        assert _activity_label(_pa(status="ok", emoji="🟢")) == "Active"

    def test_ok_semi_emoji(self):
        """status=ok + emoji=🟠 → 'Semi-Active'."""
        assert _activity_label(_pa(status="ok", emoji="🟠")) == "Semi-Active"

    def test_ok_inactive_emoji(self):
        """status=ok + emoji=🔴 → 'Inactive'."""
        assert _activity_label(_pa(status="ok", emoji="🔴")) == "Inactive"

    def test_not_found(self):
        assert _activity_label(_pa(status="not_found", emoji="⚪")) == "Not Found"

    def test_error(self):
        assert _activity_label(_pa(status="error", emoji="⚪")) == "Error"


class TestActivityLabelString:
    """_activity_label also accepts a raw status string (DB LEFT-JOIN path).

    The activity_cache.status column stores 'ok'/'not_found'/'error' (new in
    Phase A) plus legacy values 'active'/'semi'/'inactive'/'unknown' from
    older code paths. Both must map correctly.
    """

    def test_string_ok_without_emoji_falls_to_unknown(self):
        """A bare 'ok' string (no emoji) → 'Unknown' (can't derive bucket)."""
        assert _activity_label("ok") == "Unknown"

    def test_string_not_found(self):
        assert _activity_label("not_found") == "Not Found"

    def test_string_error(self):
        assert _activity_label("error") == "Error"

    def test_string_legacy_active(self):
        assert _activity_label("active") == "Active"

    def test_string_legacy_semi(self):
        assert _activity_label("semi") == "Semi-Active"

    def test_string_legacy_inactive(self):
        assert _activity_label("inactive") == "Inactive"

    def test_string_legacy_unknown(self):
        assert _activity_label("unknown") == "Unknown"

    def test_string_already_label_passes_through_via_fallthrough(self):
        """A label like 'Active' isn't a known raw code → returns 'Unknown'.

        Note: this is why report_export checks `if status in {known raw codes}`
        before calling _activity_label — already-resolved labels are passed
        through directly. This test documents the edge case.
        """
        assert _activity_label("Active") == "Unknown"

    def test_string_garbage(self):
        assert _activity_label("garbage") == "Unknown"

    def test_string_empty(self):
        assert _activity_label("") == "Unknown"


class TestRenderActivitySeries:
    """render_activity_series renders a PNG from time-series data."""

    def test_empty_data_returns_none(self):
        assert charts.render_activity_series("Test", [], []) is None
        assert charts.render_activity_series("Test", None, None) is None

    def test_single_point(self):
        """A single data point should still render (flat line)."""
        png = charts.render_activity_series("Test Settlement", [date(2025, 1, 1)], [10])
        assert png is not None
        assert len(png) > 100  # non-trivial PNG

    def test_multiple_points(self):
        dates = [date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)]
        totals = [10, 12, 15]
        actives = [7, 8, 9]
        png = charts.render_activity_series("Growth", dates, totals, actives)
        assert png is not None
        assert len(png) > 500

    def test_totals_without_actives(self):
        """actives=None should still render (total line only)."""
        dates = [date(2025, 1, 1), date(2025, 2, 1)]
        totals = [10, 12]
        png = charts.render_activity_series("Totals Only", dates, totals, None)
        assert png is not None

    def test_mismatched_lengths_ignored(self):
        """If actives length != totals length, the active series is skipped."""
        dates = [date(2025, 1, 1), date(2025, 2, 1)]
        totals = [10, 12]
        actives = [7]  # wrong length
        png = charts.render_activity_series("Mismatched", dates, totals, actives)
        assert png is not None  # still renders, just without the active overlay
