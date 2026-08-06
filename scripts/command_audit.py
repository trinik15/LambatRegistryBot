#!/usr/bin/env python3
"""Discord slash-command sync audit for the E2E harness.

Connects to Discord via the REST API (no gateway session needed beyond the
initial IDENTIFY) using the bot token, fetches the guild's registered slash
commands, and compares them against the EXPECTED_COMMANDS inventory below
(which mirrors every ``@app_commands.command`` / ``Group`` in ``cogs/``).

This catches the most common "why doesn't my command show up in Discord?"
regressions:

  * A cog failed to load (its commands never got added to the tree).
  * The guild sync silently dropped a command (rare, but happens on rate-limit).
  * A new command was added to a cog but never shipped.

Exit codes: 0 = all expected commands present, 1 = at least one missing,
2 = could not connect to Discord (token / network / guild issue).

Usage:
    python scripts/command_audit.py
    python scripts/command_audit.py --json

Requires DISCORD_TOKEN + GUILD_ID in .env (loaded automatically).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import discord  # noqa: E402

from scripts._env_loader import load_env_file  # noqa: E402

# --- Expected command inventory -------------------------------------------
# Mirrors every @app_commands.command / <group>.command in cogs/. Update this
# when a new command is added so the audit catches a missed sync.
#
# Format: "top_level_command" -> [] (standalone) or ["sub1", "sub2", ...]
# (for app_commands.Group entries, the subcommands are nested options).
EXPECTED_COMMANDS: dict[str, list[str]] = {
    # Standalone commands
    "help": [],
    "sync": [],
    "apply": [],
    # /citizen group
    "citizen": ["add", "update", "remove", "list", "dossier", "recruited-by", "search", "import"],
    # /settlement group
    "settlement": ["add", "remove", "list", "info"],
    # /data group
    "data": ["backup", "list_backups", "restore"],
    # /report group
    "report": ["census", "stats", "trends", "export", "activity", "recruiters"],
    # /audit group
    "audit": ["search"],
    # /emoji group
    "emoji": ["set", "list"],
    # /server group
    "server": ["status", "ping", "online", "trends"],
    # /snapshot group (Phase 4.6)
    "snapshot": ["annotate", "list", "clear"],
    # /factory group (Phase B / WS-6)
    "factory": ["info", "list", "recipe"],
    # /application group
    "application": ["list"],
}

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"

_PASS = f"  {GREEN}OK{RESET}  "
_FAIL = f"  {RED}FAIL{RESET} "
_WARN = f"  {YELLOW}WARN{RESET} "


def _load_env() -> None:
    load_env_file()


def _subcommands_of(cmd: dict) -> set[str]:
    """Extract subcommand names from a guild command payload.

    Discord returns groups as a top-level command with ``options`` of
    ``type == 1`` (Subcommand) or ``type == 2`` (SubcommandGroup). We only go
    one level deep — Lambat's groups are flat (no nested subcommand groups).
    """
    subs: set[str] = set()
    for opt in cmd.get("options", []) or []:
        # type 1 = Subcommand, type 2 = SubcommandGroup
        if opt.get("type") in (1, 2):
            subs.add(opt["name"])
    return subs


async def _audit(token: str, guild_id: int) -> dict[str, object]:
    """Fetch guild commands + compare against EXPECTED_COMMANDS."""
    result: dict[str, object] = {
        "guild_id": guild_id,
        "expected": EXPECTED_COMMANDS,
        "actual": {},
        "missing": [],
        "missing_subcommands": [],
        "unexpected": [],
        "failures": [],
    }
    print(f"\n{BOLD}Command sync audit{RESET} -> guild {guild_id}")

    # intents=0: we only need the REST client, no privileged intents.
    client = discord.Client(intents=discord.Intents.none())
    ready = asyncio.Event()

    @client.event
    async def on_ready():
        ready.set()

    async def _runner() -> None:
        with contextlib.suppress(discord.LoginFailure, discord.HTTPException):
            await client.start(token)

    runner = asyncio.create_task(_runner())

    try:
        await asyncio.wait_for(ready.wait(), timeout=15)
    except TimeoutError:
        print(f"{_FAIL}Timed out connecting to Discord (15s). Check network/token.")
        result["failures"].append("discord connect timeout")
        with contextlib.suppress(Exception):
            await client.close()
        runner.cancel()
        return result

    # Resolve client.user.id with a retry (discord.py 2.7.x race where
    # on_ready fires before client.user is populated).
    bot_user_id = None
    for _ in range(20):
        u = client.user
        if u is not None and getattr(u, "id", None) is not None:
            bot_user_id = u.id
            break
        await asyncio.sleep(0.1)

    if bot_user_id is None:
        print(f"{_FAIL}client.user never resolved (discord.py race). Re-run.")
        result["failures"].append("client.user never resolved")
        await client.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner, timeout=5)
        return result

    print(f"{_PASS}Connected as bot user {bot_user_id}.\n")

    try:
        existing = await client.http.get_guild_commands(bot_user_id, guild_id)
    except discord.NotFound:
        print(f"{_FAIL}Guild {guild_id} not found (wrong ID or bot not invited).")
        result["failures"].append(f"guild {guild_id} not found")
        await client.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner, timeout=5)
        return result
    except discord.HTTPException as e:
        print(f"{_FAIL}Could not fetch guild commands: {e}")
        result["failures"].append(f"guild commands fetch failed: {e}")
        await client.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner, timeout=5)
        return result

    actual: dict[str, set[str]] = {}
    for cmd in existing:
        name = cmd["name"]
        actual[name] = _subcommands_of(cmd)
    result["actual"] = {k: sorted(v) for k, v in actual.items()}  # type: ignore[index]

    # --- Compare --------------------------------------------------------
    print(f"{BOLD}Expected vs synced{RESET}")
    for name, expected_subs in EXPECTED_COMMANDS.items():
        if name not in actual:
            print(f"{_FAIL}{name} — MISSING entirely")
            result["missing"].append(name)
            result["failures"].append(f"command missing: {name}")
            continue
        if expected_subs:
            actual_subs = actual[name]
            missing_subs = set(expected_subs) - actual_subs
            if missing_subs:
                print(f"{_FAIL}{name} subcommands missing: {sorted(missing_subs)}")
                result["missing_subcommands"].append(
                    {"command": name, "missing": sorted(missing_subs)}
                )
                result["failures"].append(f"subcommands missing for {name}: {sorted(missing_subs)}")
            else:
                extra = actual_subs - set(expected_subs)
                tag = f" (+{len(extra)} extra)" if extra else ""
                print(f"{_PASS}/{name}  ({len(expected_subs)} subcommands){tag}")
        else:
            print(f"{_PASS}/{name}  (standalone)")

    # Unexpected commands (informational — not a failure, but worth flagging).
    unexpected = sorted(set(actual) - set(EXPECTED_COMMANDS))
    if unexpected:
        print(f"\n{_WARN}Unexpected commands (not in inventory, not a failure):")
        for u in unexpected:
            print(f"        /{u}  (subs: {sorted(actual[u]) or 'none'})")
        result["unexpected"] = unexpected

    # Full listing.
    print(f"\n{BOLD}Full synced command list ({len(existing)} commands){RESET}")
    for cmd in sorted(existing, key=lambda c: c["name"]):
        subs = _subcommands_of(cmd)
        if subs:
            print(f"  /{cmd['name']}  ->  {', '.join(sorted(subs))}")
        else:
            opts = cmd.get("options", []) or []
            opt_names = [o["name"] for o in opts if o.get("type") not in (1, 2)]
            tag = f"  (opts: {', '.join(opt_names)})" if opt_names else ""
            print(f"  /{cmd['name']}{tag}")

    await client.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(runner, timeout=5)
    return result


def _print_summary(failures: list[str]) -> None:
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    if failures:
        print(f"{RED}{BOLD}FAIL: {len(failures)} issue(s):{RESET}")
        for f in failures:
            print(f"   - {f}")
        print()
        print("If commands are missing, start the bot once (python main.py) to")
        print("trigger the guild sync, then re-run this audit.")
    else:
        print(f"{GREEN}{BOLD}OK: all expected commands are synced.{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout.")
    args = parser.parse_args()
    _load_env()

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    guild_id = int(os.environ.get("GUILD_ID", "0") or "0")

    if not token:
        print("ERROR: DISCORD_TOKEN is not set.", file=sys.stderr)
        sys.exit(2)
    if guild_id == 0:
        print("ERROR: GUILD_ID is not set (needed for guild command audit).", file=sys.stderr)
        print("       Global command audit is not supported (slow + 1h cache).", file=sys.stderr)
        sys.exit(2)

    try:
        result = asyncio.run(_audit(token, guild_id))
    except Exception as e:  # noqa: BLE001
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"{RED}ERROR: {e}{RESET}", file=sys.stderr)
        sys.exit(2)

    failures = result["failures"]  # type: ignore[assignment]
    if args.json:
        result["ok"] = not failures  # type: ignore[index]
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_summary(failures)  # type: ignore[arg-type]
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
