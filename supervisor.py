import asyncio
import subprocess
import sys
import os
import logging
import threading
import http.server
import socketserver
from proxy_manager import ProxyManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Supervisor")

PORT = int(os.environ.get('PORT', 10000))

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Supervisor is running.")
    def log_message(self, format, *args):
        pass

def run_http_server():
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()

threading.Thread(target=run_http_server, daemon=True).start()

class BotSupervisor:
    def __init__(self):
        self.proxy_manager = ProxyManager()
        self.current_proxy = None
        self.process = None
        self.restart_delay = 1
        self.proxy_failures = {}
        self.login_detected = asyncio.Event()

    async def start_bot(self, proxy):
        env = os.environ.copy()
        env["PROXY_URL"] = f"http://{proxy}"
        logger.info(f"Avvio bot con proxy {proxy}")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "main.py",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        self.login_detected.clear()
        asyncio.create_task(self.monitor_output())
        try:
            await asyncio.wait_for(self.login_detected.wait(), timeout=90)  # aumentato a 90 secondi
            logger.info(f"Bot con proxy {proxy} ha effettuato il login con successo.")
            return_code = await self.process.wait()
            return return_code
        except asyncio.TimeoutError:
            logger.error(f"Bot con proxy {proxy} non ha effettuato il login entro 90s. Terminazione.")
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except:
                    self.process.kill()
                    await self.process.wait()
            return 1

    async def monitor_output(self):
        async def read_stream(stream, name):
            while True:
                line = await stream.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if name == 'stdout':
                    logger.info(f"[BOT] {line_str}")
                    # Cerchiamo la stringa effettivamente stampata dal bot
                    if "Logged in as" in line_str or "Bot online as" in line_str:
                        self.login_detected.set()
                else:
                    logger.info(f"[BOT] {line_str}")
        await asyncio.gather(
            read_stream(self.process.stdout, 'stdout'),
            read_stream(self.process.stderr, 'stderr')
        )

    async def run(self):
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

                return_code = await self.start_bot(self.current_proxy)

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
