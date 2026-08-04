"""Tests for services/recruiters.py — the ID cleaning logic (Phase 2.2).

The DB-touching functions (set_recruiters, get_recruiters, leaderboard)
require a live asyncpg pool and are covered by CI integration. Here we cover
``_clean_recruiter_ids`` — the pure helper that dedupes and validates Discord
user ID strings before they hit the junction table. A bug here would corrupt
the recruiters table, so it's the highest-value thing to unit-test.
"""

from services.recruiters import _clean_recruiter_ids


def test_clean_strips_whitespace():
    ids = ["  123  ", "456", "  789"]
    assert _clean_recruiter_ids(ids) == ["123", "456", "789"]


def test_clean_dedupes_preserving_order():
    ids = ["111", "222", "111", "333", "222"]
    assert _clean_recruiter_ids(ids) == ["111", "222", "333"]


def test_clean_drops_empty_strings():
    ids = ["123", "", "  ", "456"]
    assert _clean_recruiter_ids(ids) == ["123", "456"]


def test_clean_drops_non_numeric():
    """A garbage entry (e.g. a malformed recruiter_ids value) must not corrupt the table."""
    ids = ["123", "abc", "456", "12.34", "789"]
    assert _clean_recruiter_ids(ids) == ["123", "456", "789"]


def test_clean_accepts_int_input():
    """Discord IDs sometimes arrive as ints (from discord.Member.id); str() them."""
    ids = [123456789, "987654321"]
    assert _clean_recruiter_ids(ids) == ["123456789", "987654321"]


def test_clean_empty_list_returns_empty():
    assert _clean_recruiter_ids([]) == []


def test_clean_all_garbage_returns_empty():
    """Every entry here is empty, whitespace, or non-numeric → all dropped."""
    ids = ["", "  ", "abc", None, "12.34"]
    assert _clean_recruiter_ids(ids) == []


def test_clean_handles_realistic_snowflakes():
    """Real Discord snowflakes are 17-19 digit numeric strings."""
    ids = ["111111111111111111", "222222222222222222", "333333333333333333"]
    assert _clean_recruiter_ids(ids) == ids
