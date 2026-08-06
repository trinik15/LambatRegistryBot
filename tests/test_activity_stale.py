"""Tests for CivInfo graceful degradation (Proposal 1) — the ``stale`` flag.

Covers the honest-degradation path when CivInfo auth is broken: the daily loop
flags every ``activity_cache`` row ``stale=TRUE`` (without touching last_login),
and readers (the churn-alert task) skip stale rows so a recruiter isn't nudged
about a citizen who actually logged in recently but CivInfo couldn't tell us.

Research context (ROADMAP §8.5): the original proposal planned a per-citizen
``is_online`` refresh from mcsrvstat's player list. Live probing proved CivMC
hides its player sample (``players: {"online": N, "max": M}``, no ``list``),
so that half was dropped. The staleness-marking half — which the proposal
already described — is what shipped.
"""

from unittest.mock import AsyncMock, patch

import pytest

from tasks import activity_monitor, churn_alerts

# ---------------------------------------------------------------------------
# Fake asyncpg pool/connection (same shape as test_audit_retention)
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, status: str = "UPDATE 0"):
        self.status = status
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *params):
        self.calls.append((sql, params))
        return self.status


class _FakePoolAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakePoolAcquire(self._conn)


# ---------------------------------------------------------------------------
# mark_all_stale — the staleness valve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_all_stale_runs_update_and_parses_count():
    """A 'UPDATE N' status string is parsed into the integer row count."""
    conn = _FakeConn(status="UPDATE 45")
    fake_pool = _FakePool(conn)
    with patch("core.database.get_pool", new=AsyncMock(return_value=fake_pool)):
        result = await activity_monitor.mark_all_stale()

    assert result == 45
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    # The UPDATE must set stale=TRUE and must NOT touch last_login (the cached
    # value stays as the last known good, just flagged as aging).
    assert "UPDATE activity_cache SET stale = TRUE" in sql
    assert "last_login" not in sql
    assert params == ()


@pytest.mark.asyncio
async def test_mark_all_stale_zero_rows():
    """'UPDATE 0' (empty table) parses to 0 — not an error."""
    conn = _FakeConn(status="UPDATE 0")
    fake_pool = _FakePool(conn)
    with patch("core.database.get_pool", new=AsyncMock(return_value=fake_pool)):
        result = await activity_monitor.mark_all_stale()
    assert result == 0


@pytest.mark.asyncio
async def test_mark_all_stale_unparseable_status_returns_zero():
    conn = _FakeConn(status="weird")
    fake_pool = _FakePool(conn)
    with patch("core.database.get_pool", new=AsyncMock(return_value=fake_pool)):
        result = await activity_monitor.mark_all_stale()
    assert result == 0


# ---------------------------------------------------------------------------
# _fetch_mcsrvstat_player_count — the context-only aggregate fetch
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status = status
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    def get(self, url):
        return self._resp


@pytest.mark.asyncio
async def test_mcsrvstat_player_count_parses_online_count():
    resp = _FakeResp(200, {"online": True, "players": {"online": 102, "max": 300}})
    result = await activity_monitor._fetch_mcsrvstat_player_count(_FakeSession(resp))
    assert result == 102


@pytest.mark.asyncio
async def test_mcsrvstat_player_count_server_offline_returns_zero():
    resp = _FakeResp(200, {"online": False, "players": {"online": 0, "max": 300}})
    result = await activity_monitor._fetch_mcsrvstat_player_count(_FakeSession(resp))
    assert result == 0


@pytest.mark.asyncio
async def test_mcsrvstat_player_count_non_200_returns_none():
    resp = _FakeResp(503)
    result = await activity_monitor._fetch_mcsrvstat_player_count(_FakeSession(resp))
    assert result is None


@pytest.mark.asyncio
async def test_mcsrvstat_player_count_no_list_key_still_works():
    """ROADMAP §8.5: CivMC omits players.list entirely — only aggregate count.

    The helper reads players.online (an int), NOT players.list, so the absent
    list must not cause a failure. This is the exact response shape confirmed
    by live-probing api.mcsrvstat.us/3/play.civmc.net.
    """
    resp = _FakeResp(200, {"online": True, "players": {"online": 102, "max": 300}})
    result = await activity_monitor._fetch_mcsrvstat_player_count(_FakeSession(resp))
    assert result == 102  # no KeyError, no list access


# ---------------------------------------------------------------------------
# churn _fetch_candidates — must skip stale rows (protects Proposal 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_churn_fetch_candidates_skips_stale_rows(monkeypatch):
    """The candidate query must include a NOT COALESCE(stale, FALSE) clause.

    This is the core correctness guard: without it, a CivInfo outage would
    flag every citizen stale, then the churn task would nudge recruiters about
    citizens whose last_login is aging but who may have logged in yesterday
    (CivInfo just couldn't tell us). We assert the SQL contains the guard
    rather than driving a real DB.
    """
    captured: dict = {}
    execute_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("core.database.execute_query", execute_mock)
    await churn_alerts._fetch_candidates(30)
    sql = execute_mock.call_args.args[0]
    assert "NOT COALESCE(ac.stale, FALSE)" in sql
    captured["sql"] = sql
    assert "stale" in captured["sql"]


