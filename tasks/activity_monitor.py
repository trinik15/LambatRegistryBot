import discord
from discord.ext import tasks
from core import database as db
from api import civinfo_api
from core.config import Config
from core.constants import Emojis
from datetime import datetime, timedelta
import logging
import asyncio

logger = logging.getLogger(__name__)

# Mappa distretto -> provincia (duchy) – da completare in base ai dati reali
SETTLEMENT_TO_DUCHY = {
    "New September": "Lambat City",
    "Pioneer": "Lambat City",
    "Sunnebourg": "Lambat City",
    "Poblacion": "Lambat City",
    "Timberbourg": "Florraine",
    "Immerheim": "Florraine",
    "Gulash": "Florraine",
    "Bazariskes": "Valle Occidental",
    "Mt. Abedul": "Valle Occidental",
    "Silenya": "Valle Occidental",
    "Heavensroost": "Valle Occidental",
    "Girasol": "Valle Occidental",
    "Tierra del Cabo": "Capeland",
    "Margaritaville": "Margaritaville",
    "Pampang": "San Canela",
    # Aggiungi altri se necessario
}

class ActivityMonitor:
    def __init__(self, bot):
        self.bot = bot
        logger.info("🟢 ActivityMonitor INITIALIZED")  # NUOVO LOG

    @tasks.loop(hours=24)
    async def daily_check(self):
        logger.info("🟡 daily_check LOOP ENTERED")  # NUOVO LOG
        try:
            await self.bot.wait_until_ready()
            logger.info("Starting daily activity check")

            today = datetime.now()
            citizens = await db.execute_query("SELECT ign, join_date, settlement FROM citizens", fetch_all=True)
            if not citizens:
                logger.info("No citizens to check")
                return

            # 1. Aggiorna la cache di attività per tutti i cittadini
            session = self.bot.http_session
            for row in citizens:
                await civinfo_api.get_player_activity(row["ign"], session)
                await asyncio.sleep(0.5)

            # 2. Se è il primo del mese → genera report mensile
            if True: # forced test - replace with today.day == 1 after test
                logger.info("🔵 Generating monthly report (forced)")  # NUOVO LOG
                await self.generate_monthly_report()

            logger.info("Daily activity check completed")
        except Exception as e:
            logger.error(f"❌ Error in daily_check: {e}")
            import traceback
            traceback.print_exc()

    @daily_check.before_loop
    async def before_daily_check(self):
        try:
            logger.info("🟠 before_daily_check CALLED")  # NUOVO LOG
            await self.bot.wait_until_ready()
            now = datetime.now()
            target = now.replace(hour=21, minute=56, second=0, microsecond=0)  # modifica l'ora
            if now > target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info(f"⏳ daily_check: waiting {wait_seconds:.0f} seconds until {target}")
            await asyncio.sleep(wait_seconds)
            logger.info("⏰ Wait finished, starting daily_check")  # NUOVO LOG
        except Exception as e:
            logger.error(f"❌ Error in before_daily_check: {e}")
            import traceback
            traceback.print_exc()

    async def generate_monthly_report(self):
        """Genera e invia il report mensile dettagliato nel canale census."""
        logger.info("Generating monthly report...")

        today = datetime.now()
        # Data dell'ultimo giorno del mese precedente (es. se oggi è 1 marzo, last_month = 28/29 febbraio)
        last_month = today.replace(day=1) - timedelta(days=1)
        last_month_str = last_month.strftime("%Y-%m-%d")
        month_name = last_month.strftime("%B %Y")  # Nome del mese passato (es. "February 2026")

        citizens = await db.execute_query(
            "SELECT ign, settlement, join_date FROM citizens",
            fetch_all=True
        )
        if not citizens:
            logger.warning("No citizens to generate monthly report")
            return

        # Raccogli dati correnti per provincia e distretto
        province_totals = {}
        province_active = {}
        district_totals = {}
        district_active = {}

        for c in citizens:
            status, emoji, last_login, _ = await civinfo_api.get_player_activity(c["ign"], self.bot.http_session)
            is_active = (emoji == "🟢")  # consideriamo solo i verdi come attivi per il report

            district = c["settlement"]
            duchy = SETTLEMENT_TO_DUCHY.get(district, "Unknown")

            province_totals[duchy] = province_totals.get(duchy, 0) + 1
            if is_active:
                province_active[duchy] = province_active.get(duchy, 0) + 1

            district_totals[district] = district_totals.get(district, 0) + 1
            if is_active:
                district_active[district] = district_active.get(district, 0) + 1

        # Carica snapshot del mese precedente
        old_snapshots = await db.execute_query(
            "SELECT duchy, district, total, active FROM monthly_snapshots WHERE snapshot_date = $1",
            (last_month_str,),
            fetch_all=True
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
                return None, ""  # "new"
            pct = (new - old) / old * 100
            arrow = Emojis.UP_ARROW if pct > 0 else Emojis.DOWN_ARROW if pct < 0 else ""
            return round(pct, 2), arrow

        # Costruzione messaggio
        lines = []
        lines.append(f"# Summary of Lambat's Census of Population for {month_name}\n")

        total_citizens = len(citizens)
        active_citizens = sum(province_active.values())
        lines.append(f"**Total Registered population (does not account for actual activity):** {total_citizens}\n")
        lines.append(f"**Active population (all players who have logged on within the month)**: {active_citizens} ({round(active_citizens/total_citizens*100, 2)}% of reg. citizens)\n")

        # Variazioni totali se abbiamo dati vecchi
        if old_snapshots:
            old_total = sum(s["total"] for s in old_snapshots if s["district"] is None)
            old_active = sum(s["active"] for s in old_snapshots if s["district"] is None)
            pct_total, arrow_total = calc_change(old_total, total_citizens)
            pct_active, arrow_active = calc_change(old_active, active_citizens)
            if pct_total is not None:
                lines.append(f"Registered population change :    {pct_total}% from last month {arrow_total}")
            if pct_active is not None:
                lines.append(f"Active population change:    {pct_active}% from last month {arrow_active}\n")
        else:
            lines.append("")

        # Nuovi cittadini
        one_month_ago = today - timedelta(days=30)
        new_citizens = 0
        for c in citizens:
            try:
                join_date = datetime.strptime(c["join_date"], "%d/%m/%Y")
                if join_date >= one_month_ago:
                    new_citizens += 1
            except:
                pass
        lines.append(f"Gain: +{new_citizens} new citizens (excludes removed/revoked recruits and returnees)\n")

        # POPULATION PER PROVINCE/TERRITORY
        lines.append(f"**{Emojis.LAMBAT} POPULATION PER PROVINCE/TERRITORY, RANKED**\n")
        for duchy, total in sorted(province_totals.items(), key=lambda x: x[1], reverse=True):
            emoji = Emojis.PROVINCE.get(duchy, "")
            old = old_province.get(duchy, (0,0))[0]
            pct, arrow = calc_change(old, total)
            if pct is None:
                change_str = "(new)"
            else:
                change_str = f"({pct}%)" + (f" {arrow}" if arrow else "")
            lines.append(f"{duchy} {emoji} - {total} {change_str}")

        lines.append("")

        # ACTIVE POPULATION PER PROVINCE
        lines.append(f"{Emojis.LAMBAT_CHAD} **ACTIVE POPULATION PER PROVINCE/TERRITORY**\n")
        for duchy, active in sorted(province_active.items(), key=lambda x: x[1], reverse=True):
            emoji = Emojis.PROVINCE.get(duchy, "")
            old_active_val = old_province.get(duchy, (0,0))[1]
            pct, arrow = calc_change(old_active_val, active)
            if pct is None:
                change_str = "(new)"
            else:
                change_str = f"({pct}%)" + (f" {arrow}" if arrow else "")
            lines.append(f"{duchy} {emoji} - {active} {change_str}")

        lines.append("")

        # POPULATION PER DISTRICT
        lines.append("**🏙️ POPULATION PER DISTRICT**\n")
        for district, total in sorted(district_totals.items(), key=lambda x: x[1], reverse=True):
            emoji = Emojis.DISTRICT.get(district, "")
            old = old_district.get(district, (0,0))[0]
            pct, arrow = calc_change(old, total)
            if pct is None:
                change_str = "(new)"
            else:
                change_str = f"({pct}%)" + (f" {arrow}" if arrow else "")
            lines.append(f"{district} {emoji} - {total} {change_str}")

        lines.append("")

        # ACTIVE POPULATION PER DISTRICT
        lines.append(f"{Emojis.LAMBATAN_SALUDO} **ACTIVE POPULATION PER DISTRICT**\n")
        for district, active in sorted(district_active.items(), key=lambda x: x[1], reverse=True):
            emoji = Emojis.DISTRICT.get(district, "")
            old_active_val = old_district.get(district, (0,0))[1]
            pct, arrow = calc_change(old_active_val, active)
            if pct is None:
                change_str = "(new)"
            else:
                change_str = f"({pct}%)" + (f" {arrow}" if arrow else "")
            lines.append(f"{district} {emoji} - {active} {change_str}")

        lines.append("")
        lines.append("<@&1067779118030143549>")  # ping ruolo council

        # Invio nel canale census
        channel = self.bot.get_channel(1477763652731015209)
        if channel:
            full_message = "\n".join(lines)
            if len(full_message) <= 2000:
                await channel.send(full_message)
            else:
                parts = [full_message[i:i+1900] for i in range(0, len(full_message), 1900)]
                for part in parts:
                    await channel.send(part)
            logger.info("Monthly report sent")
        else:
            logger.error("Census channel not found")

        # Salva snapshot corrente
        snapshot_date = today.strftime("%Y-%m-%d")
        await db.execute_query("DELETE FROM monthly_snapshots WHERE snapshot_date = $1", (snapshot_date,))

        for duchy, total in province_totals.items():
            await db.execute_query(
                "INSERT INTO monthly_snapshots (snapshot_date, duchy, district, total, active) VALUES ($1, $2, $3, $4, $5)",
                snapshot_date, duchy, None, total, province_active.get(duchy, 0)
            )
        for district, total in district_totals.items():
            duchy = SETTLEMENT_TO_DUCHY.get(district, "Unknown")
            await db.execute_query(
                "INSERT INTO monthly_snapshots (snapshot_date, duchy, district, total, active) VALUES ($1, $2, $3, $4, $5)",
                snapshot_date, duchy, district, total, district_active.get(district, 0)
            )
        logger.info("Monthly snapshot saved")
