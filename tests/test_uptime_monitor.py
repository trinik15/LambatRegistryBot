"""Tests for tasks/uptime_monitor.py — the edge-triggered outage state machine.

CivMC relevance: the monitor must NOT alert on a single transient mcsrvstat
hiccup, but MUST alert once an outage is confirmed (2 consecutive failures),
and MUST post a recovery alert (with duration) on online->offline->online.

We drive the state machine by calling ``_poll()`` directly with a patched
``_fetch_online`` and a captured ``_send_alert``, without starting the
discord.py tasks.loop (which would need a real gateway).
"""

from datetime import UTC, datetime, timedelta

import pytest

from tasks.uptime_monitor import OUTAGE_THRESHOLD, UptimeMonitor


class _FakeBot:
    """Minimal bot stand-in: UptimeMonitor only needs http_session (which we
    patch out via _fetch_online) and a get_channel (patched out via _send_alert).
    """

    http_session = None


def _make_monitor(monkeypatch):
    """Build a UptimeMonitor with _send_alert captured into a list.

    ``_fetch_online`` is left unpatched here; individual tests patch the
    monitor instance's method to return the desired poll result.
    """
    bot = _FakeBot()
    monitor = UptimeMonitor(bot)
    sent = []
    monkeypatch.setattr(monitor, "_send_alert", _make_send_alert_capture(sent))
    return monitor, sent


def _make_send_alert_capture(sent_list):
    async def _send_alert(embed, content=""):
        sent_list.append({"embed": embed, "content": content})

    return _send_alert


async def _poll_with(monitor, value):
    """Patch _fetch_online to return ``value`` then run one _poll cycle."""

    async def _fake_fetch():
        return value

    monitor._fetch_online = _fake_fetch
    await monitor._poll()


# ---------------------------------------------------------------------------
# Fresh monitor defaults.
# ---------------------------------------------------------------------------


def test_fresh_monitor_assumes_online():
    """A brand-new monitor assumes the server is up (no false startup alert)."""
    monkeypatch = pytest.MonkeyPatch()
    monitor, _ = _make_monitor(monkeypatch)
    assert monitor.last_online is True
    assert monitor.fail_count == 0
    assert monitor.outage_start is None
    assert monitor.alerted_outage is False
    monkeypatch.undo()


def test_outage_threshold_is_two():
    assert OUTAGE_THRESHOLD == 2


# ---------------------------------------------------------------------------
# Inconclusive polls (mcsrvstat itself unreachable) must never change state.
# ---------------------------------------------------------------------------


async def test_inconclusive_poll_does_not_alert(monkeypatch):
    monitor, sent = _make_monitor(monkeypatch)
    await _poll_with(monitor, None)  # None = couldn't tell
    assert sent == []
    assert monitor.last_online is True
    assert monitor.fail_count == 0
    assert monitor.alerted_outage is False


# ---------------------------------------------------------------------------
# Online -> online: no recovery alert (already online).
# ---------------------------------------------------------------------------


async def test_online_poll_when_already_online_no_alert(monkeypatch):
    monitor, sent = _make_monitor(monkeypatch)
    await _poll_with(monitor, True)
    assert sent == []
    assert monitor.last_online is True


# ---------------------------------------------------------------------------
# Offline detection: needs OUTAGE_THRESHOLD (2) consecutive failures.
# ---------------------------------------------------------------------------


async def test_single_offline_poll_does_not_alert(monkeypatch):
    """One offline poll is NOT enough — avoids false alarms from a mcsrvstat hiccup."""
    monitor, sent = _make_monitor(monkeypatch)
    await _poll_with(monitor, False)
    assert sent == []
    assert monitor.alerted_outage is False
    assert monitor.fail_count == 1
    assert monitor.last_online is True  # not yet flipped


async def test_two_offline_polls_declare_outage(monkeypatch):
    monitor, sent = _make_monitor(monkeypatch)
    await _poll_with(monitor, False)
    await _poll_with(monitor, False)
    assert len(sent) == 1
    assert (
        "appears to be down" in sent[0]["embed"].title.lower()
        or "down" in sent[0]["content"].lower()
    )
    assert monitor.alerted_outage is True
    assert monitor.last_online is False
    assert monitor.outage_start is not None


async def test_third_offline_poll_does_not_re_alert(monkeypatch):
    """Once alerted, subsequent offline polls stay quiet (edge-triggered)."""
    monitor, sent = _make_monitor(monkeypatch)
    await _poll_with(monitor, False)
    await _poll_with(monitor, False)
    await _poll_with(monitor, False)
    assert len(sent) == 1
    assert monitor.alerted_outage is True


# ---------------------------------------------------------------------------
# Recovery: offline -> online posts a recovery alert with duration.
# ---------------------------------------------------------------------------


async def test_recovery_after_outage_posts_duration(monkeypatch):
    monitor, sent = _make_monitor(monkeypatch)
    # Force a confirmed outage with a known start time 45 minutes ago.
    await _poll_with(monitor, False)
    await _poll_with(monitor, False)
    monitor.outage_start = datetime.now(UTC) - timedelta(minutes=45)

    await _poll_with(monitor, True)

    assert len(sent) == 2  # outage alert + recovery alert
    recovery = sent[1]
    assert "recover" in recovery["embed"].title.lower()
    # The duration string should mention minutes.
    assert "45m" in recovery["embed"].description
    # State fully reset.
    assert monitor.last_online is True
    assert monitor.alerted_outage is False
    assert monitor.outage_start is None
    assert monitor.fail_count == 0


async def test_recovery_records_last_outage_duration(monkeypatch):
    """Phase 1.5: the monitor stores the last outage duration for /metrics."""
    monitor, _ = _make_monitor(monkeypatch)
    await _poll_with(monitor, False)
    await _poll_with(monitor, False)
    monitor.outage_start = datetime.now(UTC) - timedelta(minutes=10)
    await _poll_with(monitor, True)
    assert hasattr(monitor, "last_outage_duration_seconds")
    assert monitor.last_outage_duration_seconds >= 590  # ~10 minutes


# ---------------------------------------------------------------------------
# Inconclusive poll during an outage must not reset the outage.
# ---------------------------------------------------------------------------


async def test_inconclusive_poll_during_outage_keeps_outage(monkeypatch):
    monitor, sent = _make_monitor(monkeypatch)
    await _poll_with(monitor, False)
    await _poll_with(monitor, False)
    assert monitor.alerted_outage is True
    start = monitor.outage_start

    await _poll_with(monitor, None)  # mcsrvstat blip during the outage

    assert monitor.alerted_outage is True
    assert monitor.outage_start == start
    assert len(sent) == 1  # no new alert
