"""Tests for self-service applications — Phase 3.4.

Tests the pure validation logic (IGN format, application status constants).
The DB layer is mocked/async and tested via integration; these tests cover
the pure helpers that don't need a DB.
"""

from cogs.applications import _IGN_PATTERN
from services.applications import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
)


class TestIgnPattern:
    """The IGN regex validates Minecraft usernames (3-16 alphanumeric+underscore)."""

    def test_valid_short(self):
        assert _IGN_PATTERN.match("abc")

    def test_valid_typical(self):
        assert _IGN_PATTERN.match("SteveB")
        assert _IGN_PATTERN.match("Alex_123")

    def test_valid_max_length(self):
        assert _IGN_PATTERN.match("a" * 16)

    def test_too_short(self):
        assert not _IGN_PATTERN.match("ab")

    def test_too_long(self):
        assert not _IGN_PATTERN.match("a" * 17)

    def test_invalid_chars(self):
        assert not _IGN_PATTERN.match("Steve-B")
        assert not _IGN_PATTERN.match("Steve B")
        assert not _IGN_PATTERN.match("Steve!")

    def test_empty(self):
        assert not _IGN_PATTERN.match("")


class TestStatusConstants:
    """The three application status constants are stable strings."""

    def test_pending(self):
        assert STATUS_PENDING == "pending"

    def test_approved(self):
        assert STATUS_APPROVED == "approved"

    def test_rejected(self):
        assert STATUS_REJECTED == "rejected"

    def test_all_three_distinct(self):
        assert len({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED}) == 3
