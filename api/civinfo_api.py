import aiohttp
import asyncio
from datetime import datetime, timezone
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

# When the API returns 401/403 we remember the auth-broken state for a while
# so we don't spam the log on every single citizen lookup. The flag auto-
# clears after this TTL, allowing recovery if the key is fixed at runtime.
AUTH_BROKEN_TTL = 600  # 10 min


class CivInfoCache:
    def __init__(self, ttl_seconds=CACHE_TTL_SUCCESS):
        self.cache = {}
        self.ttl = ttl_seconds

    def get(self, ign: str) -> Optional[Tuple[str, str, Optional[datetime], str]]:
        """Return (status, emoji, last_login, status_text) if cached and fresh."""
        if ign in self.cache:
            data, timestamp, ttl = self.cache[ign]
            if datetime.now(timezone.utc).timestamp() - timestamp < ttl:
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
        self.cache[ign] = (data, datetime.now(timezone.utc).timestamp(), actual_ttl)

    def clear(self):
        """Drop every cached entry (e.g. after a database restore)."""
        self.cache.clear()

    def invalidate(self, ign: str):
        """Drop a single cached entry so the next lookup hits the API fresh."""
        self.cache.pop(ign, None)

cache = CivInfoCache()

# --- Auth-broken state -------------------------------------------------------
# CivInfo now requires an API key (contact minecraft.gjum@gmail.com). When the
# key is missing or rejected (401/403), we set this flag so callers can show
# an honest "activity data unavailable" message instead of silently reporting
# "0 active citizens." The flag is logged loudly ONCE per TTL window.
_auth_broken_until: float = 0.0
_auth_warned: bool = False


def is_auth_broken() -> bool:
    """True if the CivInfo API recently returned 401/403 (or no key is set).

    Callers (census, stats, daily_check) should check this before relying on
    activity data, and show an honest degradation message when it's True.
    """
    global _auth_broken_until
    if not _auth_broken_until:
        return False
    if datetime.now(timezone.utc).timestamp() > _auth_broken_until:
        # TTL expired — allow a retry; the warning may fire again if still broken.
        _auth_broken_until = 0.0
        return False
    return True


def _mark_auth_broken(reason: str):
    """Record that the API rejected our auth, logging loudly once per window."""
    global _auth_broken_until, _auth_warned
    _auth_broken_until = datetime.now(timezone.utc).timestamp() + AUTH_BROKEN_TTL
    if not _auth_warned:
        logger.error(
            "CivInfo API auth failed: %s. Activity data will be unavailable "
            "for up to %ds. Set CIVINFO_API_KEY (contact minecraft.gjum@gmail.com "
            "for a key). Reports will show 'Activity data unavailable' instead "
            "of fake counts.",
            reason, AUTH_BROKEN_TTL
        )
        _auth_warned = True


def _clear_auth_broken():
    """Reset the auth-broken flag (e.g. after a successful call)."""
    global _auth_broken_until, _auth_warned
    if _auth_broken_until:
        logger.info("CivInfo API auth recovered — activity data is available again.")
    _auth_broken_until = 0.0
    _auth_warned = False


def _auth_headers():
    """Build request headers, including the Bearer token if a key is set."""
    # Imported here to avoid a circular import at module load time.
    from core.config import Config
    headers = {"Accept": "application/json"}
    if Config.CIVINFO_API_KEY:
        headers["Authorization"] = f"Bearer {Config.CIVINFO_API_KEY}"
    return headers


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

    On 401/403, marks the API as auth-broken (see ``is_auth_broken``) so
    callers can degrade honestly instead of showing fake "0 active" counts.
    """
    cached = cache.get(ign)
    if cached:
        return cached

    # If we already know auth is broken, don't hammer the API — return an
    # honest "unavailable" result immediately. The cache TTL on these is
    # short so we retry periodically (via is_auth_broken's own TTL).
    if is_auth_broken():
        result = ("error", "⚪", None, "API Auth Required")
        cache.set(ign, result, ttl=_ttl_for("error"))
        return result

    try:
        async with session.get(
            CIVINFO_URL, params={"mcNames": ign}, headers=_auth_headers()
        ) as resp:
            if resp.status in (401, 403):
                # Auth rejected (or no key set and the API now requires one).
                # Mark broken so callers degrade honestly; don't spam the log.
                _mark_auth_broken(f"HTTP {resp.status}")
                result = ("error", "⚪", None, "API Auth Required")
                cache.set(ign, result, ttl=_ttl_for("error"))
                return result

            if resp.status != 200:
                logger.debug(f"CivInfo API returned status {resp.status} for {ign}")
                result = ("error", "⚪", None, f"API Error ({resp.status})")
                cache.set(ign, result, ttl=_ttl_for("error"))
                return result

            data = await resp.json()
            # A successful 200 means our auth works — clear any stale broken flag.
            _clear_auth_broken()

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
            last_date = datetime.fromtimestamp(last_ts, tz=timezone.utc)
            days_ago = (datetime.now(timezone.utc) - last_date).days

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
