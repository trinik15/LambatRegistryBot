"""Tests for api/civinfo_api.py — the CivInfo TTL cache + auth-broken logic.

Phase A refactor: the return type changed from a 4-tuple to a frozen
``PlayerActivity`` dataclass. These tests cover the honest-degradation branches
(auth-broken TTL logic, per-status cache TTLs) AND the new mc-accounts/full
response parsing (timestamp conversion, bucketing, is_online derivation).
"""

import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
# CivInfoCache — set/get/invalidate/clear (now with PlayerActivity)
# ---------------------------------------------------------------------------


def _pa(status="ok", emoji="🟢", last_login=None, text="Active (today)", **kw):
    """Helper: build a PlayerActivity with sensible defaults."""
    from api.civinfo_api import PlayerActivity

    return PlayerActivity(
        status=status, emoji=emoji, last_login=last_login, status_text=text, **kw
    )


def test_cache_returns_none_for_missing_key():
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    assert c.get("ghost") is None


def test_cache_set_then_get_returns_data():
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    payload = _pa(last_login=datetime(2024, 6, 1, tzinfo=UTC))
    c.set("Notch", payload)
    assert c.get("Notch") == payload


def test_cache_invalidate_drops_single_entry():
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    c.set("a", _pa(text="x"))
    c.set("b", _pa(text="y"))
    c.invalidate("a")
    assert c.get("a") is None
    assert c.get("b") is not None


def test_cache_clear_drops_everything():
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    c.set("a", _pa(text="x"))
    c.set("b", _pa(text="y"))
    c.clear()
    assert c.get("a") is None
    assert c.get("b") is None


def test_cache_get_evicts_expired_entry():
    """An expired entry is dropped on read and returns None."""
    from api.civinfo_api import CivInfoCache

    c = CivInfoCache()
    c.set("stale", _pa(status="error", emoji="⚪", text="x"), ttl=0)
    # ttl=0 means it's already expired the instant after it was stored.
    import time

    time.sleep(0.001)
    assert c.get("stale") is None


# ---------------------------------------------------------------------------
# PlayerActivity — the dataclass that replaced the 4-tuple
# ---------------------------------------------------------------------------


def test_player_activity_is_frozen():
    """PlayerActivity is immutable — can't mutate fields after creation."""
    from api.civinfo_api import PlayerActivity

    pa = PlayerActivity(status="ok", emoji="🟢", last_login=None, status_text="x")
    with pytest.raises((AttributeError, TypeError)):
        pa.status = "error"  # type: ignore[misc]


def test_player_activity_is_online_true_when_login_newer_than_logout():
    from api.civinfo_api import PlayerActivity

    pa = PlayerActivity(
        status="ok",
        emoji="🟢",
        last_login=datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
        status_text="x",
        last_logout=datetime(2024, 6, 1, 10, 0, tzinfo=UTC),
    )
    assert pa.is_online is True


def test_player_activity_is_online_false_when_logout_newer_than_login():
    from api.civinfo_api import PlayerActivity

    pa = PlayerActivity(
        status="ok",
        emoji="🟢",
        last_login=datetime(2024, 6, 1, 10, 0, tzinfo=UTC),
        status_text="x",
        last_logout=datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
    )
    assert pa.is_online is False


def test_player_activity_is_online_true_when_logout_none():
    """No logout recorded → assume still online (last_login present)."""
    from api.civinfo_api import PlayerActivity

    pa = PlayerActivity(
        status="ok",
        emoji="🟢",
        last_login=datetime(2024, 6, 1, tzinfo=UTC),
        status_text="x",
    )
    assert pa.is_online is True


def test_player_activity_is_online_false_when_login_none():
    from api.civinfo_api import PlayerActivity

    pa = PlayerActivity(status="error", emoji="⚪", last_login=None, status_text="x")
    assert pa.is_online is False


# ---------------------------------------------------------------------------
# _parse_ts — epoch-ms → UTC datetime conversion
# ---------------------------------------------------------------------------


def test_parse_ts_valid_int():
    from api.civinfo_api import _parse_ts

    # 1717200000000 ms = 2024-06-01T00:00:00Z
    result = _parse_ts(1717200000000)
    assert result == datetime(2024, 6, 1, 0, 0, tzinfo=UTC)


def test_parse_ts_valid_float():
    from api.civinfo_api import _parse_ts

    result = _parse_ts(1717200000000.0)
    assert result == datetime(2024, 6, 1, 0, 0, tzinfo=UTC)


def test_parse_ts_none():
    from api.civinfo_api import _parse_ts

    assert _parse_ts(None) is None


def test_parse_ts_garbage_returns_none():
    from api.civinfo_api import _parse_ts

    assert _parse_ts("not-a-number") is None
    assert _parse_ts([]) is None


