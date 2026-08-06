"""Audit-log retention — nightly prune of old ``audit_log`` rows.

Closes ROADMAP §6.2 (open decision: keep audit forever vs rolling retention).
The default ``AUDIT_RETENTION_DAYS=0`` keeps everything (matches prior
behaviour — text rows are cheap); set e.g. ``730`` for a 2-year rolling window.

Design notes
------------
* **Scheduled, not event-driven.** Runs once a day at 03:30 UTC — deliberately
  offset from the 02:00 ``daily_backup`` and ``daily_check`` jobs so the three
  nightly DB-heavy tasks don't contend for the asyncpg pool.
* **Self-auditing.** When rows are removed, the task emits an ``audit.prune``
  entry (via ``services.audit.emit``) recording how many rows were deleted and
  the retention window. The policy is therefore visible in ``/audit search`` —
  a maintainer can see exactly when retention ran and what it removed.
* **No-op when disabled.** ``AUDIT_RETENTION_DAYS <= 0`` short-circuits before
  any DB call, so deployments that keep audit forever pay zero cost (the loop
  still wakes nightly, logs a DEBUG line, and sleeps — consistent with the
  other background tasks).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from discord.ext import tasks

from core.config import Config
from services import audit

logger = logging.getLogger(__name__)

# Run at 03:30 UTC — offset from daily_backup (02:00) and daily_check (02:00)
# so the three nightly DB-heavy jobs don't contend for the pool. Same
# compute-next-occurrence pattern as the other before_loop hooks.
_PRUNE_HOUR = 3
_PRUNE_MINUTE = 30


class AuditRetentionTask:
    """Nightly ``audit_log`` prune driven by ``AUDIT_RETENTION_DAYS``."""

    def __init__(self, bot):
        self.bot = bot
        logger.info("AuditRetentionTask initialized")

    @tasks.loop(hours=24)
    async def nightly_prune(self):
        await self._run_prune()

    async def _run_prune(self):
        """One prune cycle, extracted so tests can drive it without the loop.

        No-op when ``AUDIT_RETENTION_DAYS <= 0``; otherwise DELETEs old rows and
        emits a self-audit entry. Failures are caught + logged so a transient
        DB blip doesn't permanently kill the loop (matches the other tasks).
        """
        try:
            await self.bot.wait_until_ready()
            days = Config.AUDIT_RETENTION_DAYS
            if days <= 0:
                # Retention disabled — keep everything. Log at DEBUG so the
                # loop's wake cycle is traceable without spamming INFO.
                logger.debug(
                    "Audit retention disabled (AUDIT_RETENTION_DAYS=%d); skipping prune.", days
                )
                return
            deleted = await audit.prune_older_than(days)
            logger.info("Audit retention prune: removed %d rows older than %d days.", deleted, days)
            # Record that the prune ran — the retention policy is itself
            # auditable. actor=None marks it as a system action (not a human).
            if deleted > 0:
                await audit.emit(
                    audit.AUDIT_PRUNE,
                    actor_discord_id=None,
                    target_ign=None,
                    details={"rows_deleted": deleted, "retention_days": days},
                )
        except Exception as e:
            logger.error(f"Error in audit retention prune: {e}", exc_info=True)

    @nightly_prune.before_loop
    async def before_nightly_prune(self):
        try:
            await self.bot.wait_until_ready()
            now = datetime.now(UTC)
            target = now.replace(hour=_PRUNE_HOUR, minute=_PRUNE_MINUTE, second=0, microsecond=0)
            if now > target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info(f"audit retention prune: waiting {wait_seconds:.0f}s until {target}")
            await asyncio.sleep(wait_seconds)
        except Exception as e:
            logger.error(f"Error in before_nightly_prune: {e}", exc_info=True)

    def start(self):
        """Start the nightly prune loop."""
        if not self.nightly_prune.is_running():
            self.nightly_prune.start()
            logger.info(f"nightly_prune started: {self.nightly_prune.is_running()}")

    def stop(self):
        """Cancel the nightly prune loop."""
        if self.nightly_prune.is_running():
            self.nightly_prune.cancel()
            logger.info("Stopped nightly_prune loop")
