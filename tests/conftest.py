"""Shared pytest fixtures + environment bootstrap.

IMPORTANT: ``core.config`` validates required env vars on import and raises
``ValueError`` if they're missing. Many modules import ``core.config``
(transitively), so we MUST set dummy values BEFORE any test module is
collected. pytest loads this conftest first, so setting env here is enough.
"""

import os
import sys
from pathlib import Path

# --- Make the repo root importable (so `import core.config` works in tests) ---
# conftest.py lives in tests/, so the repo root is one level up.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# --- Dummy env vars for Config validation + test isolation -----------------
# These are set with setdefault so a developer can override them if needed.
os.environ.setdefault("DISCORD_TOKEN", "test-dummy-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/lambat_test")
os.environ.setdefault("OWNER_ID", "100000000000000001")
os.environ.setdefault("CITIZEN_ROLE_IDS", "111111111111111111,222222222222222222")
os.environ.setdefault("FULL_ACCESS_ROLE_IDS", "333333333333333333")
os.environ.setdefault("VIEW_ACCESS_ROLE_ID", "444444444444444444")
# Keep test backups in a tmp dir so we never touch real backups.
os.environ.setdefault("BACKUP_DIR", "/tmp/lambat_test_backups")
# Suppress noisy alert-channel warnings during uptime monitor tests.
os.environ.setdefault("ALERT_CHANNEL_ID", "0")

import pytest  # noqa: E402


@pytest.fixture
def tmp_backup_dir(tmp_path, monkeypatch):
    """Point BACKUP_DIR at a fresh tmp dir and (re)create it.

    Tests that create backups should use this so they never collide with real
    backup files or with each other. Note: Config.BACKUP_DIR is read at import
    time, so modules that captured it already (e.g. services/backup.py) won't
    see the patched value — those tests call the path-safe helpers directly
    instead of the full create_backup flow.
    """
    d = tmp_path / "backups"
    d.mkdir()
    monkeypatch.setenv("BACKUP_DIR", str(d))
    return d
