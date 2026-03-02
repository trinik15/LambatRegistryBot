import asyncio
import subprocess
import sys
import os
import logging
from proxy_manager import ProxyManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Supervisor")

class BotSupervisor:
    def __init__(self):
        self.proxy_manager = ProxyManager()
        self.current_proxy = None
        self.process = None
        self.restart_delay = 1
        self.proxy_failures = {}  # proxy -> numero di fallimenti consecutivi

    async def start_bot(self, proxy):
        """Avvia il bot con un proxy specifico."""
        env = os.environ.copy()
        env["PROXY_URL"] = f"http://{proxy}"
        logger.info(f"Avvio bot con proxy {proxy}")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "main.py",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        asyncio.create_task(self.log_output())

    async def log_output(self):
        """Legge stdout/stderr e li logga."""
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            logger.info(f"[BOT] {line.decode().strip()}")
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            logger.info(f"[BOT] {line.decode().strip()}")  # Usiamo INFO per evitare confusione

    async def run(self):
        """Ciclo principale del supervisor."""
        async with self.proxy_manager:
            asyncio.create_task(self.proxy_manager.run_periodic_update())

            while True:
                self.current_proxy = await self.proxy_manager.get_next_proxy()
                if not self.current_proxy:
                    logger.warning("Nessun proxy disponibile. Attendo 60 secondi...")
                    await asyncio.sleep(60)
                    continue

                if self.current_proxy not in self.proxy_failures:
                    self.proxy_failures[self.current_proxy] = 0

                await self.start_bot(self.current_proxy)
                return_code = await self.process.wait()

                if return_code == 429:
                    logger.error(f"Proxy {self.current_proxy} bannato (429).")
                    await self.proxy_manager.mark_proxy_failed(self.current_proxy)
                    self.proxy_failures.pop(self.current_proxy, None)
                    self.restart_delay = 1
                elif return_code == 0:
                    logger.info("Bot terminato volontariamente. Supervisor termina.")
                    break
                else:
                    self.proxy_failures[self.current_proxy] += 1
                    logger.error(f"Bot crashato con codice {return_code} per proxy {self.current_proxy} (tentativo {self.proxy_failures[self.current_proxy]})")

                    if self.proxy_failures[self.current_proxy] >= 2:
                        logger.warning(f"Proxy {self.current_proxy} fallito 2 volte consecutivamente. Rimozione.")
                        await self.proxy_manager.mark_proxy_failed(self.current_proxy)
                        self.proxy_failures.pop(self.current_proxy, None)
                        self.restart_delay = 1
                    else:
                        logger.info(f"Riavvio con stesso proxy tra {self.restart_delay}s")
                        await asyncio.sleep(self.restart_delay)
                        self.restart_delay = min(self.restart_delay * 2, 60)
                        continue

                await asyncio.sleep(2)

if __name__ == "__main__":
    supervisor = BotSupervisor()
    asyncio.run(supervisor.run())
