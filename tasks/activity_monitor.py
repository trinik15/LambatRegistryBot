import asyncio
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord.ext import tasks

from api import civinfo_api
from core import database as db
from core import emojis as emoji_db
from core.config import Config
from core.constants import Emojis
from core.i18n import tr

logger = logging.getLogger(__name__)

# Max concurrent CivInfo requests. The API is hit on-demand by /report census
# and once a day by daily_check; doing these sequentially made large registries
# take minutes. 5 concurrent is conservative enough to avoid rate-limiting
# while cutting wall-time by ~5x (the per-IGN TTL cache absorbs repeats).
CIVINFO_CONCURRENCY = 5


async def _fetch_activities(igns, session):
    """Fetch CivInfo activity for many IGNs concurrently.

    Returns ``{ign: PlayerActivity}`` — a dict mapping each IGN to its
    :class:`api.civinfo_api.PlayerActivity`. Bounded by a semaphore so we
    don't fire hundreds of requests at once.
    """
    sem = asyncio.Semaphore(CIVINFO_CONCURRENCY)

    async def _one(ign):
        async with sem:
            return ign, await civinfo_api.get_player_activity(ign, session)

    results = await asyncio.gather(*(_one(ign) for ign in igns), return_exceptions=False)
    return dict(results)


async def _persist_activities(activities: dict) -> int:
    """Upsert a batch of PlayerActivity results into the activity_cache table.

    Called by the daily loop after a batch fetch, so /metrics ACTIVE_CITIZENS
    and /report export's LEFT JOIN reflect the latest CivInfo data (not just
    rows written by /citizen add).

    Only rows with ``status == "ok"`` (a real last_login) are persisted —
    ``not_found`` / ``error`` results are skipped to avoid overwriting a known-
    good row with a transient failure. Returns the number of rows upserted.
    """
    if not activities:
        return 0
    pool = await db.get_pool()
    rows_upserted = 0
    async with pool.acquire() as conn:
        for ign, pa in activities.items():
            if pa.status != "ok" or not pa.last_login:
                continue
            await conn.execute(
                "INSERT INTO activity_cache "
                "(ign, last_login, last_logout, first_joined, status, is_online) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (ign) DO UPDATE SET "
                "last_login = EXCLUDED.last_login, "
                "last_logout = EXCLUDED.last_logout, "
                "first_joined = EXCLUDED.first_joined, "
                "status = EXCLUDED.status, "
                "is_online = EXCLUDED.is_online, "
                "last_checked = CURRENT_TIMESTAMP",
                ign,
                pa.last_login,
                pa.last_logout,
                pa.first_joined,
                pa.status,
                pa.is_online,
            )
            rows_upserted += 1
    logger.info(f"Persisted {rows_upserted} activity_cache rows from daily refresh.")
    return rows_upserted


# NOTE: SETTLEMENT_TO_DUCHY was a hardcoded dict mapping each settlement to
# its duchy. As of Phase 2.3 this mapping lives in the ``settlements.duchy``
# DB column (seeded from core.constants.SETTLEMENT_TO_DUCHY during migration).
# The monthly report now JOINs settlements to read duchy directly — no more
# hardcoded mapping in this file.


