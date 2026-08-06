#!/usr/bin/env python3
"""Database state verification for the E2E test harness.

Connects to the registry database (DATABASE_URL) and verifies:

  1. Every expected table exists (Phase 0 -> Phase 4 schema).
  2. The pg_trgm extension is installed (Phase 3.2 trigram search index).
  3. The Phase 4.6 ``monthly_snapshots.notes`` column exists.
  4. If ``scripts/seed.py`` has been run, the 5 seed settlements + 6 seed
     citizens are present (and the recruiters junction was back-filled).

This is a READ-ONLY check: it never mutates the database. Run it AFTER
``scripts/seed.py`` to confirm the seed landed, or on a live DB to confirm
no migration has been lost.

Exits 0 if every check passes, 1 if any fail. Designed to be called by
``scripts/run_e2e.ps1`` (stage S11) but also works standalone:

    python scripts/db_verify.py
    DATABASE_URL=postgresql://lambat:lambat_dev_password@localhost:5432/lambat \\
        python scripts/db_verify.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import asyncpg  # noqa: E402

from scripts._env_loader import load_env_file  # noqa: E402

# --- Expected schema -------------------------------------------------------
# Every table the bot's init_db() creates, by phase. Kept in sync with
# core/database.py. If a migration is added, append it here AND in db_verify's
# checks so the E2E harness catches a missed migration immediately.
EXPECTED_TABLES: dict[str, str] = {
    "settlements": "Phase 0 core table",
    "citizens": "Phase 0 core table",
    "activity_cache": "Phase 0 CivInfo cache",
    "monthly_snapshots": "Phase 0 census snapshots",
    "audit_log": "Phase 2.1 auditability",
    "recruiters": "Phase 2.2 normalised junction",
    "guild_emojis": "Phase 2.4 DB-backed emoji map",
    "citizen_applications": "Phase 3.4 self-service /apply",
}

# Seed data that scripts/seed.py inserts. Verified ONLY when --check-seed is on
# (the harness enables it; a fresh DB before seeding legitimately has none).
SEED_SETTLEMENTS = {
    "Lambat City",
    "New September",
    "Timberbourg",
    "Orqueda",
    "Plaza",
}
SEED_CITIZENS = {
    "TestKingAlice",
    "TestQueenBob",
    "TestKnightCara",
    "TestSquireDave",
    "TestCitizenEve",
    "TestNewcomerFrank",
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


async def _verify(db_url: str, check_seed: bool) -> dict[str, object]:
    """Run all checks. Returns a result dict for --json mode."""
    result: dict[str, object] = {
        "db_url": _mask_url(db_url),
        "tables": {},
        "extensions": {},
        "columns": {},
        "seed": {},
        "failures": [],
    }
    print(f"\n{BOLD}DB verify{RESET} -> {_mask_url(db_url)}")

    conn = await asyncpg.connect(db_url)
    try:
        # --- 1. Tables -----------------------------------------------------
        print(f"\n{BOLD}1. Expected tables{RESET}")
        existing = {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        missing_tables: list[str] = []
        for tname, why in EXPECTED_TABLES.items():
            if tname in existing:
                print(f"{_PASS}{tname}  ({why})")
                result["tables"][tname] = True  # type: ignore[index]
            else:
                print(f"{_FAIL}{tname} MISSING  ({why})")
                result["tables"][tname] = False  # type: ignore[index]
                missing_tables.append(tname)
                result["failures"].append(f"table missing: {tname}")

        if not missing_tables:
            extra = sorted(existing - set(EXPECTED_TABLES))
            if extra:
                print(f"{_WARN}Unexpected extra tables: {', '.join(extra)}")
                result["failures"].append(f"unexpected tables: {extra}")

        # --- 2. Extensions -------------------------------------------------
        print(f"\n{BOLD}2. Extensions{RESET}")
        ext_row = await conn.fetchval("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
        if ext_row == "pg_trgm":
            print(f"{_PASS}pg_trgm installed (Phase 3.2 trigram search)")
            result["extensions"]["pg_trgm"] = True  # type: ignore[index]
        else:
            print(f"{_FAIL}pg_trgm NOT installed (citizen search will be slow/wrong)")
            result["extensions"]["pg_trgm"] = False  # type: ignore[index]
            result["failures"].append("extension missing: pg_trgm")

        # --- 3. Phase 4.6 column ------------------------------------------
        print(f"\n{BOLD}3. Phase 4.6 column: monthly_snapshots.notes{RESET}")
        notes_col = await conn.fetchval(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'monthly_snapshots' AND column_name = 'notes'"
        )
        if notes_col == "notes":
            print(f"{_PASS}monthly_snapshots.notes exists")
            result["columns"]["monthly_snapshots.notes"] = True  # type: ignore[index]
        else:
            print(f"{_FAIL}monthly_snapshots.notes MISSING (Phase 4.6 not migrated)")
            result["columns"]["monthly_snapshots.notes"] = False  # type: ignore[index]
            result["failures"].append("column missing: monthly_snapshots.notes")

        # --- 4. Seed data (only if requested) -----------------------------
        if check_seed:
            print(f"\n{BOLD}4. Seed data (scripts/seed.py){RESET}")
            db_settlements = {r["name"] for r in await conn.fetch("SELECT name FROM settlements")}
            db_citizens = {r["ign"] for r in await conn.fetch("SELECT ign FROM citizens")}
            recruiter_count = await conn.fetchval("SELECT COUNT(*) FROM recruiters")

            missing_s = SEED_SETTLEMENTS - db_settlements
            missing_c = SEED_CITIZENS - db_citizens
            if not missing_s:
                print(f"{_PASS}{len(SEED_SETTLEMENTS)} seed settlements present")
            else:
                print(f"{_FAIL}Missing settlements: {sorted(missing_s)}")
                result["failures"].append(f"missing seed settlements: {sorted(missing_s)}")
            if not missing_c:
                print(f"{_PASS}{len(SEED_CITIZENS)} seed citizens present")
            else:
                print(f"{_FAIL}Missing citizens: {sorted(missing_c)}")
                result["failures"].append(f"missing seed citizens: {sorted(missing_c)}")
            if recruiter_count and recruiter_count > 0:
                print(f"{_PASS}{recruiter_count} recruiter junction rows (back-filled)")
            else:
                print(f"{_WARN}No recruiter junction rows (back-fill may not have run)")
            result["seed"] = {  # type: ignore[index]
                "settlements_present": sorted(SEED_SETTLEMENTS & db_settlements),
                "settlements_missing": sorted(missing_s),
                "citizens_present": sorted(SEED_CITIZENS & db_citizens),
                "citizens_missing": sorted(missing_c),
                "recruiter_rows": recruiter_count,
            }

        # --- 5. Row counts (informational) --------------------------------
        print(f"\n{BOLD}5. Row counts (informational){RESET}")
        for tname in EXPECTED_TABLES:
            if tname in existing:
                n = await conn.fetchval(f"SELECT COUNT(*) FROM {tname}")  # noqa: S608
                print(f"  {tname:<22} {n:>6} rows")
    finally:
        await conn.close()

    return result


def _mask_url(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if ":" not in rest.split("@", 1)[0]:
        return url
    creds, hostpart = rest.split("@", 1)
    user, _, _ = creds.partition(":")
    return f"{scheme}://{user}:****@{hostpart}"


def _print_summary(failures: list[str]) -> None:
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    if failures:
        print(f"{RED}{BOLD}FAIL: {len(failures)} check(s) failed:{RESET}")
        for f in failures:
            print(f"   - {f}")
    else:
        print(f"{GREEN}{BOLD}OK: all DB checks passed.{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-seed",
        action="store_true",
        help="Also verify the 5 settlements + 6 citizens from scripts/seed.py.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report on stdout (instead of human text).",
    )
    args = parser.parse_args()
    _load_env()

    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(2)

    try:
        result = asyncio.run(_verify(db_url, check_seed=args.check_seed))
    except Exception as e:  # noqa: BLE001
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"{RED}ERROR connecting to DB: {e}{RESET}", file=sys.stderr)
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
