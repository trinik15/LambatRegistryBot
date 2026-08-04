"""Tests for /settlement info — Phase 3.3 dashboard growth computation."""

from cogs.settlement import _compute_growth_text


class TestComputeGrowthText:
    """_compute_growth_text renders the growth field for the dashboard."""

    def test_no_snapshots(self):
        assert _compute_growth_text(None) == "N/A"
        assert _compute_growth_text([]) == "N/A"

    def test_single_snapshot(self):
        """First snapshot shows the initial count."""
        snapshots = [{"snapshot_date": "2025-01-01", "total": 10, "active": 7}]
        result = _compute_growth_text(snapshots)
        assert "+10" in result
        assert "first snapshot" in result

    def test_growth_positive(self):
        """Population grew from 10 to 15 over 3 months."""
        snapshots = [
            {"snapshot_date": "2025-01-01", "total": 10, "active": 7},
            {"snapshot_date": "2025-02-01", "total": 12, "active": 8},
            {"snapshot_date": "2025-03-01", "total": 15, "active": 9},
        ]
        result = _compute_growth_text(snapshots)
        assert "+5" in result
        assert "3 months" in result

    def test_growth_negative(self):
        """Population shrank from 15 to 10."""
        snapshots = [
            {"snapshot_date": "2025-01-01", "total": 15, "active": 10},
            {"snapshot_date": "2025-02-01", "total": 10, "active": 6},
        ]
        result = _compute_growth_text(snapshots)
        assert "-5" in result
        assert "2 months" in result

    def test_growth_zero(self):
        """Population unchanged."""
        snapshots = [
            {"snapshot_date": "2025-01-01", "total": 10, "active": 5},
            {"snapshot_date": "2025-02-01", "total": 10, "active": 6},
        ]
        result = _compute_growth_text(snapshots)
        assert "+0" in result or "0" in result