class ActivityMonitor:
    def __init__(self, bot):
        self.bot = bot
        logger.info("ActivityMonitor INITIALIZED")

    @tasks.loop(hours=24)
    async def daily_check(self):
        logger.info("daily_check LOOP ENTERED")
        try:
            await self.bot.wait_until_ready()
            logger.info("Starting daily activity check")

            today = datetime.now(UTC)
            citizens = await db.execute_query(
                "SELECT ign, join_date, settlement FROM citizens", fetch_all=True
            )
            if not citizens:
                logger.info("No citizens to check")
                return

            # 1. Aggiorna la cache di attività per tutti i cittadini
            #    (concurrent + bounded by a semaphore — was sequential with a
            #     0.5s sleep per citizen, which took minutes on big registries).
            session = self.bot.http_session
            activities = await _fetch_activities([row["ign"] for row in citizens], session)

            # Phase A (WS-3, fix B2): persist the refreshed activity to the
            # activity_cache DB table. Previously the daily loop only updated
            # the in-memory cache, so /metrics ACTIVE_CITIZENS and /report
            # export's LEFT JOIN only reflected rows written by /citizen add —
            # not the daily refresh. Now every successful fetch is upserted,
            # so the gauge and export are always current.
            await _persist_activities(activities)

            # 2. Se è il primo del mese → genera report mensile
            if today.day == 1:  # solo il primo giorno del mese
                logger.info("🔵 Generating monthly report")
                await self.generate_monthly_report()

            logger.info("Daily activity check completed")
        except Exception as e:
            logger.error(f"Error in daily_check: {e}", exc_info=True)

    @daily_check.before_loop
    async def before_daily_check(self):
        try:
            logger.info("before_daily_check CALLED")
            await self.bot.wait_until_ready()
            # Run at 02:00 UTC (consistent across deployments). See
            # before_daily_backup in main.py for the same pattern.
            now = datetime.now(UTC)
            target = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if now > target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info(f"daily_check: waiting {wait_seconds:.0f} seconds until {target}")
            await asyncio.sleep(wait_seconds)
            logger.info("Wait finished, starting daily_check")
        except Exception as e:
            logger.error(f"Error in before_daily_check: {e}", exc_info=True)

    async def generate_monthly_report(self):
        """Generate and send the detailed monthly census report."""
        logger.info("Generating monthly report...")

        today = datetime.now(UTC)
        # Data dell'ultimo giorno del mese precedente (es. se oggi è 1 marzo, last_month = 28/29 febbraio)
        last_month = today.replace(day=1) - timedelta(days=1)
        last_month_date = last_month.date()
        month_name = last_month.strftime("%B %Y")  # Nome del mese passato (es. "February 2026")

        # Phase 2.3: JOIN settlements to read duchy from the DB instead of the
        # hardcoded SETTLEMENT_TO_DUCHY dict.
        citizens = await db.execute_query(
            "SELECT c.ign, c.settlement, c.join_date, s.duchy "
            "FROM citizens c JOIN settlements s ON c.settlement = s.name",
            fetch_all=True,
        )
        if not citizens:
            logger.warning("No citizens to generate monthly report")
            return

        # Raccogli dati correnti per provincia e distretto
        province_totals: dict[str, int] = {}
        province_active: dict[str, int] = {}
        district_totals: dict[str, int] = {}
        district_active: dict[str, int] = {}

        # Fetch every citizen's activity in one concurrent batch instead of
        # one await per citizen (the old loop could exceed Discord's 15-minute
        # interaction token window for large registries).
        activities = await _fetch_activities([c["ign"] for c in citizens], self.bot.http_session)

        # If CivInfo auth is broken, the monthly report's "active population"
        # numbers would all be zero and mislead leadership. We annotate the
        # report honestly instead.
        auth_broken = civinfo_api.is_auth_broken()

        # Build a district→duchy map for the snapshot save loop below (which
        # iterates district_totals, not citizens).
        district_to_duchy: dict[str, str] = {c["settlement"]: c["duchy"] for c in citizens}

        for c in citizens:
            pa = activities.get(c["ign"])
            # Default to an error sentinel if the IGN wasn't fetched (shouldn't
            # happen — _fetch_activities covers every IGN — but be defensive).
            is_active = (pa.emoji == "🟢") if pa else False

            district = c["settlement"]
            duchy = c["duchy"]

            province_totals[duchy] = province_totals.get(duchy, 0) + 1
            if is_active:
                province_active[duchy] = province_active.get(duchy, 0) + 1

            district_totals[district] = district_totals.get(district, 0) + 1
            if is_active:
                district_active[district] = district_active.get(district, 0) + 1

        # Carica snapshot del mese precedente
        old_snapshots = await db.execute_query(
            "SELECT duchy, district, total, active FROM monthly_snapshots WHERE snapshot_date = $1",
            (last_month_date,),
            fetch_all=True,
        )
        old_province = {}
        old_district = {}
        for s in old_snapshots:
            if s["district"] is None:
                old_province[s["duchy"]] = (s["total"], s["active"])
            else:
                old_district[s["district"]] = (s["total"], s["active"])

        def calc_change(old, new):
            if old == 0:
                return None, ""
            pct = (new - old) / old * 100
            arrow = Emojis.UP_ARROW if pct > 0 else Emojis.DOWN_ARROW if pct < 0 else ""
            return round(pct, 2), arrow

        # Costruzione messaggio
        lines = []
        # Phase 4.5: monthly report headers are looked up via tr() so a
        # Filipino-themed deployment (LOCALE=fil) can translate them. The
        # numbers stay locale-independent.
        lines.append(tr("monthly.title", month_name=month_name) + "\n")

        total_citizens = len(citizens)
        active_citizens = sum(province_active.values())
        lines.append(tr("monthly.total_registered", total=total_citizens) + "\n")
        if auth_broken:
            lines.append(tr("monthly.auth_broken") + "\n")
        else:
            pct = round(active_citizens / total_citizens * 100, 2) if total_citizens else 0
            lines.append(tr("monthly.active_population", active=active_citizens, pct=pct) + "\n")

        if old_snapshots:
            old_total = sum(s["total"] for s in old_snapshots if s["district"] is None)
            old_active = sum(s["active"] for s in old_snapshots if s["district"] is None)
            pct_total, arrow_total = calc_change(old_total, total_citizens)
            pct_active, arrow_active = calc_change(old_active, active_citizens)
            if pct_total is not None:
                lines.append(tr("monthly.reg_change", pct=pct_total, arrow=arrow_total))
            if pct_active is not None:
                lines.append(tr("monthly.active_change", pct=pct_active, arrow=arrow_active) + "\n")
        else:
            lines.append("")

        # Nuovi cittadini (negli ultimi 30 giorni)
        # join_date is a DATE object after migration; compare as dates.
        one_month_ago = today.date() - timedelta(days=30)
        new_citizens = 0
        for c in citizens:
            jd = c["join_date"]
            # jd is a datetime.date after migration; fall back to parsing if
            # somehow still a string (defensive).
            if hasattr(jd, "year"):
                join_date = jd
            else:
                try:
                    join_date = datetime.strptime(str(jd), "%d/%m/%Y").date()
                except Exception:
                    continue
            if join_date >= one_month_ago:
                new_citizens += 1
        # Gain line (new citizens this month).
        lines.append(tr("monthly.gain", new_citizens=new_citizens) + "\n")

        # POPULATION PER PROVINCE/TERRITORY
        lines.append(tr("monthly.section.province_total", emoji=Emojis.LAMBAT) + "\n")
        for duchy, total in sorted(province_totals.items(), key=lambda x: x[1], reverse=True):
            emoji = await emoji_db.get_province(duchy)
            old = old_province.get(duchy, (0, 0))[0]
            pct, arrow = calc_change(old, total)
            if pct is None:
                change_str = "(new)"
            else:
                change_str = f"({pct}%)" + (f" {arrow}" if arrow else "")
            lines.append(f"{duchy} {emoji} - {total} {change_str}")
        lines.append("")

        # ACTIVE POPULATION PER PROVINCE
        lines.append(tr("monthly.section.province_active", emoji=Emojis.LAMBAT_CHAD) + "\n")
        for duchy, active in sorted(province_active.items(), key=lambda x: x[1], reverse=True):
            emoji = await emoji_db.get_province(duchy)
            old_active_val = old_province.get(duchy, (0, 0))[1]
            pct, arrow = calc_change(old_active_val, active)
            if pct is None:
                change_str = "(new)"
            else:
                change_str = f"({pct}%)" + (f" {arrow}" if arrow else "")
            lines.append(f"{duchy} {emoji} - {active} {change_str}")
        lines.append("")

        # POPULATION PER DISTRICT
        lines.append(tr("monthly.section.district_total") + "\n")
        for district, total in sorted(district_totals.items(), key=lambda x: x[1], reverse=True):
            emoji = await emoji_db.get_district(district)
            old = old_district.get(district, (0, 0))[0]
            pct, arrow = calc_change(old, total)
            if pct is None:
                change_str = "(new)"
            else:
                change_str = f"({pct}%)" + (f" {arrow}" if arrow else "")
            lines.append(f"{district} {emoji} - {total} {change_str}")
        lines.append("")

        # ACTIVE POPULATION PER DISTRICT
        lines.append(tr("monthly.section.district_active", emoji=Emojis.LAMBATAN_SALUDO) + "\n")
        for district, active in sorted(district_active.items(), key=lambda x: x[1], reverse=True):
            emoji = await emoji_db.get_district(district)
            old_active_val = old_district.get(district, (0, 0))[1]
            pct, arrow = calc_change(old_active_val, active)
            if pct is None:
                change_str = "(new)"
            else:
                change_str = f"({pct}%)" + (f" {arrow}" if arrow else "")
            lines.append(f"{district} {emoji} - {active} {change_str}")
        lines.append("")

        if Config.MONTHLY_REPORT_ROLE_ID:
            lines.append(f"<@&{Config.MONTHLY_REPORT_ROLE_ID}>")  # ping configurable role

        # Send to the configured census channel.
        channel = None
        if Config.MONTHLY_REPORT_CHANNEL_ID:
            channel = self.bot.get_channel(Config.MONTHLY_REPORT_CHANNEL_ID)
            if channel is None:
                # Channel not in cache (e.g. bot just started). Fetch from API.
                try:
                    channel = await self.bot.fetch_channel(Config.MONTHLY_REPORT_CHANNEL_ID)
                except discord.NotFound:
                    logger.error(
                        f"Monthly report channel {Config.MONTHLY_REPORT_CHANNEL_ID} not found."
                    )
                except discord.Forbidden:
                    logger.error(
                        f"Bot lacks permission to view monthly report channel {Config.MONTHLY_REPORT_CHANNEL_ID}."
                    )
                except Exception as e:
                    logger.error(f"Could not fetch monthly report channel: {e}", exc_info=True)
        else:
            logger.error("MONTHLY_REPORT_CHANNEL_ID is not set; cannot send monthly report.")
        if channel:
            full_message = "\n".join(lines)
            try:
                if len(full_message) <= 2000:
                    await channel.send(full_message)
                else:
                    # Split without breaking lines (stay under Discord's 2000-char limit).
                    parts: list[str] = []
                    current: list[str] = []
                    current_len = 0
                    for line in lines:
                        line_len = len(line) + 1  # +1 for the newline
                        if current_len + line_len > 1900:
                            parts.append("\n".join(current))
                            current = [line]
                            current_len = line_len
                        else:
                            current.append(line)
                            current_len += line_len
                    if current:
                        parts.append("\n".join(current))
                    for part in parts:
                        await channel.send(part)
                logger.info("Monthly report sent")
            except discord.Forbidden:
                logger.error(
                    f"Bot lacks permission to send in monthly report channel {Config.MONTHLY_REPORT_CHANNEL_ID}."
                )
            except discord.HTTPException as e:
                logger.error(f"Failed to send monthly report to Discord: {e}", exc_info=True)
        else:
            logger.error("Census channel not found — snapshot will still be saved.")

        # Salva snapshot corrente — DELETE + all INSERTs in ONE transaction so
        # a failure midway can never leave the snapshots table half-populated
        # (which would corrupt next month's "change since last month" math).
        snapshot_date = today.date()
        # district is None for duchy-level (province) rows, a str for district rows.
        snapshot_rows: list[tuple] = []
        for duchy, total in province_totals.items():
            snapshot_rows.append((snapshot_date, duchy, None, total, province_active.get(duchy, 0)))
        for district, total in district_totals.items():
            duchy = district_to_duchy.get(district, "Unknown")
            snapshot_rows.append(
                (snapshot_date, duchy, district, total, district_active.get(district, 0))
            )

        pool = await db.get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM monthly_snapshots WHERE snapshot_date = $1", snapshot_date
            )
            if snapshot_rows:
                await conn.executemany(
                    "INSERT INTO monthly_snapshots (snapshot_date, duchy, district, total, active) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    snapshot_rows,
                )
        logger.info(f"Monthly snapshot saved ({len(snapshot_rows)} rows).")
