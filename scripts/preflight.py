#!/usr/bin/env python3
"""Pre-flight checks: verify your .env + Discord setup BEFORE starting the bot.

Catches the 5 most common "why isn't my bot working" mistakes in <5 seconds,
without launching the full gateway:

  1. DISCORD_TOKEN is invalid or for the wrong bot.
  2. GUILD_ID points at a server the bot hasn't been invited to.
  3. The bot's top role is BELOW a role it needs to assign (silent failure).
  4. AUDIT_CHANNEL_ID / ALERT_CHANNEL_ID / etc. point at channels the bot
     can't see (so alerts silently never post).
  5. Slash commands aren't synced to the guild yet (so /help is invisible
     for up to an hour after the first invite).

Exits 0 if all checks pass, 1 if any fail. Run this BEFORE `python main.py`
every time you change .env or re-invite the bot.

Usage:
    python scripts/preflight.py

    # Only run a subset of checks (e.g. skip the channel-permission sweep)
    python scripts/preflight.py --skip channels
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path

import discord

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts._env_loader import load_env_file  # noqa: E402


def _load_env_file() -> None:
    """Load .env, correctly handling inline comments + quoted values."""
    load_env_file()


# ANSI colours — kept simple so the output reads cleanly on any terminal.
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"

_PASS = f"  {GREEN}✓{RESET} "
_FAIL = f"  {RED}✗{RESET} "
_WARN = f"  {YELLOW}⚠{RESET} "


def _env_int(name: str) -> int:
    raw = os.environ.get(name, "0").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def _safe_user_id(client: discord.Client) -> int | None:
    """Safely extract ``client.user.id``, handling discord.py 2.7.1's race.

    In discord.py 2.7.1, ``client.user`` returns ``_MissingSentinel`` (a truthy
    singleton with no ``.id`` attribute) for a brief window after ``on_ready``
    fires, before the IDENTIFY session fully populates the user object. A naive
    ``if client.user:`` check passes (sentinel is truthy), then ``.id`` raises
    ``AttributeError: '_MissingSentinel'``. This helper returns ``None`` instead
    of crashing, so callers can retry.
    """
    user = client.user
    if user is None:
        return None
    # _MissingSentinel has no .id attribute; a real ClientUser does.
    return getattr(user, "id", None)


async def _resolve_user_id(client: discord.Client, timeout_s: float = 2.0) -> int | None:
    """Poll ``client.user`` until it's a real user (or timeout).

    Works around the discord.py 2.7.1 race where ``on_ready`` fires before
    ``client.user`` is populated. Polls every 100ms for up to ``timeout_s``.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        uid = _safe_user_id(client)
        if uid is not None:
            return uid
        await asyncio.sleep(0.1)
    return None


async def run(skips: set[str]) -> int:
    from core.config import Config  # noqa: F401 — validates env on import

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    guild_id = _env_int("GUILD_ID")

    failures: list[str] = []
    warnings: list[str] = []

    # --- Check 1: token is valid -------------------------------------------------
    print(f"\n{BOLD}1. Discord token{RESET}")
    if not token:
        print(f"{_FAIL}DISCORD_TOKEN is not set.")
        failures.append("DISCORD_TOKEN missing")
    else:
        # A bare HTTP client (no gateway) is enough for all the checks below.
        # intents=0 means we don't request any privileged intents — we only
        # use the REST API.
        client = discord.Client(intents=discord.Intents.none())

        @client.event
        async def on_ready():
            # discord.py 2.7.1: client.user may be _MissingSentinel briefly
            # after on_ready. Resolve it with a retry before printing.
            uid = await _resolve_user_id(client, timeout_s=2.0)
            if uid is None:
                print(f"{_FAIL}Token accepted but client.user never resolved (discord.py race).")
                print("       Try re-running; this is transient.")
            else:
                print(f"{_PASS}Token valid. Bot user ID: {uid}.")
            await client.close()

        token_ok = True

        async def _token_check() -> None:
            nonlocal token_ok
            try:
                await client.start(token)
            except discord.LoginFailure:
                print(f"{_FAIL}Token rejected by Discord (LoginFailure).")
                token_ok = False
            except discord.PrivilegedIntentsRequired:
                # Won't happen — we requested no intents.
                print(f"{_FAIL}Privileged intents required (unexpected).")
                token_ok = False

        try:
            await asyncio.wait_for(_token_check(), timeout=15)
        except TimeoutError:
            print(f"{_FAIL}Timed out connecting to Discord (15s). Check your network.")
            token_ok = False

        if not token_ok:
            failures.append("Discord token invalid")
            # No point continuing — every other check needs the token.
            _print_summary(failures, warnings)
            return 1

    # --- Check 2: guild is reachable + bot has the right intents ----------------
    print(f"\n{BOLD}2. Guild + members intent{RESET}")
    if guild_id == 0:
        print(f"{_WARN}GUILD_ID is not set. Commands will sync globally (slow, up to 1h).")
        warnings.append("GUILD_ID not set — global sync")
    else:
        # Use a fresh client with the members intent so we can actually fetch
        # the guild + role list. This also confirms the SERVER MEMBERS INTENT
        # is enabled on the bot application (a very common gotcha).
        intents = discord.Intents.default()
        intents.members = True
        client2 = discord.Client(intents=intents)
        ready = asyncio.Event()

        @client2.event
        async def on_ready():
            # Don't resolve user here — _run_guild_checks will do it with its
            # own retry loop. Just signal that the gateway is up.
            ready.set()

        # We'll run all the remaining checks inside this client's context.
        check_task = asyncio.create_task(
            _run_guild_checks(client2, guild_id, failures, warnings, skips)
        )

        async def _runner() -> None:
            with contextlib.suppress(discord.LoginFailure):
                # Already validated in check 1 — shouldn't happen.
                await client2.start(token)

        runner = asyncio.create_task(_runner())
        try:
            await asyncio.wait_for(ready.wait(), timeout=15)
            await asyncio.wait_for(check_task, timeout=30)
        except TimeoutError:
            print(f"{_FAIL}Timed out fetching guild info (30s).")
            failures.append("guild fetch timeout")
        finally:
            await client2.close()
            runner.cancel()

    _print_summary(failures, warnings)
    return 1 if failures else 0


