import discord
from discord.ext import tasks
from core import database as db
from api import civinfo_api
from core.config import Config
from datetime import datetime, timedelta
import logging
import asyncio

logger = logging.getLogger(__name__)

class ActivityMonitor:
    def __init__(self, bot):
        self.bot = bot
        # Do NOT start the task here – it will be started in on_ready

    @tasks.loop(hours=24)
    async def daily_check(self):
        await self.bot.wait_until_ready()
        logger.info("Starting daily activity check")

        citizens = await db.execute_query("SELECT ign FROM citizens", fetch_all=True)
        if not citizens:
            return

        # Usa la sessione HTTP del bot invece di crearne una nuova
        session = self.bot.http_session
        for row in citizens:
            await civinfo_api.get_player_activity(row["ign"], session)
            await asyncio.sleep(0.5)

        channel = self.bot.get_channel(Config.REGISTRY_CHANNEL_ID)
        if channel:
            row = await db.execute_query("SELECT COUNT(*) as count FROM citizens", fetch_one=True)
            count = row["count"] if row else 0
            await channel.send(f"📊 Daily census: **{count}** citizens registered.")

        if datetime.now().weekday() == 0:
            council_channel = self.bot.get_channel(Config.COUNCIL_CHANNEL_ID)
            if council_channel:
                inactive_list = []
                # Riusa la stessa sessione anche per il secondo ciclo
                for row in citizens:
                    status, emoji, _, _ = await civinfo_api.get_player_activity(row["ign"], session)
                    if emoji == "🔴":
                        inactive_list.append(row["ign"])
                if inactive_list:
                    await council_channel.send(f"🔴 Inactive citizens ({len(inactive_list)}): " + ", ".join(inactive_list[:20]))
                else:
                    await council_channel.send("✅ No inactive citizens.")

        logger.info("Daily activity check completed")

    @daily_check.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()
        now = datetime.now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now > target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
