# supervisor.py
import asyncio
import subprocess
import sys
import os
import signal
import logging
from proxy_manager import ProxyManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Supervisor")

class BotSupervisor:
    def __init__(self):
        self.proxy_manager = ProxyManager()
        self.current_proxy = None
        self.process = None
        self.restart_delay = 1  # secondi iniziali

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
        # Opzionale: log in tempo reale
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
            logger.error(f"[BOT] {line.decode().strip()}")

    async def run(self):
        """Ciclo principale del supervisor."""
        async with self.proxy_manager:
            # Avvia aggiornamento periodico dei proxy in background
            asyncio.create_task(self.proxy_manager.run_periodic_update())

            while True:
                # Ottieni un proxy
                self.current_proxy = await self.proxy_manager.get_next_proxy()
                if not self.current_proxy:
                    logger.warning("Nessun proxy disponibile. Attendo 60 secondi...")
                    await asyncio.sleep(60)
                    continue

                await self.start_bot(self.current_proxy)
                # Attendi la terminazione del processo
                return_code = await self.process.wait()

                if return_code == 429:
                    logger.error(f"Proxy {self.current_proxy} bannato (429).")
                    await self.proxy_manager.mark_proxy_failed(self.current_proxy)
                    self.restart_delay = 1  # reset delay
                elif return_code == 0:
                    logger.info("Bot terminato volontariamente. Supervisor termina.")
                    break
                else:
                    logger.error(f"Bot crashato con codice {return_code}. Riavvio con stesso proxy tra {self.restart_delay}s")
                    await asyncio.sleep(self.restart_delay)
                    self.restart_delay = min(self.restart_delay * 2, 60)  # backoff
                    # Non marcamo il proxy come fallito, potrebbe essere un bug

                # Piccola pausa prima di riavviare
                await asyncio.sleep(2)

if __name__ == "__main__":
    supervisor = BotSupervisor()
    asyncio.run(supervisor.run())
