"""Weekly churn alerts — nudge recruiters about inactive citizens.

ROADMAP Phase 5 (speculative → implemented): with enough activity data, a
citizen going quiet is invisible until a human runs ``/report``. This task
scans ``activity_cache`` weekly for citizens whose ``last_login`` exceeds
``CHURN_THRESHOLD_DAYS`` AND who have a recruiter on file, then DMs each
recruiter a short nudge.

Design notes
------------
* **No new table.** Cooldown is tracked via ``audit_log`` (action
  ``churn.nudge``): a citizen is skipped if a *delivered* nudge for them exists
  within ``CHURN_NUDGE_COOLDOWN_DAYS``. This reuses the audit infrastructure
  and makes nudge history searchable via ``/audit search action:churn.nudge``.
  Failed DMs (``delivered: false``) are audited too but excluded from the
  cooldown so they retry the following week.
* **Best-effort DMs.** A recruiter may have DMs closed or have left the
  server. Each send is wrapped in ``rate_limit_guard`` (Phase 4.3) and a
  try/except so one bad recipient never aborts the batch.
* **Opt-in.** ``CHURN_NUDGES_ENABLED`` defaults false — DMing real humans is a
  deliberate operational choice, not something a fresh deploy should do.
* **Weekly slot.** Mirrors ``role_sync``'s scheduling: a 24h loop whose
  ``before_loop`` aligns to ``CHURN_NUDGE_WEEKLY_DAY:CHURN_NUDGE_WEEKLY_HOUR``
  (default Monday 04:00 UTC, offset from the other nightly jobs).
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


class ChurnAlertsTask:
    """Weekly recruiter-nudge loop for at-risk citizens."""

    def __init__(self, bot):
        self.bot = bot
        logger.info("ChurnAlertsTask initialized")

    def start(self):
        if not self.weekly_nudge.is_running():
            self.weekly_nudge.start()
            logger.info(f"weekly_nudge started: {self.weekly_nudge.is_running()}")

    def stop(self):
        if self.weekly_nudge.is_running():
            self.weekly_nudge.cancel()
            logger.info("Stopped weekly_nudge loop")

    @tasks.loop(hours=24)
    async def weekly_nudge(self):
        # 24h coarse tick; before_loop aligns to the weekly slot. Same pattern
        # as role_sync: resilient to restarts (runs on the next tick after
        # startup if the slot was missed).
        try:
            await self.bot.wait_until_ready()
        except Exception:  # noqa: BLE001
            return
        now = datetime.now(UTC)
        if now.weekday() != Config.CHURN_NUDGE_WEEKLY_DAY:
            return
        if now.hour != Config.CHURN_NUDGE_WEEKLY_HOUR:
            return
        await self.run_nudge_pass()

    @weekly_nudge.before_loop
    async def before_weekly_nudge(self):
        try:
            await self.bot.wait_until_ready()
            now = datetime.now(UTC)
            target = now.replace(
                hour=Config.CHURN_NUDGE_WEEKLY_HOUR, minute=0, second=0, microsecond=0
            )
            days_ahead = (Config.CHURN_NUDGE_WEEKLY_DAY - now.weekday()) % 7
            target += timedelta(days=days_ahead)
            if target <= now:
                target += timedelta(days=7)
            wait = (target - now).total_seconds()
            logger.info(f"ChurnAlertsTask: first run in {wait:.0f}s ({target} UTC).")
            await asyncio.sleep(wait)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error in before_weekly_nudge: {e}", exc_info=True)

    async def run_nudge_pass(self):
        """One full nudge pass. Exposed for testing (no discord.py loop needed).

        1. Short-circuit when disabled.
        2. Fetch candidates: activity_cache ⋈ citizens ⋈ recruiters, filtered
           to last_login older than the threshold.
        3. Fetch the cooldown set: IGNs with a *delivered* churn.nudge in the
           last CHURN_NUDGE_COOLDOWN_DAYS.
        4. For each candidate not in the cooldown set, DM the recruiter and
           emit a churn.nudge audit entry (delivered true/false).
        """
        if not Config.CHURN_NUDGES_ENABLED:
            logger.debug("Churn nudges disabled (CHURN_NUDGES_ENABLED=false); skipping.")
            return

        candidates = await _fetch_candidates(Config.CHURN_THRESHOLD_DAYS)
        if not candidates:
            logger.info("ChurnAlertsTask: no citizens above the inactivity threshold.")
            return

        cooldown_igns = await _fetch_recently_nudged(Config.CHURN_NUDGE_COOLDOWN_DAYS)
        targets = _select_targets(candidates, cooldown_igns)
        logger.info(
            "ChurnAlertsTask: %d candidates, %d in cooldown, %d to nudge.",
            len(candidates),
            len(candidates) - len(targets),
            len(targets),
        )

        delivered = 0
        failed = 0
        for row in targets:
            try:
                ok = await _send_nudge(
                    self.bot,
                    ign=row["ign"],
                    settlement=row["settlement"],
                    last_login=row["last_login"],
                    recruiter_discord_id=row["recruiter_discord_id"],
                    threshold_days=Config.CHURN_THRESHOLD_DAYS,
                )
                await audit.emit(
                    audit.CHURN_NUDGE,
                    actor_discord_id=row["recruiter_discord_id"],
                    target_ign=row["ign"],
                    details={
                        "recruiter": row["recruiter_discord_id"],
                        "ign": row["ign"],
                        "settlement": row["settlement"],
                        "days_inactive": row["days_inactive"],
                        "threshold_days": Config.CHURN_THRESHOLD_DAYS,
                        "delivered": ok,
                    },
                )
                if ok:
                    delivered += 1
                else:
                    failed += 1
            except Exception as e:  # noqa: BLE001 — one bad row must not abort the pass
                logger.error(
                    f"ChurnAlertsTask: error nudging recruiter {row['recruiter_discord_id']} "
                    f"about {row['ign']}: {e}",
                    exc_info=True,
                )
                failed += 1

        logger.info(
            "ChurnAlertsTask pass complete: %d nudged, %d failed (of %d targets).",
            delivered,
            failed,
            len(targets),
        )


# ---------------------------------------------------------------------------
# DB helpers — kept module-level so they're individually testable/mockable.
# ---------------------------------------------------------------------------


async def _fetch_candidates(threshold_days: int) -> list[dict]:
    """Return one row per (ign, recruiter) where last_login is older than threshold.

    Joins activity_cache → citizens → recruiters so every recruiter of an
    inactive citizen gets their own row (shared responsibility). Adds a
    ``days_inactive`` int for the nudge embed.
    """
    rows = await db.execute_query(
        "SELECT ac.ign, ac.last_login, c.settlement, r.recruiter_discord_id, "
        "EXTRACT(DAY FROM NOW() - ac.last_login)::int AS days_inactive "
        "FROM activity_cache ac "
        "JOIN citizens c ON ac.ign = c.ign "
        "JOIN recruiters r ON ac.ign = r.ign "
        "WHERE ac.last_login IS NOT NULL "
        "AND ac.last_login < NOW() - ($1 * INTERVAL '1 day') "
        "ORDER BY ac.last_login ASC",
        (threshold_days,),
        fetch_all=True,
    )
    return [dict(r) for r in rows] if rows else []


async def _fetch_recently_nudged(cooldown_days: int) -> set[str]:
    """Return the set of IGNs with a *delivered* churn.nudge in the cooldown window.

    Only delivered nudges count toward the cooldown — a failed DM (user closed
    DMs / left the server) is audited with ``delivered: false`` and excluded
    here so it retries the following week.
    """
    rows = await db.execute_query(
        "SELECT DISTINCT target_ign FROM audit_log "
        "WHERE action = $1 AND ts >= NOW() - ($2 * INTERVAL '1 day') "
        "AND details->>'delivered' = 'true'",
        (audit.CHURN_NUDGE, cooldown_days),
        fetch_all=True,
    )
    if not rows:
        return set()
    return {r["target_ign"] for r in rows if r["target_ign"]}


def _select_targets(candidates: list[dict], cooldown_igns: set[str]) -> list[dict]:
    """Pure filter: drop candidates whose IGN is in the cooldown set.

    Extracted so the dedup/cooldown logic is unit-testable without a DB. A
    citizen with multiple recruiters produces multiple candidate rows; the
    cooldown is per-IGN (any delivered nudge about X suppresses all recruiters
    of X until the window expires) — simpler and less noisy than per-recruiter.
    """
    return [c for c in candidates if c["ign"] not in cooldown_igns]


async def _send_nudge(
    bot,
    *,
    ign: str,
    settlement: str,
    last_login: datetime,
    recruiter_discord_id: str,
    threshold_days: int,
) -> bool:
    """DM the recruiter a nudge embed. Returns True on delivery, False on failure.

    Failure modes (all return False, never raise): recruiter ID non-numeric,
    user not found, user has DMs closed, Discord HTTP error. Wrapped in
    ``rate_limit_guard`` so a large batch doesn't trip a gateway 429.
    """
    try:
        recruiter_id_int = int(recruiter_discord_id)
    except (TypeError, ValueError):
        logger.warning(
            f"Skipping nudge: recruiter_discord_id {recruiter_discord_id!r} not numeric."
        )
        return False

    embed = _build_nudge_embed(
        ign=ign, settlement=settlement, last_login=last_login, threshold_days=threshold_days
    )
    try:
        async with role_manager.rate_limit_guard(bot):
            user = await bot.fetch_user(recruiter_id_int)
            await user.send(embed=embed)
        return True
    except discord.NotFound:
        logger.warning(f"Nudge DM failed: Discord user {recruiter_discord_id} not found.")
    except discord.Forbidden:
        logger.warning(
            f"Nudge DM failed: user {recruiter_discord_id} has DMs closed or blocked the bot."
        )
    except discord.HTTPException as e:
        logger.warning(f"Nudge DM failed for user {recruiter_discord_id}: {e}")
    except Exception as e:  # noqa: BLE001 — never let a DM failure crash the pass
        logger.error(f"Unexpected error nudging user {recruiter_discord_id}: {e}", exc_info=True)
    return False


def _build_nudge_embed(
    *, ign: str, settlement: str, last_login: datetime, threshold_days: int
) -> discord.Embed:
    """Build the recruiter-facing nudge embed.

    Pure-ish (constructs a discord.Embed; no I/O). Extracted so tests can
    assert on the field text without driving the full DM path.
    """
    days = (datetime.now(UTC) - last_login).days
    embed = discord.Embed(
        title="📣 Citizen activity check",
        description=(
            "A citizen you recruited hasn't logged into CivMC in a while. "
            "This is an automated nudge from the Lambat Registry bot — a quick "
            "check-in may help retain them."
        ),
        color=0xF39C12,
    )
    embed.add_field(name="Citizen", value=f"`{ign}`", inline=True)
    embed.add_field(name="Settlement", value=settlement or "—", inline=True)
    embed.add_field(
        name="Last login",
        value=f"{days} days ago ({last_login.strftime('%Y-%m-%d')})",
        inline=False,
    )
    embed.set_footer(
        text=f"Threshold: {threshold_days}d inactivity. Reply here if you've been in touch."
    )
    return embed
