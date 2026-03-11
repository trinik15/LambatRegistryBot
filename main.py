import discord
from discord.ext import commands, tasks
import logging
import os
import aiohttp
import traceback
import asyncio
from datetime import datetime, timedelta

from core.config import Config
from core import database as db
from services import backup
from tasks.activity_monitor import ActivityMonitor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('pavia_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class PaviaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        proxy_url = os.getenv("PROXY_URL")
        super().__init__(
            command_prefix="!",
            intents=intents,
            proxy=proxy_url
        )
        self.http_session = None
        self.activity_monitor = None
        self.command_semaphore = asyncio.Semaphore(3)

    async def setup_hook(self):
        await db.init_db()
        timeout = aiohttp.ClientTimeout(total=5, connect=3)
        self.http_session = aiohttp.ClientSession(timeout=timeout)
        self.activity_monitor = ActivityMonitor(self)

        for filename in os.listdir("cogs"):
            if filename.endswith(".py") and not filename.startswith("__"):
                cog_name = filename[:-3]
                try:
                    await self.load_extension(f"cogs.{cog_name}")
                    logger.info(f"Loaded cog: {cog_name}")
                except Exception as e:
                    logger.error(f"Failed to load cog {cog_name}: {e}")

        await self.tree.sync()
        logger.info("All cogs loaded and synced.")
        commands_list = [cmd.name for cmd in self.tree.get_commands()]
        logger.info(f"Registered commands: {commands_list}")
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        logger.error(f"Unhandled app command error in {interaction.command}: {error}\n{traceback.format_exc()}")
        embed = discord.Embed(
            title="❌ Unexpected Error",
            description="An unexpected error occurred. The developers have been notified.",
            color=0xED4245
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except:
            pass

    @tasks.loop(hours=24)
    async def daily_backup(self):
        await self.wait_until_ready()
        await backup.create_backup("auto", "daily_scheduled")
        logger.info("Daily backup created.")

    @daily_backup.before_loop
    async def before_daily_backup(self):
        await self.wait_until_ready()
        now = datetime.now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now > target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

    async def close(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await db.close_pool()
        await super().close()

async def run_bot():
    bot = PaviaBot()
    try:
        await bot.start(Config.DISCORD_TOKEN)
        await bot.wait_until_ready()
        logger.info(f"Logged in as {bot.user}")
        
        # Avvio dei task con gestione errori
        try:
            bot.daily_backup.start()
            bot.activity_monitor.daily_check.start()
            logger.info(f"🟢 daily_check started: {bot.activity_monitor.daily_check.is_running()}")
        except Exception as e:
            logger.error(f"❌ Failed to start daily_check: {e}")
            import traceback
            traceback.print_exc()
        
        print(f"✅ Bot online as {bot.user}")
        await asyncio.Future()  # run forever
    except Exception as e:
        logger.error(f"Fatal error during bot.run: {e}")
        await bot.close()
        raise
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(run_bot())
