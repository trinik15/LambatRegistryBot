"""Tests for scripts._env_loader — the .env parser used by preflight + seed.

Covers the bug Cursor AI hit on Windows: inline comments like
``COOLDOWN_FAST=5  # comment`` were not being stripped, so int() failed on
the value ``"5  # comment"``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the scripts/ directory importable.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _env_loader import _strip_inline_comment, load_env_file  # noqa: E402


class TestStripInlineComment:
    """Unit tests for the inline-comment stripper."""

    def test_plain_value_no_comment(self) -> None:
        assert _strip_inline_comment("hello world") == "hello world"

    def test_inline_comment_with_spaces(self) -> None:
        assert _strip_inline_comment("5  # comment") == "5"

    def test_inline_comment_single_space(self) -> None:
        assert _strip_inline_comment("5 # c") == "5"

    def test_no_space_before_hash_is_not_comment(self) -> None:
        # ``foo#bar`` is NOT a comment — # must be preceded by whitespace.
        assert _strip_inline_comment("foo#bar") == "foo#bar"

    def test_hash_at_start_is_comment(self) -> None:
        assert _strip_inline_comment("# comment") == ""

    def test_double_quoted_value_with_hash_preserved(self) -> None:
        assert _strip_inline_comment('"has # hash"') == "has # hash"

    def test_single_quoted_value_with_hash_preserved(self) -> None:
        assert _strip_inline_comment("'has # hash'") == "has # hash"

    def test_double_quotes_stripped_around_value(self) -> None:
        assert _strip_inline_comment('"hello"') == "hello"

    def test_single_quotes_stripped_around_value(self) -> None:
        assert _strip_inline_comment("'hello'") == "hello"

    def test_mixed_inline_comment_after_quotes(self) -> None:
        assert _strip_inline_comment('"5"  # comment') == "5"

    def test_multiple_comments_only_first_matters(self) -> None:
        assert _strip_inline_comment("value  # c1  # c2") == "value"

    def test_empty_value(self) -> None:
        assert _strip_inline_comment("") == ""

    def test_value_with_trailing_spaces(self) -> None:
        assert _strip_inline_comment("value   ") == "value"

    def test_url_with_query_string_no_comment(self) -> None:
        # URL with query params — the ? and & are not comments.
        url = "https://api.example.com/v3?key=abc&format=json"
        assert _strip_inline_comment(url) == url

    def test_url_with_inline_comment(self) -> None:
        url = "https://api.example.com/v3  # comment"
        assert _strip_inline_comment(url) == "https://api.example.com/v3"

    def test_postgres_url_with_password(self) -> None:
        url = "postgresql://user:pass@host:5432/db"
        assert _strip_inline_comment(url) == "postgresql://user:pass@host:5432/db"


class TestLoadEnvFile:
    """Integration tests for the full .env loader."""

    def test_loads_simple_key_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        monkeypatch.delenv("FOO", raising=False)
        load_env_file(env)
        assert os.environ["FOO"] == "bar"

    def test_strips_inline_comment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env = tmp_path / ".env"
        env.write_text("COOLDOWN_FAST=5  # Quick view/list commands\n")
        monkeypatch.delenv("COOLDOWN_FAST", raising=False)
        load_env_file(env)
        assert os.environ["COOLDOWN_FAST"] == "5"

    def test_skips_blank_lines(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env = tmp_path / ".env"
        env.write_text("\n\nFOO=bar\n\n")
        monkeypatch.delenv("FOO", raising=False)
        load_env_file(env)
        assert os.environ["FOO"] == "bar"

    def test_skips_full_line_comments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("# This is a comment\nFOO=bar\n# Another\n")
        monkeypatch.delenv("FOO", raising=False)
        load_env_file(env)
        assert os.environ["FOO"] == "bar"
        assert "This is a comment" not in set(os.environ)

    def test_does_not_override_existing_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("FOO=from_file\n")
        monkeypatch.setenv("FOO", "from_env")
        load_env_file(env)
        # setdefault — existing env var wins.
        assert os.environ["FOO"] == "from_env"

    def test_handles_multiple_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "TOKEN=abc123\nGUILD_ID=123456\nCOOLDOWN_FAST=5  # quick\nCOOLDOWN_SLOW=60  # slow\n"
        )
        for k in ("TOKEN", "GUILD_ID", "COOLDOWN_FAST", "COOLDOWN_SLOW"):
            monkeypatch.delenv(k, raising=False)
        load_env_file(env)
        assert os.environ["TOKEN"] == "abc123"
        assert os.environ["GUILD_ID"] == "123456"
        assert os.environ["COOLDOWN_FAST"] == "5"
        assert os.environ["COOLDOWN_SLOW"] == "60"

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        # Should not raise.
        load_env_file(tmp_path / "nonexistent.env")

    def test_quoted_value_with_spaces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text('NOTES="hello world  # not a comment"\n')
        monkeypatch.delenv("NOTES", raising=False)
        load_env_file(env)
        assert os.environ["NOTES"] == "hello world  # not a comment"

    def test_real_env_example_subset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reproduce the exact pattern from .env.example that broke Cursor."""
        env = tmp_path / ".env"
        env.write_text(
            "COOLDOWN_FAST=5          # Quick view/list commands\n"
            "COOLDOWN_MEDIUM=15       # Data modification commands\n"
            "COOLDOWN_SLOW=60         # Expensive operations (reports, exports)\n"
            "COOLDOWN_CRITICAL=120    # Very expensive ops (backup, restore)\n"
            "BACKUP_RETENTION_DAILY=30\n"
        )
        for k in (
            "COOLDOWN_FAST",
            "COOLDOWN_MEDIUM",
            "COOLDOWN_SLOW",
            "COOLDOWN_CRITICAL",
            "BACKUP_RETENTION_DAILY",
        ):
            monkeypatch.delenv(k, raising=False)
        load_env_file(env)
        # The bug: these would be "5          # Quick view/list commands" etc.
        # and int() would fail. Now they should be clean integers.
        assert int(os.environ["COOLDOWN_FAST"]) == 5
        assert int(os.environ["COOLDOWN_MEDIUM"]) == 15
        assert int(os.environ["COOLDOWN_SLOW"]) == 60
        assert int(os.environ["COOLDOWN_CRITICAL"]) == 120
        assert int(os.environ["BACKUP_RETENTION_DAILY"]) == 30
