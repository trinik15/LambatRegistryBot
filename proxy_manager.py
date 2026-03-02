# proxy_manager.py
import asyncio
import aiohttp
import sqlite3
import json
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import random

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ProxyManager")

class ProxyManager:
    def __init__(self, db_path="proxies.db"):
        self.db_path = db_path
        self._init_db()
        self.session = None
        self.lock = asyncio.Lock()
        self.running = True

    def _init_db(self):
        """Crea le tabelle necessarie."""
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

    async def fetch_proxy_sources(self):
        """Raccoglie proxy da varie fonti."""
        sources = [
            "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
            "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
            "https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list.txt",
        ]
        all_proxies = []
        for url in sources:
            try:
                async with self.session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        # Estrae IP:port da ogni riga
                        proxies = [line.strip() for line in text.splitlines() if line.strip() and ":" in line]
                        all_proxies.extend(proxies)
                        logger.info(f"Trovati {len(proxies)} proxy da {url}")
            except Exception as e:
                logger.warning(f"Errore nello scaricare {url}: {e}")
            await asyncio.sleep(1)  # delay tra le richieste
        return list(set(all_proxies))  # rimuove duplicati

    async def test_proxy(self, proxy: str) -> Optional[Dict]:
        """Testa un proxy: validità e ban da Discord."""
        test_url = "https://discord.com/api/v10/users/@me"
        headers = {"Authorization": "Bot fake_token"}  # token falso per test
        start = time.time()
        try:
            async with self.session.get(
                test_url,
                proxy=f"http://{proxy}",
                timeout=aiohttp.ClientTimeout(total=10),
                headers=headers,
                ssl=False  # per semplificare, ma attenzione
            ) as resp:
                latency = time.time() - start
                if resp.status == 401:
                    # Proxy valido e non bannato (risposta autorizzata fallita)
                    return {"proxy": proxy, "latency": latency, "banned": False}
                elif resp.status in (429, 403):
                    # Bannato da Discord
                    return {"proxy": proxy, "latency": latency, "banned": True}
                else:
                    # Altro codice (es. 500) -> probabilmente non funzionante
                    return None
        except Exception as e:
            logger.debug(f"Proxy {proxy} fallito: {e}")
            return None

    async def update_proxy_pool(self):
        """Ciclo principale: scarica nuovi proxy, testa e aggiorna DB."""
        logger.info("Avvio aggiornamento pool proxy...")
        new_proxies = await self.fetch_proxy_sources()
        if not new_proxies:
            logger.warning("Nessun proxy trovato dalle fonti.")
            return

        # Test in parallelo con limite di concorrenza
        semaphore = asyncio.Semaphore(20)
        async def test_with_semaphore(proxy):
            async with semaphore:
                return await self.test_proxy(proxy)

        tasks = [test_with_semaphore(p) for p in new_proxies[:200]]  # limitiamo a 200 per non esagerare
        results = await asyncio.gather(*tasks)

        valid = [r for r in results if r and not r["banned"]]
        banned = [r for r in results if r and r["banned"]]

        async with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                # Inserisci proxy validi
                for r in valid:
                    conn.execute("""
                        INSERT OR REPLACE INTO proxies (proxy, type, latency, last_tested, success_count)
                        VALUES (?, 'http', ?, ?, COALESCE((SELECT success_count FROM proxies WHERE proxy=?), 0) + 1)
                    """, (r["proxy"], r["latency"], datetime.now(), r["proxy"]))
                # Marca i bannati
                for r in banned:
                    conn.execute("""
                        INSERT OR REPLACE INTO blacklist (proxy, banned_until)
                        VALUES (?, ?)
                    """, (r["proxy"], datetime.now() + timedelta(hours=24)))
                    # Rimuovi dalla tabella proxy se presente
                    conn.execute("DELETE FROM proxies WHERE proxy = ?", (r["proxy"],))
                conn.commit()

        logger.info(f"Aggiornamento completato: {len(valid)} validi, {len(banned)} bannati.")

    async def get_next_proxy(self) -> Optional[str]:
        """Restituisce il miglior proxy disponibile (più veloce) e lo rimuove dalla coda."""
        async with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                # Cerca proxy non bannati, ordinati per latenza
                cur = conn.execute("""
                    SELECT proxy FROM proxies
                    WHERE (banned_until IS NULL OR banned_until < ?)
                    ORDER BY latency ASC LIMIT 1
                """, (datetime.now(),))
                row = cur.fetchone()
                if row:
                    proxy = row[0]
                    # Rimuoviamo temporaneamente? In realtà lo lasciamo, ma potremmo segnarlo come "in uso"
                    # Per semplicità, lo lasciamo; se fallisce verrà rimosso dopo
                    return proxy
                return None

    async def mark_proxy_failed(self, proxy: str):
        """Incrementa il contatore di fallimenti e banna se necessario."""
        async with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                # Incrementa fail_count
                conn.execute("""
                    UPDATE proxies SET fail_count = fail_count + 1
                    WHERE proxy = ?
                """, (proxy,))
                # Se fail_count supera una soglia, metti in blacklist
                cur = conn.execute("SELECT fail_count FROM proxies WHERE proxy = ?", (proxy,))
                row = cur.fetchone()
                if row and row[0] >= 3:
                    conn.execute("""
                        INSERT OR REPLACE INTO blacklist (proxy, banned_until)
                        VALUES (?, ?)
                    """, (proxy, datetime.now() + timedelta(hours=24)))
                    conn.execute("DELETE FROM proxies WHERE proxy = ?", (proxy,))
                conn.commit()

    async def run_periodic_update(self, interval=1800):
        """Esegue update periodico ogni 'interval' secondi."""
        while self.running:
            await self.update_proxy_pool()
            await asyncio.sleep(interval)
