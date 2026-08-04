"""Tests for services/backup.py — path-traversal safety + note sanitisation.

These are the security-critical branches: a malicious or mistyped ``note``
must never escape BACKUP_DIR, and the slug must be filename-safe + length-capped.
"""

import os

import pytest

from services import backup

# ---------------------------------------------------------------------------
# _sanitize_note
# ---------------------------------------------------------------------------


def test_sanitize_note_empty_returns_empty():
    assert backup._sanitize_note("") == ""
    assert backup._sanitize_note(None) == ""


def test_sanitize_note_already_safe():
    assert backup._sanitize_note("pre_release") == "pre_release"
    assert backup._sanitize_note("manual-2024") == "manual-2024"


def test_sanitize_note_replaces_path_separators():
    """A traversal attempt must collapse to a flat, safe slug."""
    slug = backup._sanitize_note("../etc/passwd")
    assert "/" not in slug
    assert ".." not in slug
    assert slug == "etc_passwd"


def test_sanitize_note_replaces_multiple_unsafe_runs():
    slug = backup._sanitize_note("a b!!c@d/e")
    assert slug == "a_b_c_d_e"


def test_sanitize_note_strips_leading_trailing_underscores():
    assert backup._sanitize_note("!!!hello!!!") == "hello"


def test_sanitize_note_caps_at_40_chars():
    long_note = "a" * 200
    slug = backup._sanitize_note(long_note)
    assert len(slug) == 40


# ---------------------------------------------------------------------------
# _safe_backup_path — the belt-and-suspenders traversal guard.
# ---------------------------------------------------------------------------


def test_safe_backup_path_accepts_plain_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path))
    path = backup._safe_backup_path("auto_20240101_000000.sql")
    assert os.path.commonpath(
        [os.path.abspath(str(tmp_path)), os.path.abspath(path)]
    ) == os.path.abspath(str(tmp_path))
    assert path.endswith("auto_20240101_000000.sql")


def test_safe_backup_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        backup._safe_backup_path("../../etc/cron.d/evil")


def test_safe_backup_path_rejects_absolute_path_outside(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        backup._safe_backup_path("/etc/passwd")


def test_safe_backup_path_rejects_double_dot_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        backup._safe_backup_path("../outside.sql")
