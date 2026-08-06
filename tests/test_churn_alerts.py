"""Tests for tasks/churn_alerts.py — the weekly recruiter churn-nudge pass.

Covers:
  * ``_select_targets`` — the pure cooldown filter (the most regression-prone
    logic: dedup, IGN-membership semantics).
  * ``_build_nudge_embed`` — the recruiter-facing embed field text.
  * ``run_nudge_pass`` — the full flow with patched DB helpers + a fake bot:
    disabled short-circuit, no-candidates, cooldown skip, successful delivery,
    DM failure (audited delivered=false), and per-row exception isolation.
  * ``_send_nudge`` — the DM failure modes (non-numeric ID, NotFound, Forbidden)
    via a fake bot.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import discord
import pytest

from services import audit
from tasks import churn_alerts
from tasks.churn_alerts import ChurnAlertsTask, _build_nudge_embed, _select_targets

# ---------------------------------------------------------------------------
# _select_targets — pure cooldown filter
# ---------------------------------------------------------------------------


def test_select_targets_returns_all_when_cooldown_empty():
    candidates = [
        {"ign": "Steve", "recruiter_discord_id": "111"},
        {"ign": "Alex", "recruiter_discord_id": "222"},
    ]
    assert _select_targets(candidates, set()) == candidates


def test_select_targets_drops_candidates_in_cooldown():
    """A citizen in the cooldown set is skipped for ALL their recruiters."""
    candidates = [
        {"ign": "Steve", "recruiter_discord_id": "111"},
        {"ign": "Steve", "recruiter_discord_id": "999"},  # second recruiter
        {"ign": "Alex", "recruiter_discord_id": "222"},
    ]
    result = _select_targets(candidates, {"Steve"})
    assert len(result) == 1
    assert result[0]["ign"] == "Alex"


def test_select_targets_preserves_multi_recruiter_rows():
    """A citizen NOT in cooldown with 2 recruiters keeps both rows."""
    candidates = [
        {"ign": "Steve", "recruiter_discord_id": "111"},
        {"ign": "Steve", "recruiter_discord_id": "999"},
    ]
    result = _select_targets(candidates, set())
    assert len(result) == 2


def test_select_targets_empty_candidates_returns_empty():
    assert _select_targets([], {"Steve"}) == []


# ---------------------------------------------------------------------------
# _build_nudge_embed — recruiter-facing embed
# ---------------------------------------------------------------------------


def test_build_nudge_embed_has_required_fields():
    last_login = datetime.now(UTC) - timedelta(days=47)
    embed = _build_nudge_embed(
        ign="SteveB", settlement="Lambat City", last_login=last_login, threshold_days=30
    )
    assert isinstance(embed, discord.Embed)
    assert "activity check" in embed.title.lower()
    # The citizen IGN appears in a field value.
    field_values = [f.value for f in embed.fields]
    assert any("SteveB" in v for v in field_values)
    assert any("Lambat City" in v for v in field_values)
    assert any("47 days ago" in v for v in field_values)
    # Footer references the threshold so the recruiter understands the trigger.
    assert embed.footer is not None
    assert "30" in (embed.footer.text or "")


def test_build_nudge_embed_handles_missing_settlement():
    last_login = datetime.now(UTC) - timedelta(days=35)
    embed = _build_nudge_embed(ign="Nomad", settlement="", last_login=last_login, threshold_days=30)
    field_values = [f.value for f in embed.fields]
    assert any("Nomad" in v for v in field_values)
    # Empty settlement renders as an em-dash, not an empty field.
    assert any(v == "—" for v in field_values)


# ---------------------------------------------------------------------------
# Fake bot + helpers for run_nudge_pass tests
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, fail_with: type[Exception] | None = None):
        self._fail_with = fail_with
        self.sent: list[discord.Embed] = []

    async def send(self, embed=None, **kwargs):
        if self._fail_with is not None:
            raise self._fail_with("blocked")
        if embed is not None:
            self.sent.append(embed)


class _FakeBot:
    """Bot stub for run_nudge_pass: fetch_user returns a controllable _FakeUser."""

    def __init__(self, user: _FakeUser):
        self._user = user
        self.is_ws_ratelimited = lambda: False  # guard yields immediately

    async def fetch_user(self, uid: int):
        return self._user

    async def wait_until_ready(self):
        return None


def _candidate(ign, recruiter_id, days_inactive=40, settlement="Lambat City"):
    last_login = datetime.now(UTC) - timedelta(days=days_inactive)
    return {
        "ign": ign,
        "last_login": last_login,
        "settlement": settlement,
        "recruiter_discord_id": recruiter_id,
        "days_inactive": days_inactive,
    }


# ---------------------------------------------------------------------------
# run_nudge_pass — disabled short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_nudge_pass_disabled_is_noop(monkeypatch):
    """CHURN_NUDGES_ENABLED=false → no DB queries, no DMs, no audits."""
    monkeypatch.setattr("core.config.Config.CHURN_NUDGES_ENABLED", False)
    task = ChurnAlertsTask(_FakeBot(_FakeUser()))

    fetch_mock = AsyncMock()
    with (
        patch.object(churn_alerts, "_fetch_candidates", fetch_mock),
        patch.object(churn_alerts, "_fetch_recently_nudged", AsyncMock()),
        patch.object(audit, "emit", AsyncMock()),
    ):
        await task.run_nudge_pass()

    fetch_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# run_nudge_pass — no candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_nudge_pass_no_candidates(monkeypatch):
    monkeypatch.setattr("core.config.Config.CHURN_NUDGES_ENABLED", True)
    monkeypatch.setattr("core.config.Config.CHURN_THRESHOLD_DAYS", 30)
    task = ChurnAlertsTask(_FakeBot(_FakeUser()))

    emit_mock = AsyncMock()
    with (
        patch.object(churn_alerts, "_fetch_candidates", AsyncMock(return_value=[])),
        patch.object(churn_alerts, "_fetch_recently_nudged", AsyncMock(return_value=set())),
        patch.object(audit, "emit", emit_mock),
    ):
        await task.run_nudge_pass()

    emit_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# run_nudge_pass — successful delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_nudge_pass_delivers_and_audits(monkeypatch):
    monkeypatch.setattr("core.config.Config.CHURN_NUDGES_ENABLED", True)
    monkeypatch.setattr("core.config.Config.CHURN_THRESHOLD_DAYS", 30)
    user = _FakeUser()
    task = ChurnAlertsTask(_FakeBot(user))

    candidates = [_candidate("Steve", "111")]
    emit_mock = AsyncMock(return_value=1)
    with (
        patch.object(churn_alerts, "_fetch_candidates", AsyncMock(return_value=candidates)),
        patch.object(churn_alerts, "_fetch_recently_nudged", AsyncMock(return_value=set())),
        patch.object(audit, "emit", emit_mock),
    ):
        await task.run_nudge_pass()

    # One DM sent.
    assert len(user.sent) == 1
    # One audit entry, action=churn.nudge, delivered=true.
    emit_mock.assert_awaited_once()
    args, kwargs = emit_mock.call_args
    assert args[0] == audit.CHURN_NUDGE
    assert kwargs["target_ign"] == "Steve"
    assert kwargs["details"]["delivered"] is True
    assert kwargs["details"]["recruiter"] == "111"
    assert kwargs["details"]["ign"] == "Steve"


# ---------------------------------------------------------------------------
# run_nudge_pass — cooldown skips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_nudge_pass_skips_citizens_in_cooldown(monkeypatch):
    monkeypatch.setattr("core.config.Config.CHURN_NUDGES_ENABLED", True)
    monkeypatch.setattr("core.config.Config.CHURN_THRESHOLD_DAYS", 30)
    user = _FakeUser()
    task = ChurnAlertsTask(_FakeBot(user))

    candidates = [_candidate("Steve", "111"), _candidate("Alex", "222")]
    emit_mock = AsyncMock(return_value=1)
    with (
        patch.object(churn_alerts, "_fetch_candidates", AsyncMock(return_value=candidates)),
        # Steve was nudged recently → in cooldown.
        patch.object(churn_alerts, "_fetch_recently_nudged", AsyncMock(return_value={"Steve"})),
        patch.object(audit, "emit", emit_mock),
    ):
        await task.run_nudge_pass()

    # Only Alex's recruiter gets a DM.
    assert len(user.sent) == 1
    emit_mock.assert_awaited_once()
    assert emit_mock.call_args.kwargs["target_ign"] == "Alex"


# ---------------------------------------------------------------------------
# run_nudge_pass — DM failure is audited with delivered=false
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_nudge_pass_dm_failure_audited_delivered_false(monkeypatch):
    monkeypatch.setattr("core.config.Config.CHURN_NUDGES_ENABLED", True)
    monkeypatch.setattr("core.config.Config.CHURN_THRESHOLD_DAYS", 30)
    # fetch_user's send() raises Forbidden — DMs closed.
    user = _FakeUser(fail_with=discord.Forbidden)
    task = ChurnAlertsTask(_FakeBot(user))

    candidates = [_candidate("Steve", "111")]
    emit_mock = AsyncMock(return_value=1)
    with (
        patch.object(churn_alerts, "_fetch_candidates", AsyncMock(return_value=candidates)),
        patch.object(churn_alerts, "_fetch_recently_nudged", AsyncMock(return_value=set())),
        patch.object(audit, "emit", emit_mock),
    ):
        await task.run_nudge_pass()

    # No DM delivered, but the attempt IS audited (delivered=false).
    assert len(user.sent) == 0
    emit_mock.assert_awaited_once()
    assert emit_mock.call_args.kwargs["details"]["delivered"] is False


# ---------------------------------------------------------------------------
# run_nudge_pass — one bad row doesn't abort the batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_nudge_pass_continues_after_per_row_exception(monkeypatch):
    """An exception in one nudge must not skip the rest of the batch."""
    monkeypatch.setattr("core.config.Config.CHURN_NUDGES_ENABLED", True)
    monkeypatch.setattr("core.config.Config.CHURN_THRESHOLD_DAYS", 30)
    user = _FakeUser()
    task = ChurnAlertsTask(_FakeBot(user))

    candidates = [_candidate("Steve", "111"), _candidate("Alex", "222")]
    emit_mock = AsyncMock(return_value=1)
    send_calls = []

    async def _flaky_send_nudge(bot, **kwargs):
        send_calls.append(kwargs["ign"])
        if kwargs["ign"] == "Steve":
            raise RuntimeError("transient boom")
        return True

    with (
        patch.object(churn_alerts, "_fetch_candidates", AsyncMock(return_value=candidates)),
        patch.object(churn_alerts, "_fetch_recently_nudged", AsyncMock(return_value=set())),
        patch.object(churn_alerts, "_send_nudge", side_effect=_flaky_send_nudge),
        patch.object(audit, "emit", emit_mock),
    ):
        await task.run_nudge_pass()  # must not raise

    # Both rows were attempted (the exception was isolated).
    assert send_calls == ["Steve", "Alex"]
    # The successful row (Alex) was audited; Steve's exception path also
    # increments the failed counter but does NOT emit (emit is only called
    # after _send_nudge returns). So exactly one emit call for Alex.
    emit_mock.assert_awaited_once()
    assert emit_mock.call_args.kwargs["target_ign"] == "Alex"


# ---------------------------------------------------------------------------
# _send_nudge — DM failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_nudge_non_numeric_recruiter_id_returns_false():
    bot = _FakeBot(_FakeUser())
    ok = await churn_alerts._send_nudge(
        bot,
        ign="Steve",
        settlement="X",
        last_login=datetime.now(UTC) - timedelta(days=40),
        recruiter_discord_id="not-a-number",
        threshold_days=30,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_send_nudge_user_not_found_returns_false():
    class _Bot404(_FakeBot):
        async def fetch_user(self, uid):
            raise discord.NotFound(
                __import__("discord").HTTPException.__new__(discord.HTTPException),
                "not found",
            )

    bot = _Bot404(_FakeUser())
    ok = await churn_alerts._send_nudge(
        bot,
        ign="Steve",
        settlement="X",
        last_login=datetime.now(UTC) - timedelta(days=40),
        recruiter_discord_id="111",
        threshold_days=30,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_send_nudge_forbidden_returns_false():
    class _BotForbidden(_FakeBot):
        async def fetch_user(self, uid):
            raise discord.Forbidden(
                __import__("discord").HTTPException.__new__(discord.HTTPException),
                "forbidden",
            )

    bot = _BotForbidden(_FakeUser())
    ok = await churn_alerts._send_nudge(
        bot,
        ign="Steve",
        settlement="X",
        last_login=datetime.now(UTC) - timedelta(days=40),
        recruiter_discord_id="111",
        threshold_days=30,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# _fetch_recently_nudged — JSONB-filter shape sanity (uses a fake db.execute_query)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_recently_nudged_builds_set_from_rows(monkeypatch):
    """The cooldown set is built from the target_ign column of audit rows."""
    rows = [
        {"target_ign": "Steve"},
        {"target_ign": "Alex"},
        {"target_ign": "Steve"},  # DISTINCT collapses dupes server-side; we also dedupe
    ]
    execute_mock = AsyncMock(return_value=rows)
    monkeypatch.setattr("core.database.execute_query", execute_mock)

    result = await churn_alerts._fetch_recently_nudged(14)

    assert result == {"Steve", "Alex"}
    # The query must filter on the churn.nudge action (bound as $1) + the
    # delivered JSONB key. The action literal is in the params tuple, not the SQL.
    sql = execute_mock.call_args.args[0]
    params = execute_mock.call_args.args[1]
    assert "action = $1" in sql
    assert "delivered" in sql
    assert audit.CHURN_NUDGE in params


@pytest.mark.asyncio
async def test_fetch_recently_nudged_empty_returns_empty_set(monkeypatch):
    monkeypatch.setattr("core.database.execute_query", AsyncMock(return_value=None))
    assert await churn_alerts._fetch_recently_nudged(14) == set()


# ---------------------------------------------------------------------------
# Lifecycle — stop() is safe on a never-started task.
# ---------------------------------------------------------------------------


def test_stop_is_safe_without_start():
    task = ChurnAlertsTask(_FakeBot(_FakeUser()))
    task.stop()  # must not raise
    assert task.weekly_nudge.is_running() is False
