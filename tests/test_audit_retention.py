"""Tests for audit-log retention (ROADMAP §6.2).

Covers:
  * ``services.audit.prune_older_than`` — the DELETE/parse-count path, using a
    fake asyncpg pool so no live Postgres is needed.
  * ``tasks.audit_retention.AuditRetentionTask`` — the no-op-when-disabled gate,
    the run-when-enabled path (monkeypatched to call a fake prune + capture the
    self-audit emit), and the start/stop lifecycle.

The fake pool mirrors the shape the real ``core.database.get_pool`` returns: an
object with ``acquire()`` (async context manager) yielding a connection with an
``execute(sql, *params)`` coroutine that returns a command-status string.
"""

from unittest.mock import AsyncMock, patch

import pytest

from services import audit
from tasks.audit_retention import AuditRetentionTask

# ---------------------------------------------------------------------------
# Fake asyncpg pool + connection
# ---------------------------------------------------------------------------


class _FakeConn:
    """Records the SQL + params passed to execute(); returns a canned status."""

    def __init__(self, status: str = "DELETE 0"):
        self.status = status
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *params):
        self.calls.append((sql, params))
        return self.status


class _FakePoolAcquire:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        return _FakePoolAcquire(self._conn)


# ---------------------------------------------------------------------------
# prune_older_than — the SQL helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_older_than_returns_zero_when_disabled():
    """days <= 0 must short-circuit without touching the DB."""
    with patch("core.database.get_pool", new=AsyncMock()) as mock_get_pool:
        result = await audit.prune_older_than(0)
    assert result == 0
    # The pool must never have been acquired — the guard ran first.
    mock_get_pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_older_than_negative_also_noop():
    """Defensive: a negative window (misconfig) is treated as disabled, not crash."""
    with patch("core.database.get_pool", new=AsyncMock()) as mock_get_pool:
        result = await audit.prune_older_than(-5)
    assert result == 0
    mock_get_pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_older_than_parses_delete_count():
    """A 'DELETE N' status string is parsed into the integer row count."""
    conn = _FakeConn(status="DELETE 42")
    fake_pool = _FakePool(conn)

    with patch("core.database.get_pool", new=AsyncMock(return_value=fake_pool)):
        result = await audit.prune_older_than(30)

    assert result == 42
    # Exactly one DELETE, with the days param bound positionally.
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "DELETE FROM audit_log" in sql
    assert "INTERVAL '1 day'" in sql
    assert params == (30,)


@pytest.mark.asyncio
async def test_prune_older_than_zero_rows_deleted():
    """'DELETE 0' (nothing old enough to prune) parses to 0 — not an error."""
    conn = _FakeConn(status="DELETE 0")
    fake_pool = _FakePool(conn)
    with patch("core.database.get_pool", new=AsyncMock(return_value=fake_pool)):
        result = await audit.prune_older_than(730)
    assert result == 0


@pytest.mark.asyncio
async def test_prune_older_than_unparseable_status_returns_zero():
    """A malformed status string logs + returns 0 rather than crashing.

    asyncpg's contract is 'DELETE <n>' but a defensive parse protects against
    a future driver variant or a RETURNING-based rewrite.
    """
    conn = _FakeConn(status="weird-status")
    fake_pool = _FakePool(conn)
    with patch("core.database.get_pool", new=AsyncMock(return_value=fake_pool)):
        result = await audit.prune_older_than(30)
    assert result == 0


# ---------------------------------------------------------------------------
# AuditRetentionTask — the nightly loop
# ---------------------------------------------------------------------------


class _FakeBot:
    """Minimal bot stub: wait_until_ready is a no-op so the loop body runs."""

    async def wait_until_ready(self):
        return None


