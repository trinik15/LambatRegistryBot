"""Prometheus metrics (Phase 1.5).

All metrics use the default ``prometheus_client`` registry. They are populated
either:

* continuously — by the code that owns the value (e.g. the CivInfo cache-hit
  counter is incremented inside ``civinfo_api.CivInfoCache.get``);
* or on scrape — by :func:`collect_metrics`, which is called from the
  ``/metrics`` HTTP handler and reads the current DB + in-memory state into
  the gauges before ``generate_latest()`` serialises them.

On-scrape collection keeps the gauges honest (no stale in-memory counters for
citizen counts) at the cost of one cheap DB query per scrape — fine for the
low-traffic Prometheus poll cadence (15–60s).
"""

import contextlib
import logging
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
)

logger = logging.getLogger(__name__)

# --- Process / liveness -----------------------------------------------------
BOT_UP = Gauge("lambat_bot_up", "1 if the bot process is running.")
DISCORD_GATEWAY_READY = Gauge(
    "lambat_discord_gateway_ready",
    "1 if the Discord gateway is connected.",
)
DATABASE_REACHABLE = Gauge(
    "lambat_database_reachable",
    "1 if SELECT 1 answered within the health timeout.",
)

# --- Registry domain metrics ------------------------------------------------
CITIZENS_TOTAL = Gauge("lambat_citizens_total", "Total registered citizens in the registry.")
ACTIVE_CITIZENS = Gauge(
    "lambat_active_citizens",
    "Citizens who logged into CivMC within the last 30 days (approx, from activity_cache).",
)
SETTLEMENTS_TOTAL = Gauge("lambat_settlements_total", "Total registered settlements.")

# --- CivInfo integration ----------------------------------------------------
CIVINFO_CACHE_HITS = Counter(
    "lambat_civinfo_cache_hits_total",
    "CivInfo lookups served from the in-memory cache (avoids an API call).",
)
CIVINFO_AUTH_BROKEN = Gauge(
    "lambat_civinfo_auth_broken",
    "1 if CivInfo API auth is currently broken (key missing or rejected).",
)

# --- CivMC server / uptime --------------------------------------------------
CIVMC_ONLINE = Gauge("lambat_civmc_online", "1 if the CivMC server was last seen online.")
LAST_OUTAGE_DURATION_SECONDS = Gauge(
    "lambat_last_outage_duration_seconds",
    "Duration of the most recent CivMC outage, in seconds (0 if none recorded).",
)


def record_cache_hit():
    """Incremented by civinfo_api when a lookup is served from cache.

    Kept behind a function so callers don't need to know the metric object.
    Lazy-safe: prometheus_client handles this from any thread.
    """
    CIVINFO_CACHE_HITS.inc()


async def _count_rows(query: str, bot: Any) -> int | None:
    """Run a ``SELECT COUNT(*)`` and return the int, or None on failure."""
    try:
        from core import database as db

        pool = await db.get_pool()
        if pool is None:
            return None
        async with pool.acquire() as conn:
            value = await conn.fetchval(query)
            return int(value) if value is not None else None
    except Exception as e:  # noqa: BLE001 — a metrics scrape must never crash the bot
        logger.debug(f"metrics query failed ({query!r}): {e}")
        return None


async def collect_metrics(bot: Any):
    """Populate the scrape-time gauges from the bot's live state.

    Called from the ``/metrics`` HTTP handler thread (via
    ``run_coroutine_threadsafe``) before ``generate_latest()``. Individual
    failures degrade to a sentinel value rather than raising, so a partial
    outage doesn't make /metrics return 500.
    """
    BOT_UP.set(1)

    # Gateway liveness.
    try:
        gateway_ok = bool(bot.is_ready()) and not bool(bot.is_closed())
    except Exception:  # noqa: BLE001
        gateway_ok = False
    DISCORD_GATEWAY_READY.set(1 if gateway_ok else 0)

    # DB reachability + domain counts (all cheap COUNT(*)s).
    db_ok = False
    citizens = await _count_rows("SELECT COUNT(*) FROM citizens", bot)
    if citizens is not None:
        db_ok = True
        CITIZENS_TOTAL.set(citizens)
    else:
        CITIZENS_TOTAL.set(-1)  # sentinel: scrape failed, don't pretend it's 0

    settlements = await _count_rows("SELECT COUNT(*) FROM settlements", bot)
    SETTLEMENTS_TOTAL.set(settlements if settlements is not None else -1)

    active = await _count_rows(
        "SELECT COUNT(*) FROM activity_cache WHERE last_login >= NOW() - INTERVAL '30 days'",
        bot,
    )
    ACTIVE_CITIZENS.set(active if active is not None else -1)

    DATABASE_REACHABLE.set(1 if db_ok else 0)

    # CivInfo auth state.
    try:
        from api import civinfo_api

        CIVINFO_AUTH_BROKEN.set(1 if civinfo_api.is_auth_broken() else 0)
    except Exception:  # noqa: BLE001
        CIVINFO_AUTH_BROKEN.set(0)

    # Uptime monitor state (if present).
    uptime_monitor = getattr(bot, "uptime_monitor", None)
    if uptime_monitor is not None:
        with contextlib.suppress(Exception):  # noqa: BLE001 — metrics must never crash
            CIVMC_ONLINE.set(1 if getattr(uptime_monitor, "last_online", True) else 0)
        with contextlib.suppress(Exception):  # noqa: BLE001
            LAST_OUTAGE_DURATION_SECONDS.set(
                float(getattr(uptime_monitor, "last_outage_duration_seconds", 0.0))
            )


def render() -> tuple[bytes, str]:
    """Serialise all registered metrics to Prometheus text format.

    Returns (body_bytes, content_type). Callers should call
    :func:`collect_metrics` first so the scrape-time gauges are fresh.
    """
    return generate_latest(), CONTENT_TYPE_LATEST
