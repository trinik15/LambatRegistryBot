#!/usr/bin/env python3
"""Idempotent test-data seeder for local E2E testing.

Inserts a handful of settlements + citizens directly into the registry DB so
the read-only slash commands (/citizen list, /report census, /settlement info,
…) have something to show the moment the bot is running. Skips rows that
already exist, so re-running is safe.

Does NOT touch Discord (no roles assigned, no audit emit, no governance post).
For a full E2E test you should ALSO run /citizen add in Discord for at least
one citizen to exercise the role-assignment path. This script is for
populating read-test data quickly.

Usage:
    # Uses DATABASE_URL from .env (loaded automatically)
    python scripts/seed.py

    # Or override the DB URL directly
    DATABASE_URL=postgresql://lambat:lambat_dev_password@localhost:5432/lambat \\
        python scripts/seed.py

    # Wipe the seeded data first (so you get a clean re-seed)
    python scripts/seed.py --reset
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Make the repo root importable so `from core.config import Config` works
# when running this script directly (i.e. NOT installed as a package).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import asyncpg  # noqa: E402

from scripts._env_loader import load_env_file  # noqa: E402

# --- Seed data ------------------------------------------------------------
# Five settlements spanning four duchies + the capital. Matches the canonical
# Lambat geography from core/constants.py so /settlement list groups them
# correctly under their duchy emojis.
SETTLEMENTS: list[tuple[str, str]] = [
    # (name, duchy)
    ("Lambat City", "Capital"),
    ("New September", "September"),
    ("Timberbourg", "Timberbourg"),
    ("Orqueda", "Orqueda"),
    ("Plaza", "Capital"),
]

# Six test citizens spread across the settlements. Discord IDs are obviously
# fake (16-digit numbers in the test range) — they will NOT resolve to real
# Discord users, so role assignment will fail silently for them. That's fine:
# the point is to populate the registry so read commands work. For role-
# assignment testing, add yourself via /citizen add in Discord.
TODAY = date.today()
CITIZENS: list[dict[str, object]] = [
    {
        "ign": "TestKingAlice",
        "discord_id": "100000000000000001",
        "settlement": "Lambat City",
        "recruiter_ids": "",
        "join_date": TODAY - timedelta(days=120),
    },
    {
        "ign": "TestQueenBob",
        "discord_id": "100000000000000002",
        "settlement": "New September",
        "recruiter_ids": "100000000000000001",
        "join_date": TODAY - timedelta(days=90),
    },
    {
        "ign": "TestKnightCara",
        "discord_id": "100000000000000003",
        "settlement": "Timberbourg",
        "recruiter_ids": "100000000000000001",
        "join_date": TODAY - timedelta(days=60),
    },
    {
        "ign": "TestSquireDave",
        "discord_id": "100000000000000004",
        "settlement": "Orqueda",
        "recruiter_ids": "100000000000000002",
        "join_date": TODAY - timedelta(days=30),
    },
    {
        "ign": "TestCitizenEve",
        "discord_id": "100000000000000005",
        "settlement": "Plaza",
        "recruiter_ids": "100000000000000001,100000000000000002",
        "join_date": TODAY - timedelta(days=14),
    },
    {
        "ign": "TestNewcomerFrank",
        "discord_id": "100000000000000006",
        "settlement": "Lambat City",
        "recruiter_ids": "100000000000000003",
        "join_date": TODAY - timedelta(days=2),
    },
]


def _load_env_file() -> None:
    """Load .env from the repo root into os.environ (without python-dotenv).

    Delegates to scripts._env_loader.load_env_file which correctly handles
    inline comments (``KEY=value  # comment``) and quoted values.
    """
    load_env_file()


async def _seed(reset: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print(
            "ERROR: DATABASE_URL is not set. Either export it or create a .env file.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"Connecting to: {_mask_url(db_url)}")
    conn = await asyncpg.connect(db_url)
    try:
        if reset:
            print("⚠️  --reset: deleting existing citizens + settlements...")
            # Order matters: citizens first (FK), then settlements.
            await conn.execute("DELETE FROM citizens")
            await conn.execute("DELETE FROM settlements")
            print("   Done. Tables are now empty.")

        # Settlements: idempotent upsert.
        inserted_settlements = 0
        for name, duchy in SETTLEMENTS:
            result = await conn.execute(
                "INSERT INTO settlements (name, duchy) VALUES ($1, $2) "
                "ON CONFLICT (name) DO NOTHING",
                name,
                duchy,
            )
            if result.endswith("1"):
                inserted_settlements += 1
                print(f"   + settlement: {name} (duchy={duchy})")
            else:
                print(f"   = settlement: {name} (already exists)")

        # Citizens: idempotent upsert by IGN (CITEXT PK).
        inserted_citizens = 0
        for c in CITIZENS:
            result = await conn.execute(
                """
                INSERT INTO citizens (ign, discord_id, settlement, recruiter_ids, join_date)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (ign) DO NOTHING
                """,
                c["ign"],
                c["discord_id"],
                c["settlement"],
                c["recruiter_ids"],
                c["join_date"],
            )
            if result.endswith("1"):
                inserted_citizens += 1
                print(f"   + citizen: {c['ign']} → {c['settlement']}")
            else:
                print(f"   = citizen: {c['ign']} (already exists)")

        # Back-fill recruiters junction for the newly-inserted citizens
        # (mirrors the logic in core/database.py init_db()).
        backfilled = 0
        for c in CITIZENS:
            raw = c["recruiter_ids"]
            if not raw:
                continue
            for rid in (s.strip() for s in str(raw).split(",")):
                if not rid or not rid.isdigit():
                    continue
                result = await conn.execute(
                    "INSERT INTO recruiters (ign, recruiter_discord_id, recruited_at) "
                    "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    c["ign"],
                    rid,
                    c["join_date"],
                )
                if result.endswith("1"):
                    backfilled += 1
        if backfilled:
            print(f"   Back-filled {backfilled} recruiter junction rows.")

        # Summary count.
        s_count = await conn.fetchval("SELECT COUNT(*) FROM settlements")
        c_count = await conn.fetchval("SELECT COUNT(*) FROM citizens")
        print()
        print(f"✅ Done. Registry now has {s_count} settlements, {c_count} citizens.")
        print()
        print("Next steps:")
        print("  1. Start the bot:  python main.py   (or: docker compose up)")
        print("  2. In Discord, run:  /citizen list  →  should show 6 citizens")
        print("  3. In Discord, run:  /report census  →  should show per-settlement breakdown")
        print("  4. In Discord, run:  /settlement info name:Lambat City  →  dashboard")
    finally:
        await conn.close()


def _mask_url(url: str) -> str:
    """Hide the password in a postgres URL for safe logging."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if ":" not in rest.split("@", 1)[0]:
        return url
    creds, hostpart = rest.split("@", 1)
    user, _, _ = creds.partition(":")
    return f"{scheme}://{user}:****@{hostpart}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all existing citizens + settlements before seeding.",
    )
    args = parser.parse_args()
    _load_env_file()
    asyncio.run(_seed(reset=args.reset))


if __name__ == "__main__":
    main()
