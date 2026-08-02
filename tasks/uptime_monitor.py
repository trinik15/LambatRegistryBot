"""Uptime monitor — edge-triggered CivMC outage / recovery alerts.

CivMC relevance: citizens get stranded not knowing if the server is down or
if it's just them. This task polls mcsrvstat.us every few minutes and posts
an alert **only on state transitions** (online→offline, offline→online) to
avoid spamming. Downtime duration is reported on recovery.

The monitor reuses the same mcsrvstat.us endpoint as /server status, so no
extra auth or dependencies are needed.
"""

import discord
from discord.ext import tasks
from core.config import Config
import logging
import aiohttp
from datetime import datetime

logger = logging.getLogger(__name__)

# How many consecutive failed polls before we declare an outage. This avoids
# false alarms from a single transient mcsrvstat hiccup (mcsrvstat itself can
# be briefly unavailable even when CivMC is fine).
OUTAGE_THRESHOLD = 2


class UptimeMonitor:
    """Polls CivMC server status and alerts on transitions.

    State machine:
      - ``last_online`` (bool): last known server state.
      - ``fail_count`` (int): consecutive poll failures.
      - ``outage_start`` (datetime|None): when the outage was first declared.
      - ``alerted_outage`` (bool): whether we already posted the outage alert
        (so we don't re-post every poll while the outage persists).
    """

    def __init__(self, bot):
        self.bot = bot
        self.last_online = True  # assume online at start (avoid false startup alert)
        self.fail_count = 0
        self.outage_start = None
        self.alerted_outage = False
        logger.info("🖥️ UptimeMonitor initialized")

    async def _fetch_online(self) -> bool | None:
        """Return True if server is online, False if offline, None on error.

        ``None`` means "we couldn't tell" (mcsrvstat itself failed) — this is
        NOT counted as an outage, only as a skipped poll.
        """
        url = f"{Config.MCSRVSTAT_API_BASE}/{Config.SERVER_ADDRESS}"
        try:
            async with self.bot.http_session.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"mcsrvstat returned {resp.status} for uptime check")
                    return None
                data = await resp.json()
                return bool(data.get("online"))
        except Exception as e:
            logger.debug(f"Uptime poll failed: {e}")
            return None

    async def _send_alert(self, embed: discord.Embed, content: str = ""):
        """Post an alert to the configured channel (if any)."""
        if not Config.ALERT_CHANNEL_ID:
            logger.warning(
                "Uptime alert skipped — ALERT_CHANNEL_ID is not set. "
                f"Would have posted: {embed.title}"
            )
            return
        channel = self.bot.get_channel(Config.ALERT_CHANNEL_ID)
        if not channel:
            logger.error(f"Alert channel {Config.ALERT_CHANNEL_ID} not found.")
            return
        try:
            await channel.send(content=content, embed=embed)
        except Exception as e:
            logger.error(f"Failed to send uptime alert: {e}", exc_info=True)

    async def _poll(self):
        """Run one status poll and handle state transitions."""
        result = await self._fetch_online()

        if result is None:
            # mcsrvstat itself was unreachable — don't change state, just skip.
            logger.debug("Uptime poll inconclusive (API unreachable), skipping.")
            return

        if result:
            # --- Server is online ---
            if not self.last_online or self.alerted_outage:
                # Transition: offline -> online (recovery).
                duration_str = ""
                if self.outage_start:
                    delta = datetime.now() - self.outage_start
                    mins = int(delta.total_seconds() // 60)
                    if mins < 60:
                        duration_str = f" (was down ~{mins}m)"
                    else:
                        hours = mins // 60
                        rem_mins = mins % 60
                        duration_str = f" (was down ~{hours}h {rem_mins}m)"

                embed = discord.Embed(
                    title="✅ CivMC Server Recovered",
                    description=f"The server is back online{duration_str}.",
                    color=0x3BAD4C
                )
                embed.add_field(
                    name="Address",
                    value=f"`{Config.SERVER_ADDRESS}`",
                    inline=False
                )
                embed.set_footer(text="Use /server status for live details")
                await self._send_alert(embed, content="✅ **CivMC is back online**")
                logger.info(f"CivMC recovered after outage{duration_str}.")

            self.last_online = True
            self.fail_count = 0
            self.outage_start = None
            self.alerted_outage = False

        else:
            # --- Server appears offline ---
            self.fail_count += 1
            if self.fail_count < OUTAGE_THRESHOLD:
                logger.debug(
                    f"CivMC offline poll ({self.fail_count}/{OUTAGE_THRESHOLD}) — waiting for confirmation."
                )
                return

            # We've confirmed the outage (enough consecutive failures).
            if not self.alerted_outage:
                self.outage_start = self.outage_start or datetime.now()
                self.last_online = False
                self.alerted_outage = True

                embed = discord.Embed(
                    title="⚠️ CivMC Server Appears Down",
                    description=(
                        f"The CivMC server (`{Config.SERVER_ADDRESS}`) is not responding.\n"
                        f"This could be a scheduled restart, a crash, or maintenance.\n"
                        f"You will be notified when it comes back up."
                    ),
                    color=0xED4245
                )
                embed.add_field(
                    name="Detected at",
                    value=self.outage_start.strftime("%H:%M:%S UTC"),
                    inline=True
                )
                embed.set_footer(text="Automatic recovery alert will follow")
                await self._send_alert(embed, content="@here ⚠️ **CivMC appears to be down**")
                logger.warning("CivMC outage detected — alert sent.")
            else:
                # Already alerted — just log quietly.
                logger.debug("CivMC still offline (already alerted).")

    @tasks.loop(seconds=Config.UPTIME_CHECK_INTERVAL)
    async def uptime_check(self):
        try:
            await self._poll()
        except Exception as e:
            logger.error(f"Error in uptime_check: {e}", exc_info=True)

    @uptime_check.before_loop
    async def before_uptime_check(self):
        await self.bot.wait_until_ready()
        logger.info(
            f"Uptime monitor starting (polling every {Config.UPTIME_CHECK_INTERVAL}s, "
            f"server={Config.SERVER_ADDRESS}, alert_channel={Config.ALERT_CHANNEL_ID or 'DISABLED'})"
        )

    def start(self):
        """Start the uptime polling loop."""
        if not self.uptime_check.is_running():
            self.uptime_check.start()
            logger.info(f"uptime_check started: {self.uptime_check.is_running()}")

    def stop(self):
        """Cancel the uptime polling loop."""
        if self.uptime_check.is_running():
            self.uptime_check.cancel()
            logger.info("Stopped uptime_check loop")
