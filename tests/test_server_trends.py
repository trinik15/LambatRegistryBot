"""Tests for /server trends + render_server_trends — Phase B (WS-5)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.charts as charts
from api import civinfo_api


@pytest.fixture(autouse=True)
def reset_civinfo_auth_state():
    """Reset auth-broken + server-status cache before each test.

    The 403 test marks auth-broken (10-min TTL); without a reset, subsequent
    tests would short-circuit and never hit the mocked HTTP layer.
    """
    civinfo_api._auth_broken_until = 0.0
    civinfo_api._auth_warned = False
    civinfo_api._server_status_cache.clear()
    yield
    civinfo_api._auth_broken_until = 0.0
    civinfo_api._auth_warned = False
    civinfo_api._server_status_cache.clear()


class TestRenderServerTrends:
    """render_server_trends renders a PNG from (timestamp, player_count) pairs."""

    def test_empty_data_returns_none(self):
        assert charts.render_server_trends("Test", []) is None
        assert charts.render_server_trends("Test", None) is None

    def test_single_point_renders(self):
        """A single data point should still render (flat line)."""
        data = [(datetime.now(UTC), 5)]
        png = charts.render_server_trends("Single", data, "day")
        assert png is not None
        assert len(png) > 100

    def test_multiple_points_renders(self):
        """Multiple points render a full chart with peak/low annotations."""
        now = datetime.now(UTC)
        data = [
            (now - timedelta(hours=2), 10),
            (now - timedelta(hours=1), 25),
            (now, 5),
        ]
        png = charts.render_server_trends("24h", data, "day")
        assert png is not None
        assert len(png) > 500

    def test_minute_period_renders(self):
        now = datetime.now(UTC)
        data = [(now - timedelta(minutes=i), 10 + i) for i in range(5, 0, -1)]
        png = charts.render_server_trends("Last minute", data, "minute")
        assert png is not None

    def test_hour_period_renders(self):
        now = datetime.now(UTC)
        data = [(now - timedelta(hours=i), 15 - i) for i in range(5, 0, -1)]
        png = charts.render_server_trends("Last hour", data, "hour")
        assert png is not None


class TestGetServerStatusHistory:
    """get_server_status_history fetches + parses the mc-server-status endpoint."""

    @pytest.mark.asyncio
    async def test_success_parses_parallel_arrays(self):
        """A 200 with timestamps + playerCounts returns sorted (datetime, int) pairs."""
        # Reset the module-level cache before the test.
        civinfo_api._server_status_cache.clear()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "timestamps": [1717200000000, 1717203600000, 1717207200000],
                "playerCounts": [10, 25, 5],
            }
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        with (
            patch("core.config.Config.CIVINFO_API_KEY", "fake-key"),
            patch("core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"),
            patch("core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"),
            patch("core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"),
        ):
            result = await civinfo_api.get_server_status_history("day", session)

        assert len(result) == 3
        assert result[0] == (datetime(2024, 6, 1, 0, 0, tzinfo=UTC), 10)
        assert result[1] == (datetime(2024, 6, 1, 1, 0, tzinfo=UTC), 25)
        assert result[2] == (datetime(2024, 6, 1, 2, 0, tzinfo=UTC), 5)
        # Already sorted ascending (timestamps were given in order).
        assert result == sorted(result, key=lambda x: x[0])

    @pytest.mark.asyncio
    async def test_sorts_unordered_timestamps(self):
        """The result is sorted ascending by timestamp, regardless of input order."""
        civinfo_api._server_status_cache.clear()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "timestamps": [1717207200000, 1717200000000, 1717203600000],
                "playerCounts": [5, 10, 25],
            }
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        with (
            patch("core.config.Config.CIVINFO_API_KEY", "fake-key"),
            patch("core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"),
            patch("core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"),
            patch("core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"),
        ):
            result = await civinfo_api.get_server_status_history("day", session)

        assert result[0][1] == 10  # earliest timestamp → 10 players
        assert result[1][1] == 25
        assert result[2][1] == 5

    @pytest.mark.asyncio
    async def test_filters_invalid_entries(self):
        """None/invalid timestamps or counts are silently dropped."""
        civinfo_api._server_status_cache.clear()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "timestamps": [1717200000000, None, "bad", 1717203600000],
                "playerCounts": [10, 5, 15, None],
            }
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        with (
            patch("core.config.Config.CIVINFO_API_KEY", "fake-key"),
            patch("core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"),
            patch("core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"),
            patch("core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"),
        ):
            result = await civinfo_api.get_server_status_history("day", session)

        # Only the first entry (valid ts + valid count) survives.
        assert len(result) == 1
        assert result[0] == (datetime(2024, 6, 1, 0, 0, tzinfo=UTC), 10)

    @pytest.mark.asyncio
    async def test_403_returns_empty_list(self):
        """A 403 response marks auth-broken and returns []."""
        civinfo_api._server_status_cache.clear()

        mock_resp = AsyncMock()
        mock_resp.status = 403
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        with (
            patch("core.config.Config.CIVINFO_API_KEY", "fake-key"),
            patch("core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"),
            patch("core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"),
            patch("core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"),
        ):
            result = await civinfo_api.get_server_status_history("day", session)

        assert result == []
        assert civinfo_api.is_auth_broken() is True

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_refetch(self):
        """A second call within the TTL doesn't re-hit the API."""
        civinfo_api._server_status_cache.clear()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={"timestamps": [1717200000000], "playerCounts": [10]}
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        with (
            patch("core.config.Config.CIVINFO_API_KEY", "fake-key"),
            patch("core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"),
            patch("core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"),
            patch("core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"),
        ):
            first = await civinfo_api.get_server_status_history("day", session)
            assert session.get.call_count == 1
            second = await civinfo_api.get_server_status_history("day", session)
            # Still 1 — the second call was served from cache.
            assert session.get.call_count == 1
            assert first == second

    @pytest.mark.asyncio
    async def test_invalid_period_defaults_to_day(self):
        """An invalid period value defaults to 'day'."""
        civinfo_api._server_status_cache.clear()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={"timestamps": [1717200000000], "playerCounts": [10]}
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        with (
            patch("core.config.Config.CIVINFO_API_KEY", "fake-key"),
            patch("core.config.Config.CIVINFO_API_BASE", "https://api.civinfo.net"),
            patch("core.config.Config.CIVINFO_MC_SERVER", "play.civmc.net"),
            patch("core.config.Config.CIVINFO_FRONTEND_VERSION", "abc123"),
        ):
            await civinfo_api.get_server_status_history("week", session)

        # The URL should contain "day", not "week".
        call_url = session.get.call_args.args[0]
        assert "/day" in call_url
