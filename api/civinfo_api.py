import aiohttp
import asyncio
from datetime import datetime
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

CIVINFO_URL = "https://api.civinfo.net/mc-sessions/all"

# How long to remember each kind of result.
#   success      -> 5 min  (the data only changes when the player logs in)
#   not_found    -> 2 min  (player may have just joined; retry sooner)
#   error        -> 60 sec (transient outage / rate limit; don't poison the
#                            cache for the full 5 minutes)
CACHE_TTL_SUCCESS = 300
CACHE_TTL_NOT_FOUND = 120
CACHE_TTL_ERROR = 60


class CivInfoCache:
    def __init__(self, ttl_seconds=CACHE_TTL_SUCCESS):
        self.cache = {}
        self.ttl = ttl_seconds

    def get(self, ign: str) -> Optional[Tuple[str, str, Optional[datetime], str]]:
        """Return (status, emoji, last_login, status_text) if cached and fresh."""
        if ign in self.cache:
            data, timestamp, ttl = self.cache[ign]
            if datetime.now().timestamp() - timestamp < ttl:
                return data
            else:
                del self.cache[ign]
        return None

    def set(self, ign: str, data: Tuple[str, str, Optional[datetime], str], ttl: Optional[int] = None):
        """Store (status, emoji, last_login, status_text) with current timestamp.

        ``ttl`` overrides the default TTL — used to keep error / not_found
        results around for a shorter window so a transient CivInfo outage
        doesn't blacklist a player for the full success TTL.
        """
        actual_ttl = ttl if ttl is not None else self.ttl
        self.cache[ign] = (data, datetime.now().timestamp(), actual_ttl)

    def clear(self):
        """Drop every cached entry (e.g. after a database restore)."""
        self.cache.clear()

    def invalidate(self, ign: str):
        """Drop a single cached entry so the next lookup hits the API fresh."""
        self.cache.pop(ign, None)

cache = CivInfoCache()


def _ttl_for(status: str) -> int:
    """Pick the cache TTL appropriate to a result status."""
    if status == "ok":
        return CACHE_TTL_SUCCESS
    if status == "not_found":
        return CACHE_TTL_NOT_FOUND
    return CACHE_TTL_ERROR

async def get_player_activity(ign: str, session: aiohttp.ClientSession) -> Tuple[str, str, Optional[datetime], str]:
    """
    Return a tuple (status, emoji, last_login, status_text)
    status is one of: "ok", "not_found", "error"
    """
    cached = cache.get(ign)
    if cached:
        return cached

    try:
        async with session.get(CIVINFO_URL, params={"mcNames": ign}) as resp:
            if resp.status != 200:
                logger.debug(f"CivInfo API returned status {resp.status} for {ign}")
                result = ("error", "⚪", None, f"API Error ({resp.status})")
                cache.set(ign, result, ttl=_ttl_for("error"))
                return result

            data = await resp.json()
            if not data or "mcNames" not in data or not data["mcNames"]:
                logger.warning(f"No data for IGN {ign} from CivInfo")
                result = ("not_found", "⚪", None, "Not Found")
                cache.set(ign, result, ttl=_ttl_for("not_found"))
                return result

            timestamps = data.get("loginTimestamps", [])
            if not timestamps:
                logger.warning(f"No login timestamps for {ign}")
                result = ("not_found", "⚪", None, "No Data")
                cache.set(ign, result, ttl=_ttl_for("not_found"))
                return result

            # Filtra eventuali valori non numerici (es. None) che causerebbero errore in max()
            valid_timestamps = [ts for ts in timestamps if isinstance(ts, (int, float))]
            if not valid_timestamps:
                logger.warning(f"Invalid timestamps for {ign}: {timestamps}")
                result = ("error", "⚪", None, "Invalid Data")
                cache.set(ign, result, ttl=_ttl_for("error"))
                return result

            last_ts = max(valid_timestamps) / 1000.0
            last_date = datetime.fromtimestamp(last_ts)
            days_ago = (datetime.now() - last_date).days

            if days_ago < 30:
                emoji, text = "🟢", f"Active ({days_ago}d ago)" if days_ago > 0 else "Active (today)"
            elif days_ago < 60:
                emoji, text = "🟠", f"Semi-Inactive ({days_ago}d ago)"
            else:
                emoji, text = "🔴", f"Inactive ({days_ago}d ago)"

            result = ("ok", emoji, last_date, text)
            cache.set(ign, result, ttl=_ttl_for("ok"))
            return result

    except asyncio.TimeoutError:
        logger.warning(f"Timeout fetching CivInfo for {ign}")
        result = ("error", "⚪", None, "Timeout")
        cache.set(ign, result, ttl=_ttl_for("error"))
        return result
    except Exception as e:
        logger.error(f"Error fetching {ign}: {e}", exc_info=True)
        result = ("error", "⚪", None, "Error")
        cache.set(ign, result, ttl=_ttl_for("error"))
        return result
