"""CivInfo API client — player activity + (future) server-status history.

Phase A refactor (see research/civinfo-api-improvement-plan.md):
  * Switched endpoint ``mc-sessions/all`` -> ``mc-accounts/full``. The new
    endpoint returns ``first_joined``, ``last_login`` AND ``last_logout`` in
    a single call (the old one only gave ``loginTimestamps``). We can now
    detect "online right now" (``last_login > last_logout``) without a second
    mcsrvstat.us call.
  * Replaced the fragile ``(status, emoji, last_login, status_text)`` 4-tuple
    with a frozen ``PlayerActivity`` dataclass. Callers access fields by name
    (``pa.emoji``, ``pa.last_login``) instead of by positional index, so the
    return type can grow without breaking every unpacker.
  * Request hygiene: send ``civinfo-version: git:<hash>`` (matches Gjum's
    official frontend, may help with rate-limit allowlisting), use the singular
    ``mcName`` query param (not the legacy ``mcNames`` plural), and always pass
    ``mcServer`` explicitly (the official frontend never omits it).

The honest-degradation contract is unchanged: on 401/403 we set an auth-broken
flag (TTL 10 min, warns once) so callers show "Activity data unavailable"
instead of silently reporting fake "0 active" counts.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

logger = logging.getLogger(__name__)

# --- Endpoint ---------------------------------------------------------------
# Switched from mc-sessions/all (returns loginTimestamps/logoutTimestamps
# parallel arrays) to mc-accounts/full (returns a single account object with
# first_joined, last_login, last_logout). One call gives us 3× the data.
# The base URL is configurable for testing; the path is fixed by the API.
CIVINFO_ENDPOINT = "/mc-accounts/full"

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


# --- PlayerActivity ---------------------------------------------------------
# Replaces the old ``(status, emoji, last_login, status_text)`` 4-tuple.
# Frozen + slots so it's immutable and cheap; callers access fields by name
# (pa.emoji, pa.last_login) — no more fragile positional unpacking.
@dataclass(frozen=True, slots=True)
class PlayerActivity:
    """A single player's CivMC activity, as returned by mc-accounts/full.

    ``status`` is the internal result code (one of ``ok`` / ``not_found`` /
    ``error``); callers should branch on it. ``emoji`` + ``status_text`` are
    display-ready. ``last_login`` / ``last_logout`` / ``first_joined`` are
    timezone-aware UTC datetimes, or ``None`` when unknown.

    The ``is_online`` property derives "currently logged in" from
    ``last_login > last_logout`` — no separate mcsrvstat.us call needed.
    """

    status: str
    emoji: str
    last_login: datetime | None
    status_text: str
    last_logout: datetime | None = None
    first_joined: datetime | None = None

    @property
    def is_online(self) -> bool:
        """True if the player is currently logged in (last_login > last_logout)."""
        return bool(
            self.last_login
            and (not self.last_logout or self.last_login > self.last_logout)
        )


# Sentinel returned when the API is auth-broken and we don't want to re-hit it.
# Module-level so callers can identity-check it if they want (most just read
# .status == "error").
def _auth_broken_result() -> PlayerActivity:
    return PlayerActivity(
        status="error", emoji="⚪", last_login=None, status_text="API Auth Required"
    )


class CivInfoCache:
    """Per-IGN TTL cache. Each entry is a PlayerActivity + timestamp + ttl."""

    def __init__(self, ttl_seconds: int = CACHE_TTL_SUCCESS):
        self.cache: dict[str, tuple[PlayerActivity, float, int]] = {}
        self.ttl = ttl_seconds

    def get(self, ign: str) -> PlayerActivity | None:
        """Return the cached PlayerActivity if fresh, else None (evicting stale)."""
        if ign in self.cache:
            data, timestamp, ttl = self.cache[ign]
            if datetime.now(UTC).timestamp() - timestamp < ttl:
                # Phase 1.5: count cache hits so /metrics can show the hit rate
                # (a high hit rate means we're saving CivInfo API calls).
                try:
                    from core.metrics import record_cache_hit

                    record_cache_hit()
                except Exception:  # noqa: BLE001 — metrics must never break lookups
                    pass
                return data
            else:
                del self.cache[ign]
        return None

    def set(self, ign: str, data: PlayerActivity, ttl: int | None = None):
        """Store a PlayerActivity with current timestamp.

        ``ttl`` overrides the default TTL — used to keep error / not_found
        results around for a shorter window so a transient CivInfo outage
        doesn't blacklist a player for the full success TTL.
        """
        actual_ttl = ttl if ttl is not None else self.ttl
        self.cache[ign] = (data, datetime.now(UTC).timestamp(), actual_ttl)

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
    if datetime.now(UTC).timestamp() > _auth_broken_until:
        # TTL expired — allow a retry; the warning may fire again if still broken.
        _auth_broken_until = 0.0
        return False
    return True


def _mark_auth_broken(reason: str):
    """Record that the API rejected our auth, logging loudly once per window."""
    global _auth_broken_until, _auth_warned
    _auth_broken_until = datetime.now(UTC).timestamp() + AUTH_BROKEN_TTL
    if not _auth_warned:
        logger.error(
            "CivInfo API auth failed: %s. Activity data will be unavailable "
            "for up to %ds. Set CIVINFO_API_KEY (contact minecraft.gjum@gmail.com "
            "for a key). Reports will show 'Activity data unavailable' instead "
            "of fake counts.",
            reason,
            AUTH_BROKEN_TTL,
        )
        _auth_warned = True


def _clear_auth_broken():
    """Reset the auth-broken flag (e.g. after a successful call)."""
    global _auth_broken_until, _auth_warned
    if _auth_broken_until:
        logger.info("CivInfo API auth recovered — activity data is available again.")
    _auth_broken_until = 0.0
    _auth_warned = False


def _request_headers():
    """Build request headers, including Bearer token + civinfo-version.

    The ``civinfo-version`` header mirrors what Gjum's official frontend
    (civmc.netlify.app) sends on every request. It's observational today
    (analytics / allowlisting) but sending it future-proofs us against any
    validation tightening.
    """
    # Imported here to avoid a circular import at module load time.
    from core.config import Config

    headers = {
        "Accept": "application/json",
        "civinfo-version": f"git:{Config.CIVINFO_FRONTEND_VERSION}",
    }
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


def _parse_ts(value) -> datetime | None:
    """Parse a CivInfo timestamp (epoch-ms int/float, or None) into a UTC datetime.

    The mc-accounts/full endpoint returns ``first_joined``, ``last_login``,
    ``last_logout`` as epoch-millisecond integers (matching the old
    loginTimestamps format). None / non-numeric values yield None.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bucket_activity(last_login: datetime) -> tuple[str, str]:
    """Return (emoji, status_text) bucketed by days since last login.

    🟢 Active    < 30 days
    🟠 Semi      30-60 days
    🔴 Inactive  > 60 days
    """
    days_ago = (datetime.now(UTC) - last_login).days
    if days_ago < 30:
        return "🟢", f"Active ({days_ago}d ago)" if days_ago > 0 else "Active (today)"
    if days_ago < 60:
        return "🟠", f"Semi-Inactive ({days_ago}d ago)"
    return "🔴", f"Inactive ({days_ago}d ago)"


