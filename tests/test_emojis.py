"""Tests for core/emojis.py — key validation + cache behaviour (Phase 2.4).

The DB-touching functions (get, set_emoji, list_all) require a live pool and
are covered by CI integration. Here we cover the pure validation logic (which
runs BEFORE any DB call and must reject malformed input loudly) and the
cache invalidation semantics.

pytest-asyncio is in auto mode (pyproject.toml), so async tests run without
decorators.
"""

import pytest

from core import emojis as emoji_db


@pytest.fixture(autouse=True)
def _reset_cache():
    """Ensure each test starts and ends with a clean (None) cache.

    Without this, one test's populated cache would leak into the next, making
    failures depend on execution order — the classic test-isolation trap.
    """
    emoji_db._cache = None
    yield
    emoji_db._cache = None


# ---------------------------------------------------------------------------
# set_emoji — key validation (runs before the DB upsert)
# ---------------------------------------------------------------------------


async def test_set_emoji_rejects_empty_key():
    with pytest.raises(ValueError, match="namespace:name"):
        await emoji_db.set_emoji("", "🌻")


async def test_set_emoji_rejects_key_without_colon():
    with pytest.raises(ValueError, match="namespace:name"):
        await emoji_db.set_emoji("LambatCity", "🌻")


async def test_set_emoji_rejects_unknown_namespace():
    with pytest.raises(ValueError, match="Unknown emoji namespace"):
        await emoji_db.set_emoji("city:Lambat", "🌻")


async def test_set_emoji_rejects_empty_emoji_str():
    with pytest.raises(ValueError, match="emoji_str must not be empty"):
        await emoji_db.set_emoji("province:Lambat City", "")


async def test_set_emoji_rejects_whitespace_only_emoji_str():
    with pytest.raises(ValueError, match="emoji_str must not be empty"):
        await emoji_db.set_emoji("province:Lambat City", "   ")


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def test_invalidate_clears_cache():
    """invalidate() resets the cache so the next get() reloads from the DB."""
    emoji_db._cache = {"province:Test": "🌟"}
    emoji_db.invalidate()
    assert emoji_db._cache is None


def test_invalidate_is_idempotent():
    """Calling invalidate() twice is safe (cache is already None)."""
    emoji_db._cache = {"province:Test": "🌟"}
    emoji_db.invalidate()
    emoji_db.invalidate()  # must not raise
    assert emoji_db._cache is None


# ---------------------------------------------------------------------------
# Key construction + lookup (pure, uses a pre-populated cache so _ensure_loaded
# never touches the DB)
# ---------------------------------------------------------------------------


async def test_get_province_returns_cached_value():
    emoji_db._cache = {"province:Lambat City": "<:LCity:123>"}
    assert await emoji_db.get_province("Lambat City") == "<:LCity:123>"


async def test_get_district_returns_cached_value():
    emoji_db._cache = {"district:New September": "<:LCity:123>"}
    assert await emoji_db.get_district("New September") == "<:LCity:123>"


async def test_get_province_empty_duchy_returns_empty():
    emoji_db._cache = {}
    assert await emoji_db.get_province("") == ""


async def test_get_missing_key_returns_empty_string():
    """A key not in the DB degrades to empty string (not an error)."""
    emoji_db._cache = {"province:Other": "🌟"}
    assert await emoji_db.get("province:Nonexistent") == ""


async def test_get_district_empty_settlement_returns_empty():
    emoji_db._cache = {}
    assert await emoji_db.get_district("") == ""
