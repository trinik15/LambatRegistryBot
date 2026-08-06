"""Weekly role reconciliation task (Phase 2.5).

Checks every citizen's Discord member still holds the citizen/settler/
settlement roles and lacks the guest role. Discrepancies are recorded in the
audit log and posted to the audit channel. When ``ROLE_SYNC_AUTO=true`` the
bot also re-applies the correct roles automatically.

Runs weekly at ``ROLE_SYNC_WEEKLY_DAY:ROLE_SYNC_WEEKLY_HOUR`` UTC (default
Monday 03:00 UTC). The check is idempotent: running it twice in a row produces
no duplicate fixes.

Why a task and not on-member-update?
-----------------------------------
discord.py's ``on_member_update`` fires on EVERY role change for EVERY member,
which is noisy and would make the reconciliation logic fight manual council
adjustments (e.g. a council member temporarily removing a citizen role during
a dispute would get it instantly re-added). A weekly batch is deliberate: it
surfaces drift without being adversarial, and the audit log gives a clear
history of who drifted and when it was noticed.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord.ext import tasks

from core import database as db
from core.config import Config
from services import audit, role_manager

logger = logging.getLogger(__name__)


class RoleSyncTask:
    """Weekly role reconciliation loop.

    The loop is started in ``main.py``'s ``setup_hook`` and cancelled in
    ``bot.close()``. It delegates the actual checking to a function that's
    easy to unit-test without a live Discord guild.
    """

    def __init__(self, bot):
        self.bot = bot
        self._task = None

    def start(self):
        self.weekly_sync.start()
        logger.info("RoleSyncTask scheduled (weekly).")

    def stop(self):
        if self.weekly_sync.is_running():
            self.weekly_sync.cancel()
            logger.info("RoleSyncTask stopped.")

    @tasks.loop(hours=24)
    async def weekly_sync(self):
        # The 24h loop is a coarse tick; the before_loop waits until the
        # configured weekly slot. This pattern (used by daily_check too) keeps
        # the loop resilient to restarts: if the bot is down at the slot time,
        # it runs on the next tick after startup rather than skipping the week.
        try:
            await self.bot.wait_until_ready()
        except Exception:  # noqa: BLE001
            return
        # Only actually run on the configured weekday.
        now = datetime.now(UTC)
        if now.weekday() != Config.ROLE_SYNC_WEEKLY_DAY:
            return
        # And only once per day (guard against the 24h loop firing twice on
        # the same weekday after a restart near midnight).
        if now.hour != Config.ROLE_SYNC_WEEKLY_HOUR:
            return
        await self.run_check()

    @weekly_sync.before_loop
    async def before_weekly_sync(self):
        try:
            await self.bot.wait_until_ready()
            # Align to the next occurrence of the configured slot so the first
            # run doesn't happen immediately on startup (which could surprise
            # a freshly-restarted bot during peak hours).
            now = datetime.now(UTC)
            target = now.replace(
                hour=Config.ROLE_SYNC_WEEKLY_HOUR, minute=0, second=0, microsecond=0
            )
            days_ahead = (Config.ROLE_SYNC_WEEKLY_DAY - now.weekday()) % 7
            target += timedelta(days=days_ahead)
            if target <= now:
                target += timedelta(days=7)
            wait = (target - now).total_seconds()
            logger.info(f"RoleSyncTask: first run in {wait:.0f}s ({target} UTC).")
            await asyncio.sleep(wait)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error in before_weekly_sync: {e}", exc_info=True)

    async def run_check(self):
        """Run one full reconciliation pass. Exposed for testing."""
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            logger.warning("RoleSyncTask: bot is in no guilds; skipping.")
            return

        citizens = await db.execute_query(
            "SELECT ign, discord_id, settlement FROM citizens", fetch_all=True
        )
        if not citizens:
            logger.info("RoleSyncTask: no citizens to check.")
            return

        discrepancies = 0
        fixed = 0
        for c in citizens:
            try:
                member = guild.get_member(int(c["discord_id"]))
                if member is None:
                    # Member left the guild (or isn't cached). Record as a
                    # discrepancy so council can decide whether to remove the
                    # citizen from the registry.
                    await audit.emit(
                        audit.ROLE_SYNC_DISCREPANCY,
                        None,
                        c["ign"],
                        {"member": c["discord_id"], "issue": "member_not_in_guild"},
                    )
                    discrepancies += 1
                    continue

                issues = detect_role_issues(member, c["settlement"])
                if not issues:
                    continue

                discrepancies += 1
                await audit.emit(
                    audit.ROLE_SYNC_DISCREPANCY,
                    None,
                    c["ign"],
                    {"member": c["discord_id"], "issues": issues},
                )
                await audit.post_to_channel(
                    self.bot,
                    audit.ROLE_SYNC_DISCREPANCY,
                    None,
                    c["ign"],
                    {"member": c["discord_id"], "issues": issues},
                )

                if Config.ROLE_SYNC_AUTO:
                    try:
                        # Phase 4.3: back off when the Discord gateway is
                        # globally rate-limited. The weekly loop can fire
                        # many add_roles calls in succession (one per
                        # discrepancy), which can trip a 429 mid-batch and
                        # leave the sync half-done. The guard waits up to
                        # 30s for the limit to clear before each op.
                        async with role_manager.rate_limit_guard(self.bot):
                            await role_manager.assign_citizen_roles(member, c["settlement"])
                        fixed += 1
                        await audit.emit(
                            audit.ROLE_SYNC_FIXED,
                            None,
                            c["ign"],
                            {"member": c["discord_id"], "fixed": issues},
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            f"RoleSyncTask: auto-fix failed for {c['ign']}: {e}",
                            exc_info=True,
                        )
            except Exception as e:  # noqa: BLE001 — one bad row must not abort the pass
                logger.error(f"RoleSyncTask: error checking citizen {c['ign']}: {e}")

        logger.info(
            f"RoleSyncTask pass complete: {len(citizens)} checked, "
            f"{discrepancies} discrepancies, {fixed} auto-fixed."
        )


def detect_role_issues(member: discord.Member, settlement: str) -> list[str]:
    """Return a list of human-readable role discrepancies for a member.

    Pure function (no I/O) so it's trivially unit-testable with a fake member.
    Each issue is a short string suitable for the audit details JSON.
    """
    issues: list[str] = []
    member_role_ids = {r.id for r in member.roles}

    # Missing citizen role(s).
    for rid in Config.CITIZEN_ROLE_IDS:
        if rid and rid not in member_role_ids:
            issues.append(f"missing_citizen_role:{rid}")
            break  # one is enough to flag

    # Missing settler role.
    if Config.SETTLER_ROLE_ID and Config.SETTLER_ROLE_ID not in member_role_ids:
        issues.append("missing_settler_role")

    # Missing settlement role (case-insensitive name match).
    if settlement:
        target = settlement.lower()
        has_settlement_role = any(r.name.lower() == target for r in member.roles)
        if not has_settlement_role:
            issues.append(f"missing_settlement_role:{settlement}")

    # Has guest role (should have been removed on registration).
    if Config.GUEST_ROLE_ID and Config.GUEST_ROLE_ID in member_role_ids:
        issues.append("has_guest_role")

    return issues