async def get_player_activity(
    ign: str, session: aiohttp.ClientSession
) -> PlayerActivity:
    """Fetch a single player's CivMC activity via the mc-accounts/full endpoint.

    Returns a :class:`PlayerActivity` with ``status`` one of:
      * ``"ok"``        — account found; ``last_login`` / ``last_logout`` /
        ``first_joined`` populated.
      * ``"not_found"`` — account doesn't exist on CivMC (likely a typo).
      * ``"error"``     — API failure (timeout, non-200, auth broken).

    On 401/403, marks the API as auth-broken (see :func:`is_auth_broken`) so
    callers degrade honestly instead of showing fake "0 active" counts.

    Results are cached per-IGN with a status-dependent TTL.
    """
    cached = cache.get(ign)
    if cached:
        return cached

    # If we already know auth is broken, don't hammer the API — return an
    # honest "unavailable" result immediately. The cache TTL on these is
    # short so we retry periodically (via is_auth_broken's own TTL).
    if is_auth_broken():
        result = _auth_broken_result()
        cache.set(ign, result, ttl=_ttl_for("error"))
        return result

    # Imported here to avoid a circular import at module load time.
    from core.config import Config

    url = f"{Config.CIVINFO_API_BASE}{CIVINFO_ENDPOINT}"

    try:
        async with session.get(
            url,
            params={"mcName": ign, "mcServer": Config.CIVINFO_MC_SERVER},
            headers=_request_headers(),
        ) as resp:
            if resp.status in (401, 403):
                # Auth rejected (or no key set and the API now requires one).
                # Mark broken so callers degrade honestly; don't spam the log.
                _mark_auth_broken(f"HTTP {resp.status}")
                result = _auth_broken_result()
                cache.set(ign, result, ttl=_ttl_for("error"))
                return result

            if resp.status != 200:
                logger.debug(f"CivInfo API returned status {resp.status} for {ign}")
                result = PlayerActivity(
                    status="error",
                    emoji="⚪",
                    last_login=None,
                    status_text=f"API Error ({resp.status})",
                )
                cache.set(ign, result, ttl=_ttl_for("error"))
                return result

            data = await resp.json()
            # A successful 200 means our auth works — clear any stale broken flag.
            _clear_auth_broken()

            # mc-accounts/full returns {"accounts": [{uuid, mc_name, first_joined,
            # last_login, last_logout}]}. An empty list (or missing key) means
            # the IGN doesn't exist on CivMC.
            accounts = data.get("accounts") if isinstance(data, dict) else None
            if not accounts:
                logger.warning(f"No account data for IGN {ign} from CivInfo")
                result = PlayerActivity(
                    status="not_found", emoji="⚪", last_login=None, status_text="Not Found"
                )
                cache.set(ign, result, ttl=_ttl_for("not_found"))
                return result

            acct = accounts[0]
            last_login = _parse_ts(acct.get("last_login"))
            if not last_login:
                # Account exists but has never logged in (or timestamp missing).
                logger.warning(f"No last_login for {ign}: {acct}")
                result = PlayerActivity(
                    status="not_found", emoji="⚪", last_login=None, status_text="No Data"
                )
                cache.set(ign, result, ttl=_ttl_for("not_found"))
                return result

            emoji, text = _bucket_activity(last_login)
            result = PlayerActivity(
                status="ok",
                emoji=emoji,
                last_login=last_login,
                status_text=text,
                last_logout=_parse_ts(acct.get("last_logout")),
                first_joined=_parse_ts(acct.get("first_joined")),
            )
            cache.set(ign, result, ttl=_ttl_for("ok"))
            return result

    except TimeoutError:
        logger.warning(f"Timeout fetching CivInfo for {ign}")
        result = PlayerActivity(
            status="error", emoji="⚪", last_login=None, status_text="Timeout"
        )
        cache.set(ign, result, ttl=_ttl_for("error"))
        return result
    except Exception as e:
        logger.error(f"Error fetching {ign}: {e}", exc_info=True)
        result = PlayerActivity(
            status="error", emoji="⚪", last_login=None, status_text="Error"
        )
        cache.set(ign, result, ttl=_ttl_for("error"))
        return result