# ---------------------------------------------------------------------------
# _persist_activities — recovery clears stale (the upsert sets stale=FALSE)
# ---------------------------------------------------------------------------


def test_persist_activities_upsert_clears_stale():
    """The ON CONFLICT UPDATE must set stale=FALSE so a successful fetch after
    an outage clears the flag (recovery). We assert on the SQL string embedded
    in the function source — the upsert is constructed inline, so a source
    inspection is the cheapest regression guard."""
    import inspect

    src = inspect.getsource(activity_monitor._persist_activities)
    assert "stale = FALSE" in src
    # And it must NOT set stale=TRUE (that's mark_all_stale's job).
    assert "stale = TRUE" not in src


# ---------------------------------------------------------------------------
# daily_check — auth-broken path calls mark_all_stale + logs context
# (Integration-style: we drive the ActivityMonitor.daily_check coroutine body
# via its .coro to avoid the 24h loop, with CivInfo + DB + session mocked.)
# ---------------------------------------------------------------------------


class _FakeBotForDailyCheck:
    """Bot stub with an http_session (used by _fetch_mcsrvstat_player_count)."""

    def __init__(self, session):
        self.http_session = session

    async def wait_until_ready(self):
        return None


@pytest.mark.asyncio
async def test_daily_check_marks_stale_when_auth_broken(monkeypatch):
    """When CivInfo auth is broken after the batch, mark_all_stale runs.

    Drives one daily_check cycle with: one citizen (so the early-return guard
    is passed), is_auth_broken()=True, mark_all_stale mocked to return 12, and
    a fake mcsrvstat session returning 102. Asserts mark_all_stale was awaited.
    generate_monthly_report is mocked so the test is date-independent.
    """
    from api import civinfo_api
    from tasks.activity_monitor import ActivityMonitor

    bot = _FakeBotForDailyCheck(
        _FakeSession(_FakeResp(200, {"online": True, "players": {"online": 102}}))
    )
    monitor = ActivityMonitor(bot)

    mark_mock = AsyncMock(return_value=12)
    persist_mock = AsyncMock(return_value=0)
    fetch_mock = AsyncMock(return_value={})
    monthly_mock = AsyncMock()

    # Non-empty citizens list so daily_check doesn't early-return before the
    # is_auth_broken check.
    monkeypatch.setattr(
        "core.database.execute_query",
        AsyncMock(return_value=[{"ign": "Steve", "join_date": None, "settlement": "X"}]),
    )
    monkeypatch.setattr(civinfo_api, "is_auth_broken", lambda: True)
    monkeypatch.setattr(activity_monitor, "mark_all_stale", mark_mock)
    monkeypatch.setattr(activity_monitor, "_persist_activities", persist_mock)
    monkeypatch.setattr(activity_monitor, "_fetch_activities", fetch_mock)
    monkeypatch.setattr(monitor, "generate_monthly_report", monthly_mock)

    await monitor.daily_check.coro(monitor)  # type: ignore[attr-defined]

    mark_mock.assert_awaited_once()
    persist_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_daily_check_does_not_mark_stale_when_auth_ok(monkeypatch):
    """When CivInfo auth is fine, mark_all_stale must NOT run (no false staleness)."""
    from api import civinfo_api
    from tasks.activity_monitor import ActivityMonitor

    bot = _FakeBotForDailyCheck(
        _FakeSession(_FakeResp(200, {"online": True, "players": {"online": 5}}))
    )
    monitor = ActivityMonitor(bot)

    mark_mock = AsyncMock(return_value=99)
    # Non-empty citizens so daily_check reaches the is_auth_broken check.
    monkeypatch.setattr(
        "core.database.execute_query",
        AsyncMock(return_value=[{"ign": "Steve", "join_date": None, "settlement": "X"}]),
    )
    monkeypatch.setattr(civinfo_api, "is_auth_broken", lambda: False)
    monkeypatch.setattr(activity_monitor, "mark_all_stale", mark_mock)
    monkeypatch.setattr(activity_monitor, "_persist_activities", AsyncMock(return_value=0))
    monkeypatch.setattr(activity_monitor, "_fetch_activities", AsyncMock(return_value={}))
    monkeypatch.setattr(monitor, "generate_monthly_report", AsyncMock())

    await monitor.daily_check.coro(monitor)  # type: ignore[attr-defined]

    mark_mock.assert_not_awaited()