async def _run_guild_checks(
    client: discord.Client,
    guild_id: int,
    failures: list[str],
    warnings: list[str],
    skips: set[str],
) -> None:
    """Runs checks 2b-5 inside a connected discord.Client."""
    # discord.py 2.7.1: resolve client.user.id with a retry, because
    # on_ready fires before the user object is fully populated.
    bot_user_id = await _resolve_user_id(client, timeout_s=2.0)
    if bot_user_id is None:
        print(f"{_FAIL}Could not resolve bot user ID (discord.py 2.7.1 race condition).")
        print("       This is transient — re-run preflight.py.")
        failures.append("client.user never resolved")
        return

    guild = client.get_guild(guild_id)
    if guild is None:
        # Fall back to an HTTP fetch — sometimes the cache isn't populated yet
        # for a freshly-connected client.
        try:
            guild = await client.fetch_guild(guild_id)
        except discord.NotFound:
            print(f"{_FAIL}Guild {guild_id} not found. Has the bot been invited?")
            failures.append("guild not found")
            return
        except discord.Forbidden:
            print(f"{_FAIL}Bot cannot see guild {guild_id} (Forbidden).")
            failures.append("guild forbidden")
            return

    print(f"{_PASS}Guild reachable: {guild.name} (id={guild.id}, members={guild.member_count}).")

    # Members intent verification: if on_ready saw the guild but member_count
    # is None, the members intent is NOT enabled on the bot application.
    if guild.member_count is None:
        print(f"{_FAIL}member_count is None — SERVER MEMBERS INTENT is NOT enabled.")
        print("       Discord Developer Portal → your app → Bot → Privileged Gateway Intents.")
        failures.append("members intent disabled")
    else:
        print(f"{_PASS}Members intent working (member_count={guild.member_count}).")

    if "roles" in skips:
        print(f"{_WARN}Skipping role-hierarchy check (--skip roles).")
    else:
        # --- Check 3: role hierarchy ---
        print(f"\n{BOLD}3. Role hierarchy{RESET}")
        me = guild.get_member(bot_user_id)
        if me is None:
            try:
                me = await guild.fetch_member(bot_user_id)
            except discord.HTTPException:
                me = None
        if me is None:
            print(f"{_FAIL}Bot is not a member of the guild (was it kicked?).")
            failures.append("bot not in guild")
            return

        bot_top = me.top_role
        print(f"{_PASS}Bot's top role: {bot_top.name} (position={bot_top.position}).")

        # The role IDs the bot needs to assign must be BELOW the bot's top role.
        citizen_ids = [
            int(x) for x in os.environ.get("CITIZEN_ROLE_IDS", "").split(",") if x.strip()
        ]
        settler_id = _env_int("SETTLER_ROLE_ID")
        all_managed = [("Citizen", rid) for rid in citizen_ids]
        all_managed.append(("Settler", settler_id))
        for label, rid in all_managed:
            if rid == 0:
                continue
            role = guild.get_role(rid)
            if role is None:
                print(f"{_FAIL}{label} role id {rid} not found in guild.")
                failures.append(f"{label} role id {rid} not found")
                continue
            if role.position >= bot_top.position:
                print(
                    f"{_FAIL}{label} role '{role.name}' (pos={role.position}) is AT OR ABOVE "
                    f"bot's top role (pos={bot_top.position}). Role assignment will silently fail."
                )
                failures.append(f"{label} role above bot top role")
            else:
                print(
                    f"{_PASS}{label} role '{role.name}' (pos={role.position}) < bot top (pos={bot_top.position})."
                )

    # --- Check 4: configured channels are visible to the bot ---
    if "channels" in skips:
        print(f"\n{BOLD}4. Channels{RESET}  (skipped via --skip channels)")
    else:
        print(f"\n{BOLD}4. Channels{RESET}")
        channel_vars = [
            ("ALERT_CHANNEL_ID", "outage alerts"),
            ("AUDIT_CHANNEL_ID", "audit log mirror"),
            ("APPLICATIONS_CHANNEL_ID", "citizen applications"),
            ("GOVERNANCE_CHANNEL_ID", "governance notifications"),
            ("MONTHLY_REPORT_CHANNEL_ID", "monthly census report"),
        ]
        for var_name, purpose in channel_vars:
            cid = _env_int(var_name)
            if cid == 0:
                print(f"{_WARN}{var_name} not set (0). {purpose.capitalize()} will be disabled.")
                continue
            try:
                ch = await client.fetch_channel(cid)
                # fetch_channel returns a channel object; check it's a text-ish channel.
                from discord import TextChannel, Thread, VoiceChannel

                if isinstance(ch, (TextChannel, Thread, VoiceChannel)):
                    # Check the bot has Send Messages permission.
                    me = ch.guild.get_member(bot_user_id)
                    if me is None:
                        try:
                            me = await ch.guild.fetch_member(bot_user_id)
                        except discord.HTTPException:
                            me = None
                    if me is None:
                        print(
                            f"{_FAIL}{var_name}={cid} → #{ch.name}. Bot member not found in guild."
                        )
                        failures.append(f"{var_name} bot member not found")
                        continue
                    perms = ch.permissions_for(me)
                    if perms.send_messages:
                        print(
                            f"{_PASS}{var_name}={cid} → #{ch.name} ({purpose}). Can send messages."
                        )
                    else:
                        print(f"{_FAIL}{var_name}={cid} → #{ch.name}. Bot lacks Send Messages.")
                        failures.append(f"{var_name} no send permission")
                else:
                    print(f"{_WARN}{var_name}={cid} is a {type(ch).__name__}, not a text channel.")
            except discord.NotFound:
                print(f"{_FAIL}{var_name}={cid} not found.")
                failures.append(f"{var_name} channel not found")
            except discord.Forbidden:
                print(f"{_FAIL}{var_name}={cid}: bot can't see this channel (Forbidden).")
                failures.append(f"{var_name} channel forbidden")

    # --- Check 5: command tree sync status ---
    if "sync" in skips:
        print(f"\n{BOLD}5. Command sync{RESET}  (skipped via --skip sync)")
    else:
        print(f"\n{BOLD}5. Command sync{RESET}")
        # Fetch the guild's registered commands via the HTTP API.
        # Use bot_user_id (already resolved) instead of client.user.id.
        try:
            existing = await client.http.get_guild_commands(bot_user_id, guild_id)
            if not existing:
                print(f"{_WARN}No slash commands synced to this guild yet.")
                print("       Start the bot once (python main.py) to trigger the initial sync,")
                print("       then re-run this script to confirm. Or run /sync as the owner.")
                warnings.append("no commands synced to guild")
            else:
                names = sorted(c["name"] for c in existing)
                print(f"{_PASS}{len(names)} commands synced: {', '.join(names)}")
        except discord.HTTPException as e:
            print(f"{_WARN}Could not fetch guild commands: {e}")
            warnings.append(f"guild commands fetch failed: {e}")


def _print_summary(failures: list[str], warnings: list[str]) -> None:
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    if failures:
        print(f"{RED}{BOLD}✗ {len(failures)} check(s) FAILED:{RESET}")
        for f in failures:
            print(f"   • {f}")
        print()
        print(f"{BOLD}Fix the issues above, then re-run:{RESET}  python scripts/preflight.py")
    else:
        print(f"{GREEN}{BOLD}✓ All critical checks passed.{RESET}")
        if warnings:
            print(f"{YELLOW}  ({len(warnings)} non-blocking warning(s) — review above.){RESET}")
        print()
        print(f"{BOLD}You're clear to start the bot:{RESET}  python main.py")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=["roles", "channels", "sync"],
        help="Skip a category of checks (can be repeated).",
    )
    args = parser.parse_args()
    _load_env_file()
    try:
        rc = asyncio.run(run(set(args.skip)))
    except KeyboardInterrupt:
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
