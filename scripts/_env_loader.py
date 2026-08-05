"""Shared .env loader for the scripts/ toolkit.

The scripts in this directory (preflight.py, seed.py) both need to load .env
without depending on python-dotenv. This module provides a single correct
implementation that handles:

  * Blank lines and full-line comments (``# foo``)
  * Inline comments (``KEY=value  # comment``) — stripped
  * Quoted values (``KEY="value with #"``) — ``#`` inside quotes preserved
  * Whitespace around key and value

It only sets variables that aren't already in the environment, so explicit
``DATABASE_URL=...`` on the command line takes precedence over .env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def load_env_file(env_path: Path | None = None) -> None:
    """Load .env into os.environ (setdefault — never overrides).

    Args:
        env_path: Path to the .env file. Defaults to <repo_root>/.env.
    """
    if env_path is None:
        env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # Skip blank lines and full-line comments.
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue

        value = _strip_inline_comment(value)
        os.environ.setdefault(key, value)


def _strip_inline_comment(value: str) -> str:
    """Remove an inline `` # comment`` from a value, respecting quotes.

    Examples:
        ``"5  # comment"``         → ``"5"``
        ``"hello world"``           → ``"hello world"``
        ``'"has # hash"'``          → ``"has # hash"``  (# inside quotes kept)
        ``"value  # c1  # c2"``     → ``"value"``

    The heuristic: walk the string tracking whether we're inside single or
    double quotes. The first unquoted `` #`` (space-hash) starts a comment.
    This matches the docker-compose / python-dotenv convention.
    """
    value = value.strip()
    in_single = False
    in_double = False
    for i in range(len(value)):
        ch = value[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif (
            ch == "#"
            and not in_single
            and not in_double
            and (i == 0 or value[i - 1] in (" ", "\t"))
        ):
            # A '#' starts a comment only if preceded by whitespace or at
            # the start of the value. ``foo#bar`` is NOT a comment.
            value = value[:i].rstrip()
            break

    # Strip surrounding quotes (single or double, matched pair only).
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")
    ):
        value = value[1:-1]
    return value
