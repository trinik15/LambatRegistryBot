import os
import re
import asyncio
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse
from core.config import Config
from services.backup_sinks import upload_to_sink
import logging

logger = logging.getLogger(__name__)

BACKUP_DIR = Config.BACKUP_DIR
DATABASE_URL = Config.DATABASE_URL

# Hard cap on how long pg_dump / psql may run before we give up and abort.
# Prevents a wedged DB connection from hanging the backup/restore task forever.
PG_TIMEOUT = int(os.getenv("PG_TIMEOUT", 180))

# Allow only filename-safe characters in the user-supplied backup "note" so it
# can never escape BACKUP_DIR via path traversal (e.g. "../../etc/cron.d/x").
_NOTE_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _sanitize_note(note: str) -> str:
    """Reduce a free-form note to a filename-safe slug (max 40 chars)."""
    if not note:
        return ""
    slug = _NOTE_SAFE.sub("_", note).strip("_")
    return slug[:40]


def _safe_backup_path(filename: str) -> str:
    """Build a backup path and guarantee it stays inside BACKUP_DIR.

    Belt-and-suspenders: even if a caller passes a crafted filename, we refuse
    to write outside BACKUP_DIR by resolving both paths and checking the backup
    path is a descendant of BACKUP_DIR.
    """
    base = os.path.abspath(BACKUP_DIR)
    target = os.path.abspath(os.path.join(base, filename))
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"Refusing to write backup outside {BACKUP_DIR}: {filename!r}")
    return target


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
    """Create a database backup using pg_dump."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_note = f"_{_sanitize_note(note)}" if note else ""
    filename = f"{backup_type}_{timestamp}{safe_note}.sql"
    backup_path = _safe_backup_path(filename)

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
            # --no-owner / --no-privileges make the dump portable across DB
            # users (the restore user often differs from the dump user, which
            # would otherwise fail on every OWNER / ACL clause).
            "--no-owner",
            "--no-privileges",
            "--file", backup_path
        ]
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True,
                           env=env, timeout=PG_TIMEOUT)
            with open(backup_path + ".meta", "w") as f:
                f.write(f"type={backup_type}\nnote={note}\ndate={timestamp}")
            logger.info(f"Backup creato: {filename}, size: {os.path.getsize(backup_path)} bytes")
            logger.info(f"Files in BACKUP_DIR after write: {os.listdir(BACKUP_DIR)}")
        except subprocess.TimeoutExpired:
            logger.error(f"pg_dump timed out after {PG_TIMEOUT}s")
            # Clean up a possibly-partial file so list_backups won't list it.
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
            raise
        except subprocess.CalledProcessError as e:
            logger.error(f"pg_dump fallito: {e.stderr}")
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
            raise

    await asyncio.to_thread(_sync_dump)

    # Off-site upload is best-effort: the local copy is the authoritative safety
    # net, so a sink failure must NOT fail the whole backup. upload_to_sink
    # logs loudly on failure and returns None; the .meta is uploaded too so the
    # off-site copy is self-describing.
    await upload_to_sink(backup_path)
    await upload_to_sink(backup_path + ".meta")

    # Prune old auto backups so the disk doesn't fill forever. Manual and
    # pre_restore backups are never pruned; monthly backups are preserved.
    try:
        await prune_backups(Config.BACKUP_RETENTION_DAILY)
    except Exception as e:
        # Pruning must never break a successful backup.
        logger.warning(f"Backup retention pruning failed (non-fatal): {e}", exc_info=True)

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


async def prune_backups(keep_daily: int = 30):
    """Delete old ``auto`` backups beyond ``keep_daily``, newest-first.

    Rules (so we never lose data an operator would want):
      * ``auto`` backups with note == "monthly" (generated on the 1st) are
        NEVER pruned — they're the long-term trend history.
      * ``manual`` and ``pre_restore`` backups are NEVER pruned.
      * All other ``auto`` backups are kept newest-first up to ``keep_daily``;
        the rest are deleted (both the ``.sql`` and the ``.sql.meta``).

    ``list_backups`` already returns newest-first, so the slice is trivial.
    """
    if keep_daily < 1:
        return

    backups = await list_backups()
    if not backups:
        return

    # Candidate auto backups that are NOT monthly. (list_backups parses the
    # .meta `note` field; "monthly" is set by main.py on the 1st of the month.)
    candidates = [
        b for b in backups
        if b.get("type") == "auto" and b.get("note") != "monthly"
    ]
    # newest-first already; keep the first `keep_daily`, prune the rest.
    to_prune = candidates[keep_daily:]
    if not to_prune:
        return

    pruned = 0
    for b in to_prune:
        for suffix in ("", ".meta"):
            path = _safe_backup_path(b["filename"] + suffix)
            try:
                os.remove(path)
                pruned += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning(f"Could not prune {path}: {e}")
    logger.info(
        f"Pruned {len(to_prune)} old auto backup(s) "
        f"(retention={keep_daily} daily, monthly/manual/pre_restore preserved); "
        f"{pruned} file(s) removed."
    )

async def restore_backup(filename):
    """Ripristina il database da un file di backup SQL.

    Raises RuntimeError if the restore cannot be completed (file missing,
    pg_dump/psql failure, or timeout). Returning a bool previously let the
    caller silently report success on a failed restore, so we now raise.
    """
    backup_path = _safe_backup_path(filename)
    if not os.path.exists(backup_path):
        logger.error(f"Backup file non trovato: {backup_path}")
        raise RuntimeError(f"Backup file not found: {filename}")

    user, password, host, port, dbname = _parse_db_url(DATABASE_URL)

    def _sync_restore():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        emergency_filename = f"pre_restore_{timestamp}_before_restore.sql"
        emergency_path = _safe_backup_path(emergency_filename)
        env = os.environ.copy()
        env["PGPASSWORD"] = password

        # Backup di emergenza (safety net so a bad restore is reversible).
        try:
            cmd_dump = [
                "pg_dump",
                "--host", host,
                "--port", str(port),
                "--username", user,
                "--dbname", dbname,
                "--no-owner",
                "--no-privileges",
                "--file", emergency_path
            ]
            subprocess.run(cmd_dump, check=True, capture_output=True, text=True,
                           env=env, timeout=PG_TIMEOUT)
            logger.info(f"Backup di emergenza creato: {emergency_filename}")
        except subprocess.TimeoutExpired:
            logger.error(f"Emergency pg_dump timed out after {PG_TIMEOUT}s")
            raise
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
            subprocess.run(cmd_restore, check=True, capture_output=True, text=True,
                           env=env, timeout=PG_TIMEOUT)
            logger.info(f"Ripristino da {filename} completato.")
            return True
        except subprocess.TimeoutExpired:
            logger.error(f"psql restore timed out after {PG_TIMEOUT}s")
            raise
        except subprocess.CalledProcessError as e:
            logger.error(f"Ripristino fallito: {e.stderr}")
            raise

    return await asyncio.to_thread(_sync_restore)
