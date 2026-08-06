"""Tests for services/role_manager.rate_limit_guard (Phase 4.3).

The guard backs off when ``bot.is_ws_ratelimited()`` returns True, so the
weekly role_sync loop (and future bulk callers) don't trip Discord 429s.
"""

import asyncio

import pytest

from services.role_manager import rate_limit_guard


class _FakeBot:
    """Minimal bot stub with a controllable is_ws_ratelimited()."""

    def __init__(self, rate_limited: bool):
        self._rate_limited = rate_limited
        self.call_count = 0

    def is_ws_ratelimited(self) -> bool:
        self.call_count += 1
        return self._rate_limited


class _FakeBotClearsAfter:
    """Bot that reports rate-limited N times, then clears."""

    def __init__(self, limited_times: int):
        self._remaining = limited_times
        self.call_count = 0

    def is_ws_ratelimited(self) -> bool:
        self.call_count += 1
        if self._remaining > 0:
            self._remaining -= 1
            return True
        return False


class _FakeBotNoMethod:
    """Bot stub without is_ws_ratelimited (defensive — should not crash)."""

    pass


# ---------------------------------------------------------------------------
# Happy path — gateway clear, guard yields immediately.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_yields_immediately_when_not_rate_limited():
    bot = _FakeBot(rate_limited=False)
    async with rate_limit_guard(bot, poll=0.01):
        pass
    # The guard checks at least once before yielding.
    assert bot.call_count >= 1


# ---------------------------------------------------------------------------
# Back-off path — gateway rate-limited, then clears.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_waits_then_yields_when_rate_limit_clears():
    bot = _FakeBotClearsAfter(limited_times=3)
    async with rate_limit_guard(bot, max_wait=10.0, poll=0.01):
        pass
    # Should have polled 4 times: 3 rate-limited + 1 clear.
    assert bot.call_count == 4


# ---------------------------------------------------------------------------
# Max-wait path — gateway stays rate-limited forever; guard proceeds anyway
# after max_wait so the role op isn't stalled indefinitely.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_proceeds_after_max_wait():
    bot = _FakeBot(rate_limited=True)
    # max_wait=0.05 + poll=0.02 → ~3 polls before giving up.
    async with rate_limit_guard(bot, max_wait=0.05, poll=0.02):
        pass
    assert bot.call_count >= 2


# ---------------------------------------------------------------------------
# Defensive path — bot without is_ws_ratelimited (e.g. a test mock) doesn't
# crash; the guard yields immediately.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_tolerates_bot_without_is_ws_ratelimited():
    bot = _FakeBotNoMethod()
    # Should not raise AttributeError.
    async with rate_limit_guard(bot, poll=0.01):  # type: ignore[arg-type]
        pass


# ---------------------------------------------------------------------------
# Concurrency — the guard doesn't hold any lock, so two guards can run
# concurrently (relevant for the role_sync loop which is sequential but
# could be parallelised in future).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_does_not_block_concurrent_use():
    bot = _FakeBot(rate_limited=False)

    async def _use_guard():
        async with rate_limit_guard(bot, poll=0.01):
            await asyncio.sleep(0.01)

    await asyncio.gather(_use_guard(), _use_guard(), _use_guard())
