import aiohttp
import asyncio
from datetime import datetime
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

CIVINFO_URL = "https://api.civinfo.net/mc-sessions/all"

class CivInfoCache:
    def __init__(self, ttl_seconds=300):
        self.cache = {}
        self.ttl = ttl_seconds

    def get(self, ign: str) -> Optional[Tuple[str, str, Optional[datetime], str]]:
        """Return (status, emoji, last_login, status_text) if cached and fresh."""
        if ign in self.cache:
            data, timestamp = self.cache[ign]
            if datetime.now().timestamp() - timestamp < self.ttl:
                return data
            else:
                del self.cache[ign]
        return None

    def set(self, ign: str, data: Tuple[str, str, Optional[datetime], str]):
        """Store (status, emoji, last_login, status_text) with current timestamp."""
        self.cache[ign] = (data, datetime.now().timestamp())

cache = CivInfoCache()

async def get_player_activity(ign: str, session: aiohttp.ClientSession) -> Tuple[str, str, Optional[datetime], str]:
    """
    Return a tuple (status, emoji, last_login, status_text)
    status is one of: "ok", "not_found", "error"
    """
    cached = cache.get(ign)
    if cached:
        return cached

    try:
        async with session.get(f"{CIVINFO_URL}?mcNames={ign}") as resp:
            if resp.status != 200:
                logger.debug(f"CivInfo API returned status {resp.status} for {ign}")
                result = ("error", "⚪", None, f"API Error ({resp.status})")
                cache.set(ign, result)
                return result

            data = await resp.json()
            if not data or "mcNames" not in data or not data["mcNames"]:
                logger.warning(f"No data for IGN {ign} from CivInfo")
                result = ("not_found", "⚪", None, "Not Found")
                cache.set(ign, result)
                return result

            timestamps = data.get("loginTimestamps", [])
            if not timestamps:
                logger.warning(f"No login timestamps for {ign}")
                result = ("not_found", "⚪", None, "No Data")
                cache.set(ign, result)
                return result

            # Filtra eventuali valori non numerici (es. None) che causerebbero errore in max()
            valid_timestamps = [ts for ts in timestamps if isinstance(ts, (int, float))]
            if not valid_timestamps:
                logger.warning(f"Invalid timestamps for {ign}: {timestamps}")
                result = ("error", "⚪", None, "Invalid Data")
                cache.set(ign, result)
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
            cache.set(ign, result)
            return result

    except asyncio.TimeoutError:
        logger.warning(f"Timeout fetching CivInfo for {ign}")
        result = ("error", "⚪", None, "Timeout")
        cache.set(ign, result)
        return result
    except Exception as e:
        logger.error(f"Error fetching {ign}: {e}", exc_info=True)
        result = ("error", "⚪", None, "Error")
        cache.set(ign, result)
        return result
