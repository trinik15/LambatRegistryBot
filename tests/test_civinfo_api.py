"""Tests for api/civinfo_api.py — the CivInfo TTL cache + auth-broken logic.

These cover the "honest degradation" branches that are the whole reason the
bot doesn't silently report fake "0 active" counts when CivInfo auth fails.
"""

import importlib
from datetime import UTC, datetime

import pytest


@pytest.fixture(autouse=True)
def reset_civinfo_state():
    """Reset module-level auth/cache state before every test.

    civinfo_api keeps ``_auth_broken_until``, ``_auth_warned`` and the shared
    ``cache`` as module globals. Without a reset, one test's auth-broken flag
    would leak into the next. We reload the module to get a clean slate.
    """
    import api.civinfo_api as civinfo_api

    importlib.reload(civinfo_api)
    yield
    # Clean up after (in case a test marked auth-broken).
    civinfo_api._auth_broken_until = 0.0
    civinfo_api._auth_warned = False
    civinfo_api.cache.clear()


# ---------------------------------------------------------------------------
# _ttl_for — the per-status cache TTL that stops transient outages poisoning
# the cache for the full 5-minute success window.
# ---------------------------------------------------------------------------


def test_ttl_for_ok_returns_success_ttl():
    from api.civinfo_api import CACHE_TTL_SUCCESS, _ttl_for

    assert _ttl_for("ok") == CACHE_TTL_SUCCESS == 300


def test_ttl_for_not_found_returns_shorter_ttl():
    from api.civinfo_api import CACHE_TTL_NOT_FOUND, _ttl_for

    assert _ttl_for("not_found") == CACHE_TTL_NOT_FOUND == 120


def test_ttl_for_error_returns_error_ttl():
    from api.civinfo_api import CACHE_TTL_ERROR, _ttl_for

    assert _ttl_for("error") == CACHE_TTL_ERROR == 60


def test_ttl_for_unknown_status_falls_back_to_error_ttl():
    from api.civinfo_api import CACHE_TTL_ERROR, _ttl_for

    assert _ttl_for("something_unexpected") == CACHE_TTL_ERROR


# ---------------------------------------------------------------------------
# CivInfoCache — set/get/invalidate/clear
# ---------------------------------------------------------------------------


def test_cache_returns_none_for_missing_key():
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    assert c.get("ghost") is None


def test_cache_set_then_get_returns_data():
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    payload = ("ok", "🟢", datetime(2024, 6, 1, tzinfo=UTC), "Active (today)")
    c.set("Notch", payload)
    assert c.get("Notch") == payload


def test_cache_invalidate_drops_single_entry():
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    c.set("a", ("ok", "🟢", None, "x"))
    c.set("b", ("ok", "🟢", None, "y"))
    c.invalidate("a")
    assert c.get("a") is None
    assert c.get("b") is not None


def test_cache_clear_drops_everything():
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    c.set("a", ("ok", "🟢", None, "x"))
    c.set("b", ("ok", "🟢", None, "y"))
    c.clear()
    assert c.get("a") is None
    assert c.get("b") is None


def test_cache_get_evicts_expired_entry():
    """An expired entry is dropped on read and returns None."""
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    c.set("stale", ("error", "⚪", None, "x"), ttl=0)
    # ttl=0 means it's already expired the instant after it was stored.
    import time

    time.sleep(0.001)
    assert c.get("stale") is None


# ---------------------------------------------------------------------------
# Auth-broken state — the honest-degradation flag.
# ---------------------------------------------------------------------------


def test_auth_not_broken_by_default():
    from api.civinfo_api import is_auth_broken

    assert is_auth_broken() is False


def test_mark_auth_broken_sets_flag():
    from api.civinfo_api import _mark_auth_broken, is_auth_broken

    _mark_auth_broken("HTTP 401")
    assert is_auth_broken() is True


def test_auth_broken_clears_after_ttl():
    """The auth-broken flag auto-expires so a fixed key takes effect."""
    import api.civinfo_api as civinfo_api

    civinfo_api._mark_auth_broken("HTTP 401")
    assert civinfo_api.is_auth_broken() is True
    # Force the deadline into the past.
    civinfo_api._auth_broken_until = 0.0
    assert civinfo_api.is_auth_broken() is False


def test_mark_auth_broken_warns_once_per_window():
    """The loud log fires once per window, not on every call."""
    import api.civinfo_api as civinfo_api

    civinfo_api._mark_auth_broken("HTTP 401")
    assert civinfo_api._auth_warned is True
    # Second mark must NOT reset the warned flag (it stays True until cleared).
    warned_before = civinfo_api._auth_warned
    civinfo_api._mark_auth_broken("HTTP 403")
    assert civinfo_api._auth_warned is warned_before is True


def test_clear_auth_broken_resets_flag():
    from api.civinfo_api import _clear_auth_broken, _mark_auth_broken, is_auth_broken

    _mark_auth_broken("HTTP 401")
    assert is_auth_broken() is True
    _clear_auth_broken()
    assert is_auth_broken() is False


def test_clear_auth_broken_resets_warned_flag():
    """After recovery, a new outage can warn again."""
    import api.civinfo_api as civinfo_api

    civinfo_api._mark_auth_broken("HTTP 401")
    assert civinfo_api._auth_warned is True
    civinfo_api._clear_auth_broken()
    assert civinfo_api._auth_warned is False
