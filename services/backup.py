import os
import shutil
import asyncio
import subprocess
from datetime import datetime
from urllib.parse import urlparse
from core.config import Config
import logging

logger = logging.getLogger(__name__)

BACKUP_DIR = Config.BACKUP_DIR
DATABASE_URL = Config.DATABASE_URL

os.makedirs(BACKUP_DIR, exist_ok=True)

def _parse_db_url(url):
    """Parsa DATABASE_URL e restituisce (user, password, host, port, dbname)."""
    parsed = urlparse(url)
    user = parsed.username
    password = parsed.password
    host = parsed.hostname
    port = parsed.port or 5432
    dbname = parsed.path.lstrip('/')
    return user, password, host, port, dbname

async def create_backup(backup_type="manual", note=""):
    """Crea un backup del database usando pg_dump."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_note = f"_{note}" if note else ""
    filename = f"{backup_type}_{timestamp}{safe_note}.sql"
    backup_path = os.path.join(BACKUP_DIR, filename)

    user, password, host, port, dbname = _parse_db_url(DATABASE_URL)

    def _sync_dump():
        cmd = [
            "pg_dump",
            "--host", host,
            "--port", str(port),
            "--username", user,
            "--dbname", dbname,
            "--clean",
            "--if-exists",
            "--file", backup_path
        ]
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
            with open(backup_path + ".meta", "w") as f:
                f.write(f"type={backup_type}\nnote={note}\ndate={timestamp}")
            logger.info(f"Backup creato: {filename}, size: {os.path.getsize(backup_path)} bytes")
            if os.path.exists(backup_path):
                logger.info(f"Backup file exists after write: {backup_path}")
            else:
                logger.error(f"Backup file does NOT exist after write: {backup_path}")
            logger.info(f"Files in BACKUP_DIR after write: {os.listdir(BACKUP_DIR)}")
        except subprocess.CalledProcessError as e:
            logger.error(f"pg_dump fallito: {e.stderr}")
            raise

    await asyncio.to_thread(_sync_dump)
    return filename

async def list_backups():
    """Restituisce la lista dei backup (file .sql)."""
    logger.info(f"Listing backups in directory: {BACKUP_DIR}")
    try:
        files = os.listdir(BACKUP_DIR)
        logger.info(f"All files in backup dir: {files}")
    except Exception as e:
        logger.error(f"Error listing directory: {e}")
        return []

    backups = []
    for f in files:
        if f.endswith(".sql") and not f.endswith(".meta"):
            full = os.path.join(BACKUP_DIR, f)
            try:
                mtime = os.path.getmtime(full)
                size = os.path.getsize(full)
                meta_path = full + ".meta"
                if os.path.exists(meta_path):
                    with open(meta_path) as mf:
                        meta = dict(line.strip().split("=", 1) for line in mf if "=" in line)
                else:
                    meta = {}
                backups.append({
                    "filename": f,
                    "type": meta.get("type", "unknown"),
                    "note": meta.get("note", ""),
                    "created": datetime.fromtimestamp(mtime),
                    "size": size
                })
                logger.debug(f"Valid backup file: {f}, type: {meta.get('type')}")
            except Exception as e:
                logger.error(f"Errore nel leggere il file di backup {f}: {e}")
    backups.sort(key=lambda x: x["created"], reverse=True)
    logger.info(f"Returning {len(backups)} backups")
    return backups

async def restore_backup(filename):
    """Ripristina il database da un file di backup SQL."""
    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path):
        logger.error(f"Backup file non trovato: {backup_path}")
        return False

    user, password, host, port, dbname = _parse_db_url(DATABASE_URL)

    def _sync_restore():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        emergency_filename = f"pre_restore_{timestamp}_before_restore.sql"
        emergency_path = os.path.join(BACKUP_DIR, emergency_filename)
        env = os.environ.copy()
        env["PGPASSWORD"] = password

        # Backup di emergenza
        try:
            cmd_dump = [
                "pg_dump",
                "--host", host,
                "--port", str(port),
                "--username", user,
                "--dbname", dbname,
                "--file", emergency_path
            ]
            subprocess.run(cmd_dump, check=True, capture_output=True, text=True, env=env)
            logger.info(f"Backup di emergenza creato: {emergency_filename}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Backup di emergenza fallito: {e.stderr}")
            raise

        # Restore
        cmd_restore = [
            "psql",
            "--host", host,
            "--port", str(port),
            "--username", user,
            "--dbname", dbname,
            "--file", backup_path
        ]
        try:
            subprocess.run(cmd_restore, check=True, capture_output=True, text=True, env=env)
            logger.info(f"Ripristino da {filename} completato.")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Ripristino fallito: {e.stderr}")
            return False

    try:
        success = await asyncio.to_thread(_sync_restore)
        return success
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        return False