# ---------------------------------------------------------------------------
# _bucket_activity — the 30d/60d Active/Semi/Inactive bucketing
# ---------------------------------------------------------------------------


def test_bucket_active_today():
    from api.civinfo_api import _bucket_activity

    now = datetime.now(UTC)
    emoji, text = _bucket_activity(now)
    assert emoji == "🟢"
    assert "today" in text


def test_bucket_active_recent():
    from datetime import timedelta

    from api.civinfo_api import _bucket_activity

    ten_days_ago = datetime.now(UTC) - timedelta(days=10)
    emoji, text = _bucket_activity(ten_days_ago)
    assert emoji == "🟢"
    assert "10d ago" in text


def test_bucket_semi_inactive():
    from datetime import timedelta

    from api.civinfo_api import _bucket_activity

    forty_days_ago = datetime.now(UTC) - timedelta(days=40)
    emoji, text = _bucket_activity(forty_days_ago)
    assert emoji == "🟠"
    assert "40d ago" in text


def test_bucket_inactive():
    from datetime import timedelta

    from api.civinfo_api import _bucket_activity

    ninety_days_ago = datetime.now(UTC) - timedelta(days=90)
    emoji, text = _bucket_activity(ninety_days_ago)
    assert emoji == "🔴"
    assert "90d ago" in text


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


# ---------------------------------------------------------------------------
# get_player_activity — the HTTP path (mocked)
# ---------------------------------------------------------------------------


def _mock_response(status, json_data=None):
    """Build an aiohttp response mock."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    # async context manager: __aenter__ returns self, __aexit__ returns False
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
async def test_get_player_activity_success_parses_mc_accounts_full():
    """A 200 with a valid accounts[0] yields a PlayerActivity with all fields."""
    import api.civinfo_api as civinfo_api

    # login = 2024-06-01T00:00:00Z, logout = 2024-06-01T02:00:00Z, joined = 2023-01-01
    mock_resp = _mock_response(
        200,
        {
            "accounts": [
                {
                    "uuid": "abc",
                    "mc_name": "Notch",
                    "first_joined": 1672531200000,
                    "last_login": 1717200000000,
                    "last_logout": 1717207200000,
                }
            ]
        },
    )
    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    with patch("core.config.Config.CIVINFO_API_KEY", "fake-key"), patch(
        "core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"
    ), patch(
        "core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"
    ), patch(
        "core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"
    ):
        pa = await civinfo_api.get_player_activity("Notch", session)

    assert pa.status == "ok"
    assert pa.last_login == datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    assert pa.last_logout == datetime(2024, 6, 1, 2, 0, tzinfo=UTC)
    assert pa.first_joined == datetime(2023, 1, 1, 0, 0, tzinfo=UTC)
    assert pa.is_online is False  # logout (02:00) > login (00:00)


@pytest.mark.asyncio
async def test_get_player_activity_not_found():
    """Empty accounts list → not_found status."""
    import api.civinfo_api as civinfo_api

    mock_resp = _mock_response(200, {"accounts": []})
    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    with patch("core.config.Config.CIVINFO_API_KEY", "fake-key"), patch(
        "core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"
    ), patch(
        "core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"
    ), patch(
        "core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"
    ):
        pa = await civinfo_api.get_player_activity("Ghost", session)

    assert pa.status == "not_found"
    assert pa.last_login is None


@pytest.mark.asyncio
async def test_get_player_activity_403_marks_auth_broken():
    """A 403 response marks auth-broken and returns an error PlayerActivity."""
    import api.civinfo_api as civinfo_api

    mock_resp = _mock_response(403)
    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    with patch("core.config.Config.CIVINFO_API_KEY", "fake-key"), patch(
        "core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"
    ), patch(
        "core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"
    ), patch(
        "core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"
    ):
        pa = await civinfo_api.get_player_activity("Notch", session)

    assert pa.status == "error"
    assert civinfo_api.is_auth_broken() is True


@pytest.mark.asyncio
async def test_get_player_activity_sends_correct_params():
    """The request uses mcName (singular) + mcServer + civinfo-version header."""
    import api.civinfo_api as civinfo_api

    mock_resp = _mock_response(200, {"accounts": []})
    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    with patch("core.config.Config.CIVINFO_API_KEY", "my-key"), patch(
        "core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"
    ), patch(
        "core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"
    ), patch(
        "core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"
    ):
        await civinfo_api.get_player_activity("Notch", session)

    session.get.assert_called_once()
    call_args = session.get.call_args
    assert call_args.kwargs["params"] == {"mcName": "Notch", "mcServer": "play.civmc.net"}
    headers = call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer my-key"
    assert headers["civinfo-version"] == "git:abc123"
