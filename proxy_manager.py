import asyncio
import aiohttp
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ProxyManager")

# Lista statica di proxy noti (fallback se le fonti online falliscono)
FALLBACK_PROXIES = [
    "45.190.78.20:999",
    "160.20.55.235:8080",
    "45.4.202.144:999",
]

class ProxyManager:
    def __init__(self, db_path="proxies.db"):
        self.db_path = db_path
        self._init_db()
        self.session = None
        self.lock = asyncio.Lock()
        self.running = True

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proxies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proxy TEXT UNIQUE,
                    type TEXT,
                    latency REAL,
                    last_tested TIMESTAMP,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    banned_until TIMESTAMP NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blacklist (
                    proxy TEXT PRIMARY KEY,
                    banned_until TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proxies_banned ON proxies(banned_until)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proxies_latency ON proxies(latency)")

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def fetch_clearproxy_list(self):
        """Scarica la lista di proxy già testati per Discord da ClearProxy."""
        url = "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/custom/discord/http.txt"
        try:
            async with self.session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    proxies = [line.strip() for line in text.splitlines() if line.strip() and ":" in line]
                    logger.info(f"Trovati {len(proxies)} proxy validi per Discord da ClearProxy")
                    return proxies
                else:
                    logger.warning(f"ClearProxy list returned status {resp.status}")
                    return []
        except Exception as e:
            logger.warning(f"Errore nel scaricare lista ClearProxy: {e}")
            return []

    async def fetch_proxy_sources(self):
        """Raccoglie proxy da ClearProxy (fonte principale) e da altre fonti come fallback."""
        # Fonte principale: ClearProxy
        main_proxies = await self.fetch_clearproxy_list()

        # Fonti secondarie (per aumentare il pool)
        other_sources = [
            "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
            "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        ]
        all_proxies = set(main_proxies)  # partiamo dai proxy di ClearProxy
        for url in other_sources:
            try:
                async with self.session.get(url, timeout=15) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        proxies = [line.strip() for line in text.splitlines() if line.strip() and ":" in line]
                        all_proxies.update(proxies)
                        logger.info(f"Trovati {len(proxies)} proxy da {url}")
            except Exception as e:
                logger.warning(f"Errore nello scaricare {url}: {e}")
            await asyncio.sleep(1)

        # Aggiungi i fallback statici
        for p in FALLBACK_PROXIES:
            all_proxies.add(p)

        return list(all_proxies)

    async def test_proxy(self, proxy: str) -> Optional[Dict]:
        """Test rapido con Discord (timeout 5 secondi)."""
        discord_url = "https://discord.com/api/v10/users/@me"
        headers = {"Authorization": "Bot fake_token"}
        try:
            start = time.time()
            async with self.session.get(
                discord_url,
                proxy=f"http://{proxy}",
                timeout=aiohttp.ClientTimeout(total=5),
                headers=headers,
                ssl=False
            ) as resp:
                latency = time.time() - start
                if resp.status == 401:
                    return {"proxy": proxy, "latency": latency, "banned": False}
                elif resp.status in (429, 403):
                    return {"proxy": proxy, "latency": latency, "banned": True}
                else:
                    return None
        except Exception as e:
            logger.debug(f"Proxy {proxy} fallito: {e}")
            return None

    async def update_proxy_pool(self):
        logger.info("Avvio aggiornamento pool proxy...")
        new_proxies = await self.fetch_proxy_sources()
        if not new_proxies:
            logger.warning("Nessun proxy trovato dalle fonti.")
            return

        # Concorrenza alta perché la lista è più piccola e i test sono rapidi
        semaphore = asyncio.Semaphore(50)
        async def test_with_semaphore(proxy):
            async with semaphore:
                return await self.test_proxy(proxy)

        tasks = [test_with_semaphore(p) for p in new_proxies]
        results = await asyncio.gather(*tasks)

        valid = [r for r in results if r and not r["banned"]]
        banned = [r for r in results if r and r["banned"]]

        async with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                for r in valid:
                    conn.execute("""
                        INSERT OR REPLACE INTO proxies (proxy, type, latency, last_tested, success_count)
                        VALUES (?, 'http', ?, ?, COALESCE((SELECT success_count FROM proxies WHERE proxy=?), 0) + 1)
                    """, (r["proxy"], r["latency"], datetime.now(), r["proxy"]))
                for r in banned:
                    conn.execute("""
                        INSERT OR REPLACE INTO blacklist (proxy, banned_until)
                        VALUES (?, ?)
                    """, (r["proxy"], datetime.now() + timedelta(hours=24)))
                    conn.execute("DELETE FROM proxies WHERE proxy = ?", (r["proxy"],))
                conn.commit()

        logger.info(f"Aggiornamento completato: {len(valid)} validi, {len(banned)} bannati.")

    async def get_next_proxy(self) -> Optional[str]:
        async with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("""
                    SELECT proxy FROM proxies
                    WHERE (banned_until IS NULL OR banned_until < ?)
                    ORDER BY latency ASC LIMIT 1
                """, (datetime.now(),))
                row = cur.fetchone()
                return row[0] if row else None

    async def mark_proxy_failed(self, proxy: str):
        async with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE proxies SET fail_count = fail_count + 1 WHERE proxy = ?", (proxy,))
                cur = conn.execute("SELECT fail_count FROM proxies WHERE proxy = ?", (proxy,))
                row = cur.fetchone()
                if row and row[0] >= 3:
                    conn.execute("""
                        INSERT OR REPLACE INTO blacklist (proxy, banned_until)
                        VALUES (?, ?)
                    """, (proxy, datetime.now() + timedelta(hours=24)))
                    conn.execute("DELETE FROM proxies WHERE proxy = ?", (proxy,))
                conn.commit()

    async def run_periodic_update(self, interval=600):  # ogni 10 minuti
        while self.running:
            await self.update_proxy_pool()
            await asyncio.sleep(interval)
