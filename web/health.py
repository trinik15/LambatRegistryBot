"""Health + keep-alive HTTP server.

Replaces the old always-200 ``http_keepalive.py``. Three endpoints:

* ``/``        (and any non-matching path) — 200 OK body, keeps hosts like
  Render/Railway from marking the bot idle. (Legacy keep-alive behaviour.)
* ``/healthz`` — honest liveness. Returns 200 **only** when the Discord
  gateway is connected and the database pool answers ``SELECT 1``. Returns
  503 otherwise, so a host liveness probe won't be lied to.
* ``/metrics`` — Prometheus text-format metrics (Phase 1.5). Populated by
  :func:`core.metrics.collect_metrics` then rendered with ``generate_latest``.

The server runs on a daemon thread and uses ``ThreadingTCPServer`` so a
slow ``/healthz`` DB ping never blocks the keep-alive responses.

CivMC relevance: leadership and hosting platforms need an honest signal that
the bot is actually functioning — not just that the process is alive.
"""

import asyncio
import http.server
import logging
import socketserver
import time
from typing import Any

from core.config import Config

logger = logging.getLogger(__name__)


class _BotHealthState:
    """Mutable container holding the references the health handler needs.

    Populated by :func:`start_health_server` from the bot instance. The
    handler thread reads it without locking — these values are write-once
    at startup and then read-only, which is safe for our purposes.
    """

    bot: Any = None  # discord.Bot / commands.Bot
    loop: Any = None  # asyncio event loop (set at start_health_server)


async def _db_ping(bot) -> bool:
    """Run ``SELECT 1`` on the asyncpg pool. True if the DB answers."""
    try:
        from core import database as db

        pool = await db.get_pool()
        if pool is None:
            return False
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
            return value == 1
    except Exception as e:
        logger.debug(f"healthz DB ping failed: {e}")
        return False


def _check_gateway(bot) -> bool:
    """True if the Discord gateway is ready and the bot is not closed."""
    if bot is None:
        return False
    try:
        # discord.py exposes is_ready() and is_closed() on Client/Bot.
        return bool(bot.is_ready()) and not bool(bot.is_closed())
    except Exception:
        return False


def _check_civinfo() -> bool:
    """True if CivInfo auth is NOT in a long-broken state.

    Note: a broken CivInfo key is a *soft* degradation — the bot still serves
    every command, just with "activity unavailable" instead of fake counts.
    We report it here (and in /metrics) but do NOT fail /healthz on it, since
    a host restarting the bot over a broken third-party key would loop
    pointlessly without fixing anything.
    """
    try:
        from api import civinfo_api

        return not civinfo_api.is_auth_broken()
    except Exception:
        # If we can't even import the module, don't guess — treat as ok
        # so we don't fail health on an unrelated import error.
        return True


class HealthHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for /healthz, /metrics, and the legacy keep-alive path."""

    # Class attribute set by start_health_server; the handler instances share
    # this reference.
    state: _BotHealthState = _BotHealthState()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/healthz", "/health"):
            self._handle_healthz()
        elif path == "/metrics":
            self._handle_metrics()
        else:
            # Legacy keep-alive: always 200 so the host doesn't idle-kill us.
            self._send(200, "text/plain", "Lambat Registry Bot is running.")

    # --- endpoints -------------------------------------------------------

    def _handle_healthz(self):
        bot = self.state.bot
        gateway_ok = _check_gateway(bot)

        # Run the (async) DB ping on the bot's event loop with a short
        # timeout so a wedged DB doesn't hang the liveness probe.
        db_ok = False
        loop = self.state.loop
        if loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(_db_ping(bot), loop)
                db_ok = bool(fut.result(timeout=3))
            except TimeoutError:
                db_ok = False
            except Exception:
                db_ok = False

        civinfo_ok = _check_civinfo()

        # Liveness gating: gateway + DB must both be up. CivInfo is reported
        # but not gating (it's a gracefully-degraded third-party dependency).
        healthy = gateway_ok and db_ok

        body_lines = [
            "{",
            f'  "status": "{"ok" if healthy else "degraded"}",',
            f'  "discord_gateway": {str(gateway_ok).lower()},',
            f'  "database": {str(db_ok).lower()},',
            f'  "civinfo_ok": {str(civinfo_ok).lower()},',
            f'  "timestamp": {int(time.time())}',
            "}",
        ]
        body = "\n".join(body_lines)
        self._send(200 if healthy else 503, "application/json", body)

    def _handle_metrics(self):
        # Populated the scrape-time gauges from live DB + in-memory state via
        # the bot's event loop, then render with prometheus_client.
        body = b""
        try:
            from core import metrics

            loop = self.state.loop
            if loop is not None:
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        metrics.collect_metrics(self.state.bot), loop
                    )
                    fut.result(timeout=5)
                except TimeoutError:
                    logger.warning("metrics collection timed out; rendering last-known values.")
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"metrics collection error: {e}")
            body, content_type = metrics.render()
        except Exception as e:  # noqa: BLE001 — never 500 on /metrics
            logger.error(f"Failed to render metrics: {e}", exc_info=True)
            body = b"# metrics rendering failed\n"
            content_type = "text/plain"
        self._send(200, content_type, body)

    # --- helpers ---------------------------------------------------------

    def _send(self, status: int, content_type: str, body):
        """Send a response. ``body`` may be str or bytes."""
        data = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.send_response(status)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        # Silence default request logging so the bot log stays readable.
        # (The old keep-alive server did the same.)
        pass


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    """Threading server so /healthz DB pings don't block keep-alive probes.

    ``allow_reuse_address`` avoids "Address already in use" on quick restarts.
    """

    allow_reuse_address = True
    daemon_threads = True


def _run_health_server(port: int):
    try:
        with _ThreadingTCPServer(("", port), HealthHandler) as httpd:
            logger.info(f"Health/keep-alive server listening on port {port}")
            httpd.serve_forever()
    except Exception as e:
        # Never crash the bot if the port is unavailable (e.g. already in use
        # or PORT not set). Match the old keep-alive's defensive behaviour.
        logger.warning(f"Health/keep-alive server could not bind to port {port}: {e}")


def start_health_server(bot):
    """Start the health + keep-alive HTTP server on a daemon thread.

    Captures the bot reference and its event loop so the handler can run
    async DB pings via ``asyncio.run_coroutine_threadsafe``. Safe to call
    from ``setup_hook`` (the loop is running by then).
    """
    # Capture the running loop NOW (from the bot) so the handler thread can
    # schedule coroutines onto it. start_health_server is called from
    # setup_hook (a coroutine), so the loop is running in this thread.
    loop = getattr(bot, "loop", None)
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Fallback: should not happen when called from setup_hook.
            loop = asyncio.new_event_loop()
    HealthHandler.state.bot = bot
    HealthHandler.state.loop = loop

    port = Config.PORT
    import threading

    thread = threading.Thread(target=_run_health_server, args=(port,), daemon=True)
    thread.start()
