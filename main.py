import asyncio
import contextlib
import logging
import os
import signal
from datetime import UTC, datetime, timedelta

import aiohttp
import discord
from discord.ext import commands, tasks

from core import database as db
from core.config import Config
from core.logging_config import setup_logging
from services import backup
from tasks.activity_monitor import ActivityMonitor
from tasks.audit_retention import AuditRetentionTask
from tasks.churn_alerts import ChurnAlertsTask
from tasks.role_sync import RoleSyncTask
from tasks.uptime_monitor import UptimeMonitor
from web.health import start_health_server

# Configure logging BEFORE anything else logs. setup_logging() reads env vars
# directly (LOG_FORMAT/LOG_LEVEL/LOG_FILE/SENTRY_DSN) so it works even if a
# later import (e.g. Config validation) fails and needs readable output.
setup_logging()
logger = logging.getLogger(__name__)


class LambatRegistryBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        # message_content is NOT needed — this bot is slash-command-only.
        # Keeping it off follows the principle of least privilege.
        super().__init__(command_prefix="!", intents=intents, proxy=Config.PROXY_URL)
        self.http_session = None
        self.activity_monitor = None
        self.uptime_monitor = None
        self.role_sync = None  # Phase 2.5: weekly role reconciliation task
        self.audit_retention = None  # ROADMAP §6.2: nightly audit_log prune
        self.churn_alerts = None  # ROADMAP Phase 5: weekly recruiter churn nudges

    async def setup_hook(self):
        """
        This is called when the bot is starting up.
        We initialize the connection pool, database, HTTP session, and load cogs.
        """
        # 1. Initialize the database connection pool
        #    This creates the pool and ensures it's ready before any commands run.
        try:
            await db.get_pool()  # Pre-initialize the pool
            await db.init_db()
            logger.info("Database connection pool and tables ready.")
        except Exception as e:
            logger.critical(f"Failed to initialize database: {e}", exc_info=True)
            raise

        # 2. Set up HTTP session for CivInfo API
        timeout = aiohttp.ClientTimeout(
            total=Config.AIOHTTP_TOTAL_TIMEOUT, connect=Config.AIOHTTP_CONNECT_TIMEOUT
        )
        self.http_session = aiohttp.ClientSession(timeout=timeout)

        # 3. Initialize activity monitor
        self.activity_monitor = ActivityMonitor(self)
        logger.info("ActivityMonitor initialized in setup_hook")
        if self.activity_monitor and hasattr(self.activity_monitor, "daily_check"):
            self.activity_monitor.daily_check.start()
            logger.info(f"daily_check started: {self.activity_monitor.daily_check.is_running()}")
        else:
            logger.error("Failed to initialize daily_check")

        # 3b. Start the scheduled daily database backup loop.
        #     (Previously defined but never .start()-ed, so daily backups silently
        #      never ran. See daily_backup / before_daily_backup below.)
        try:
            self.daily_backup.start()
            logger.info(f"daily_backup started: {self.daily_backup.is_running()}")
        except Exception as e:
            logger.error(f"Failed to start daily_backup loop: {e}", exc_info=True)

        # 3c. Start the CivMC uptime monitor (edge-triggered outage alerts).
        #     Polls mcsrvstat.us every UPTIME_CHECK_INTERVAL seconds and posts
        #     to ALERT_CHANNEL_ID only on online<->offline transitions.
        try:
            self.uptime_monitor = UptimeMonitor(self)
            self.uptime_monitor.start()
        except Exception as e:
            logger.error(f"Failed to start uptime_monitor: {e}", exc_info=True)

        # 3c-bis. Start the weekly role reconciliation task (Phase 2.5).
        #        Checks every citizen still holds the correct Discord roles;
        #        logs discrepancies to the audit channel + audit_log table.
        #        Auto-fixes only when ROLE_SYNC_AUTO=true.
        try:
            self.role_sync = RoleSyncTask(self)
            self.role_sync.start()
        except Exception as e:
            logger.error(f"Failed to start role_sync task: {e}", exc_info=True)

        # 3c-ter. Start the nightly audit-log retention prune (ROADMAP §6.2).
        #        No-op when AUDIT_RETENTION_DAYS <= 0 (keep forever); otherwise
        #        DELETEs rows older than the window at 03:30 UTC and emits an
        #        audit.prune entry so the policy is itself auditable.
        try:
            self.audit_retention = AuditRetentionTask(self)
            self.audit_retention.start()
        except Exception as e:
            logger.error(f"Failed to start audit_retention task: {e}", exc_info=True)

        # 3c-quater. Start the weekly churn-alert nudge task (ROADMAP Phase 5).
        #            DMs a citizen's recruiter(s) when the citizen hasn't logged
        #            into CivMC for CHURN_THRESHOLD_DAYS. Opt-in
        #            (CHURN_NUDGES_ENABLED, default false) — DMs real humans.
        try:
            self.churn_alerts = ChurnAlertsTask(self)
            self.churn_alerts.start()
        except Exception as e:
            logger.error(f"Failed to start churn_alerts task: {e}", exc_info=True)

        # 3d. Start the HTTP keep-alive + health server so the host platform
        #     (e.g. Render) does not mark the service as idle and shut it down.
        #     The same server exposes /healthz (honest liveness: gateway + DB)
        #     and /metrics (basic Prometheus text). See web/health.py.
        try:
            start_health_server(self)
            logger.info(
                f"Health/keep-alive server started on port {os.environ.get('PORT', 10000)}."
            )
        except Exception as e:
            logger.warning(f"Failed to start health/keep-alive server: {e}")

        # 4. Load all cogs
        for filename in os.listdir("cogs"):
            if filename.endswith(".py") and not filename.startswith("__"):
                cog_name = filename[:-3]
                try:
                    await self.load_extension(f"cogs.{cog_name}")
                    logger.info(f"Loaded cog: {cog_name}")
                except Exception as e:
                    logger.error(f"Failed to load cog {cog_name}: {e}", exc_info=True)

        # 5. Sync command tree
        # Guild-scoped sync is instant; global sync can take up to 1 hour.
        # For a single-server nation bot, GUILD_ID should be set in .env.
        if Config.GUILD_ID:
            guild = discord.Object(id=Config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} commands to guild {Config.GUILD_ID} (instant).")
        else:
            synced = await self.tree.sync()
            logger.info(
                f"Synced {len(synced)} commands globally (may take up to 1h to propagate). "
                f"Set GUILD_ID in .env for instant updates."
            )
        commands_list = [cmd.name for cmd in self.tree.get_commands()]
        logger.info(f"Registered commands: {commands_list}")

        # Set custom error handler for app command errors (rate limits, etc.)
        self.tree.on_error = self.on_app_command_error  # type: ignore[method-assign]

    # ------------------------------------------------------------------
    # Gateway lifecycle hooks (Phase 1.4 observability).
    # These log every gateway state transition so operators can correlate
    # command failures / dropped alerts with gateway reconnects.
    # ------------------------------------------------------------------
    async def on_connect(self):
        logger.info("Discord gateway: connected (initial handshake).")

    async def on_ready(self):
        guilds = len(self.guilds) if self.guilds else 0
        logger.info(
            "Discord gateway: READY. Logged in as %s (id=%s). Guilds visible: %d.",
            self.user,
            self.user.id if self.user else "n/a",
            guilds,
        )

    async def on_resumed(self):
        # A RESUMED session means we reconnected WITHOUT losing events — much
        # better than a full re-READY. Still worth logging so a flaky network
        # is visible.
        logger.info("Discord gateway: session RESUMED (reconnected without replay).")

    async def on_disconnect(self):
        # discord.py fires this on ANY gateway disconnect, including the clean
        # shutdown path. Logged at WARNING so it stands out in aggregators; the
        # close() method logs the clean-shutdown context separately.
        logger.warning("Discord gateway: DISCONNECTED. Will retry automatically.")

    async def on_guild_available(self, guild: discord.Guild):
        # Fired when a guild becomes available (initial ready, or after an outage
        # that made it unavailable). Logs the transition so a guild-side Discord
        # outage is visible alongside our own gateway events.
        logger.info(
            "Discord guild available: %s (id=%s, members=%d).",
            guild.name,
            guild.id,
            guild.member_count or 0,
        )

    async def on_guild_unavailable(self, guild: discord.Guild):
        # The counterpart to on_guild_available — the guild is still in cache but
        # Discord reports it temporarily unavailable (usually a Discord-side incident).
        logger.warning("Discord guild unavailable: %s (id=%s).", guild.name, guild.id)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ):
        """Handle application command errors with special handling for rate limits."""
        # Handle rate limit errors specially
        if isinstance(error, discord.app_commands.CommandOnCooldown):
            embed = discord.Embed(
                title="⏳ Rate Limited",
                description=f"Please wait **{error.retry_after:.1f} seconds** before using this command again.",
                color=0xFFCC00,
            )
            embed.add_field(
                name="Why?",
                value="This prevents server overload and ensures fair usage for everyone.",
                inline=False,
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to send rate limit error message: {e}", exc_info=True)
            return

        # General error handling for other errors
        logger.error(
            f"Unhandled app command error in {interaction.command}: {error}", exc_info=True
        )
        embed = discord.Embed(
            title="❌ Unexpected Error",
            description="An unexpected error occurred. The developers have been notified.",
            color=0xED4245,
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to send error message: {e}", exc_info=True)

    @tasks.loop(hours=24)
    async def daily_backup(self):
        await self.wait_until_ready()
        try:
            # Tag the 1st-of-month backup as "monthly" so prune_backups preserves
            # it as long-term history (it's never auto-deleted by retention).
            now = datetime.now(UTC)
            note = "monthly" if now.day == 1 else "daily_scheduled"
            await backup.create_backup("auto", note)
            logger.info(f"Daily backup created successfully (note={note}).")
        except Exception as e:
            logger.error(f"Failed to create daily backup: {e}", exc_info=True)

    @daily_backup.before_loop
    async def before_daily_backup(self):
        await self.wait_until_ready()
        # Schedule for 02:00 UTC (consistent across deployments regardless of
        # the host's local timezone). Override by changing the hour below.
        now = datetime.now(UTC)
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now > target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

    async def close(self):
        """
        Gracefully shut down the bot.
        We close the connection pool, HTTP session, and any other resources.
        """
        logger.info("Shutting down bot...")

        # Stop the daily_check loop if it's running
        if self.activity_monitor and hasattr(self.activity_monitor, "daily_check"):
            self.activity_monitor.daily_check.cancel()
            logger.info("Stopped daily_check loop")

        # Stop the daily_backup loop if it's running
        if self.daily_backup.is_running():
            self.daily_backup.cancel()
            logger.info("Stopped daily_backup loop")

        # Stop the uptime monitor if it's running
        if self.uptime_monitor:
            self.uptime_monitor.stop()

        # Stop the weekly role reconciliation task if running (Phase 2.5).
        if self.role_sync:
            self.role_sync.stop()

        # Stop the nightly audit-log retention prune if running (ROADMAP §6.2).
        if self.audit_retention:
            self.audit_retention.stop()

        # Stop the weekly churn-alert nudge task if running (ROADMAP Phase 5).
        if self.churn_alerts:
            self.churn_alerts.stop()

        # Close HTTP session
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("Closed HTTP session")

        # Close database connection pool
        await db.close_pool()

        # Call the parent close method
        await super().close()
        logger.info("Bot shutdown complete.")


async def main():
    bot = LambatRegistryBot()
    # Config.validate_config() guarantees DISCORD_TOKEN is set (it raises on
    # import otherwise), but mypy sees it as str | None from os.getenv.
    token = Config.DISCORD_TOKEN
    assert token is not None, "DISCORD_TOKEN is set (validate_config enforces it)"

    # Phase 4.1: install signal handlers for graceful shutdown.
    # Container orchestrators (Docker/Kubernetes/Render) send SIGTERM to ask
    # the process to shut down; the default Python behaviour is to die
    # immediately without flushing logs or closing the Discord gateway cleanly
    # (which leaves the bot "online" in Discord for up to 5 minutes). We
    # intercept SIGTERM (and SIGINT, for local Ctrl-C) and route both into
    # bot.close(), which cancels loops, closes the HTTP session + DB pool, and
    # logs out of the gateway — letting Discord mark us offline immediately.
    #
    # loop.add_signal_handler is Unix-only; on Windows we fall back to the
    # signal.signal() form (which runs the callback in a thread, so we still
    # schedule the close on the loop via run_coroutine_threadsafe).
    loop = asyncio.get_running_loop()
    shutdown_requested = False

    def _request_shutdown():
        nonlocal shutdown_requested
        if shutdown_requested:
            # Second signal (e.g. user hits Ctrl-C twice) — force-exit
            # immediately so a stuck shutdown never traps the operator.
            logger.warning("Second shutdown signal received; forcing exit.")
            raise SystemExit(1)
        shutdown_requested = True
        logger.info("Shutdown signal received (SIGTERM/SIGINT); initiating graceful close.")
        # bot.close() is idempotent — calling it here wakes bot.start() so the
        # `finally` block runs, then the bounded wait below enforces the grace.
        asyncio.ensure_future(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except (NotImplementedError, AttributeError, RuntimeError):
            # Windows / non-Unix platforms don't support loop.add_signal_handler.
            # Fall back to the thread-based signal.signal() API. The handler
            # schedules close() on the event loop from whatever thread the
            # signal was delivered to.
            def _thread_handler(signum, frame, _cb=_request_shutdown):
                # Loop already closed — nothing more we can do; the
                # asyncio.run() teardown will handle process exit.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_cb)

            signal.signal(sig, _thread_handler)

    try:
        await bot.start(token)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by interrupt.")
    finally:
        # Bound the close with SHUTDOWN_GRACE_SECONDS so a stuck DB pool close
        # or a hung gateway logout never traps the container past its grace
        # period (which would trigger a SIGKILL — losing log flushes).
        try:
            await asyncio.wait_for(bot.close(), timeout=Config.SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            logger.error(
                "Graceful shutdown exceeded SHUTDOWN_GRACE_SECONDS=%ds; forcing exit.",
                Config.SHUTDOWN_GRACE_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 — never crash during shutdown
            logger.error(f"Error during graceful shutdown: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
