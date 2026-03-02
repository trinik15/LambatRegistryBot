import discord
from discord.ext import commands, tasks
import logging
import os
import aiohttp
import traceback
import asyncio
from datetime import datetime, timedelta
import sys
from collections import deque
import time

from core.config import Config
from core import database as db
from services import backup
from tasks.activity_monitor import ActivityMonitor
# from web.http_keepalive import start_http_server  # Disabilitato per evitare conflitti

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('pavia_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Variabili per il monitoraggio dei rate limit
rate_limit_errors = deque(maxlen=10)
last_rate_limit_reset = time.time()

def check_rate_limit_exit():
    """Se troppi 429 in poco tempo, esce con codice 429."""
    global rate_limit_errors, last_rate_limit_reset
    now = time.time()
    # Resetta se è passata più di un'ora
    if now - last_rate_limit_reset > 3600:
        rate_limit_errors.clear()
        last_rate_limit_reset = now
    # Se più di 3 errori in 10 minuti, esci
    if len(rate_limit_errors) >= 3 and (now - rate_limit_errors[0]) < 600:
        logger.critical("Troppi rate limit (429) in breve tempo. Uscita per cambio proxy.")
        sys.exit(429)

class PaviaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        # Legge il proxy dalle variabili d'ambiente (se presente)
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
        self.http_session = aiohttp.ClientSession()
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
        if isinstance(error, discord.HTTPException) and error.status == 429:
            # Registra l'errore per il monitoraggio
            rate_limit_errors.append(time.time())
            check_rate_limit_exit()
            embed = discord.Embed(
                title="⏳ Troppe richieste",
                description="Il bot ha raggiunto il limite di richieste a Discord. Attendi qualche secondo e riprova.",
                color=0xff9900
            )
        else:
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
    """Avvia il bot con retry in caso di rate limiting."""
    max_retries = 5
    retry_delay = 5

    for attempt in range(max_retries):
        bot = PaviaBot()
        try:
            await bot.start(Config.DISCORD_TOKEN)
            await bot.wait_until_ready()
            logger.info(f"Logged in as {bot.user}")
            bot.daily_backup.start()
            bot.activity_monitor.daily_check.start()
            print(f"✅ Bot online as {bot.user}")
            await asyncio.Future()  # Attende indefinitamente
        except discord.HTTPException as e:
            if e.status == 429:
                if attempt < max_retries - 1:
                    logger.warning(f"Rate limited (429) during login. Retrying in {retry_delay} seconds... (attempt {attempt+1}/{max_retries})")
                    await bot.close()
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.critical("Login fallito per 429 dopo tutti i tentativi. Uscita con codice 429.")
                    await bot.close()
                    sys.exit(429)  # Esce con codice 429 per il supervisor
            else:
                logger.error(f"Fatal HTTP error during login: {e}")
                await bot.close()
                raise
        except Exception as e:
            logger.error(f"Unexpected error during bot.run: {e}")
            await bot.close()
            raise
        finally:
            await bot.close()

if __name__ == "__main__":
    # start_http_server()  # Disabilitato per evitare conflitti con riavvii frequenti
    asyncio.run(run_bot())
