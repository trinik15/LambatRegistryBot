"""Tests for /report activity + export — Phase 3.5/3.6."""

from datetime import date

import services.charts as charts
from cogs.reports import _activity_label


class TestActivityLabel:
    """_activity_label maps CivInfo status codes to CSV labels."""

    def test_active(self):
        assert _activity_label("active") == "Active"

    def test_semi(self):
        assert _activity_label("semi") == "Semi-Active"

    def test_inactive(self):
        assert _activity_label("inactive") == "Inactive"

    def test_unknown(self):
        assert _activity_label("unknown") == "Unknown"

    def test_error(self):
        assert _activity_label("error") == "Error"

    def test_already_label(self):
        """If the status is already a label, return as-is."""
        assert _activity_label("Active") == "Active"
        assert _activity_label("Semi-Active") == "Semi-Active"

    def test_garbage_input(self):
        assert _activity_label("garbage") == "Unknown"

    def test_empty_string(self):
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