@pytest.mark.asyncio
async def test_task_skips_when_retention_disabled(monkeypatch):
    """AUDIT_RETENTION_DAYS <= 0 → no prune, no audit emit, just a debug log."""
    monkeypatch.setattr("core.config.Config.AUDIT_RETENTION_DAYS", 0)
    task = AuditRetentionTask(_FakeBot())

    prune_mock = AsyncMock(return_value=999)
    emit_mock = AsyncMock(return_value=1)
    with (
        patch.object(audit, "prune_older_than", prune_mock),
        patch.object(audit, "emit", emit_mock),
    ):
        # Drive one cycle via the extracted helper (no discord.py loop needed).
        await task._run_prune()

    prune_mock.assert_not_awaited()
    emit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_prunes_and_audits_when_enabled(monkeypatch):
    """Enabled + rows deleted → prune runs once + a single audit.prune emit."""
    monkeypatch.setattr("core.config.Config.AUDIT_RETENTION_DAYS", 365)
    task = AuditRetentionTask(_FakeBot())

    prune_mock = AsyncMock(return_value=17)
    emit_mock = AsyncMock(return_value=1)
    with (
        patch.object(audit, "prune_older_than", prune_mock),
        patch.object(audit, "emit", emit_mock),
    ):
        await task._run_prune()

    prune_mock.assert_awaited_once_with(365)
    emit_mock.assert_awaited_once()
    # The self-audit entry must use the AUDIT_PRUNE action + record the count.
    args, kwargs = emit_mock.call_args
    assert args[0] == audit.AUDIT_PRUNE
    assert kwargs["actor_discord_id"] is None
    assert kwargs["details"]["rows_deleted"] == 17
    assert kwargs["details"]["retention_days"] == 365


@pytest.mark.asyncio
async def test_task_no_emit_when_zero_rows_deleted(monkeypatch):
    """A prune that removes nothing is not audited (no noise in /audit search)."""
    monkeypatch.setattr("core.config.Config.AUDIT_RETENTION_DAYS", 30)
    task = AuditRetentionTask(_FakeBot())

    prune_mock = AsyncMock(return_value=0)
    emit_mock = AsyncMock(return_value=1)
    with (
        patch.object(audit, "prune_older_than", prune_mock),
        patch.object(audit, "emit", emit_mock),
    ):
        await task._run_prune()

    prune_mock.assert_awaited_once_with(30)
    emit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_swallows_prune_exception(monkeypatch):
    """A prune failure is logged but never crashes the loop (it'd kill the task).

    Matches the pattern in the other background tasks (uptime_monitor,
    activity_monitor) — the top-level try/except keeps the loop alive so a
    transient DB blip doesn't permanently disable retention.
    """
    monkeypatch.setattr("core.config.Config.AUDIT_RETENTION_DAYS", 90)
    task = AuditRetentionTask(_FakeBot())

    prune_mock = AsyncMock(side_effect=RuntimeError("db down"))
    emit_mock = AsyncMock(return_value=1)
    with (
        patch.object(audit, "prune_older_than", prune_mock),
        patch.object(audit, "emit", emit_mock),
    ):
        # Must not raise.
        await task._run_prune()

    prune_mock.assert_awaited_once()
    # emit must NOT have run — the prune failed before the emit step.
    emit_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Lifecycle — start/stop are idempotent and don't crash on a fresh task.
# ---------------------------------------------------------------------------


def test_start_and_stop_are_safe_without_event_loop():
    """start()/stop() on a task whose loop was never started must not raise.

    The real bot calls stop() in close() for every task unconditionally; if
    start() failed earlier (e.g. setup_hook exception), stop() must still be
    a no-op rather than crashing the shutdown path.
    """
    task = AuditRetentionTask(_FakeBot())
    # Neither has started the underlying discord.py tasks.loop; both no-op.
    task.stop()
    # We don't call start() here because it needs a running event loop + the
    # discord.py loop machinery; that's exercised by the integration/E2E run.
    assert task.nightly_prune.is_running() is False


# ---------------------------------------------------------------------------
# before_loop scheduling — target time is 03:30 UTC, rolls to tomorrow if past.
# We don't run the real before_loop (it sleeps until 03:30); we just assert the
# hour/minute constants are what the docstring promises, so a future edit can't
# silently collide with the 02:00 daily_backup/daily_check jobs.
# ---------------------------------------------------------------------------


def test_prune_schedule_offset_from_other_nightly_jobs():
    """The prune must NOT run at 02:00 UTC (collides with backup + daily_check)."""
    from tasks.audit_retention import _PRUNE_HOUR, _PRUNE_MINUTE

    assert _PRUNE_HOUR != 2 or _PRUNE_MINUTE != 0, (
        "Audit prune must not run at 02:00 UTC — it collides with daily_backup "
        "and daily_check. Pick a different offset (currently 03:30)."
    )
    assert 0 <= _PRUNE_HOUR <= 23
    assert 0 <= _PRUNE_MINUTE <= 59
