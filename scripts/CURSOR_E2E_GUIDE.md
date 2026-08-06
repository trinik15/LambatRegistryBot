# Cursor AI — E2E Run Guide for LambatRegistryBot

Copy everything inside the ` ```text ... ``` ` block below (or the whole
"PROMPT" section) and paste it into a fresh Cursor AI chat on your Windows
machine. Cursor will set up, run the full automated E2E harness, and paste back
a structured report.

> The harness lives in `scripts/run_e2e.ps1`. It runs 15 stages (lint, types,
> unit tests, docker build, docker up, healthz wait, Discord preflight, DB
> seed, HTTP smoke, DB verify, slash-command sync audit, log capture, teardown)
> and writes `e2e_report.md` + `e2e_report.txt` + `e2e_docker_logs.txt`.

---

## PROMPT — copy from here

```text
You are an autonomous QA agent running on a Windows machine. Your job is to
run the full end-to-end (E2E) test harness for the LambatRegistryBot Discord
bot project and report back a detailed result. Work step by step. Do NOT skip
steps. Do NOT rewrite application source code unless a stage failure is clearly
caused by a trivial env/config typo you can fix in one line.

=== CONTEXT ===
- Project: LambatRegistryBot — a Python 3.11+ Discord bot (discord.py 2.6 +
  asyncpg + PostgreSQL) for the Kingdom of Lambat nation on CivMC.
- Repo:  https://github.com/trinik15/LambatRegistryBot
- The repo already has a Windows E2E harness at scripts/run_e2e.ps1. USE IT.
  Do not reinvent it.
- Phases 0-4 are all implemented and shipped; this run verifies them E2E.

=== STEP 0 — Locate or clone the repo ===
1. Check if the repo already exists on this machine. Try these in order:
   - The current working directory (look for main.py + scripts/run_e2e.ps1).
   - C:\dev\LambatRegistryBot , C:\Users\<you>\LambatRegistryBot , D:\LambatRegistryBot
2. If NOT found, clone it:
       git clone https://github.com/trinik15/LambatRegistryBot.git
3. cd into the repo root (the folder containing main.py and scripts\).
4. Run:  git log --oneline -3   (confirm HEAD is at least "Phase 4" commit).
5. Report the absolute repo path back to me.

=== STEP 1 — Verify prerequisites ===
Run each and report the version. If any is missing, STOP and tell me which one.
- python --version        (needs 3.11+; if `python` not found, try `py -3`)
- docker --version        (needs Docker Desktop running)
- docker compose version  (v2 plugin; if missing, try `docker-compose --version`)
- git --version

If Docker Desktop is installed but not running, START IT (start the Docker
Desktop app) and wait until `docker info` succeeds before continuing.

=== STEP 2 — Set up the Python environment ===
1. Create a venv (recommended so deps don't pollute system Python):
       py -3 -m venv .venv
       .\.venv\Scripts\Activate.ps1
   (If `py -3` is missing, use `python -m venv .venv`.)
2. Upgrade pip:
       python -m pip install --upgrade pip
3. Install the project + dev tooling (ruff, mypy, pytest):
       pip install -e ".[dev]"
4. Confirm the tools are importable:
       python -m ruff --version
       python -m mypy --version
   If any fails, STOP and paste the error.

=== STEP 3 — Configure .env ===
1. If .env does not exist:  copy .env.example .env
2. Open .env. Fill in the REQUIRED values if you have them:
   - DISCORD_TOKEN   (from https://discord.com/developers/applications)
   - GUILD_ID        (right-click your test server > Copy ID; enable Dev Mode)
   - OWNER_ID        (your Discord user ID)
   - DATABASE_URL    (for docker-compose runs, LEAVE the default in .env.example
                       OR set it to: postgresql://lambat:lambat_dev_password@localhost:5432/lambat
                       — the harness overrides it for the compose `db` service anyway)
   - CITIZEN_ROLE_IDS, SETTLER_ROLE_ID, FULL_ACCESS_ROLE_IDS  (real role IDs)
   - AUDIT_CHANNEL_ID, APPLICATIONS_CHANNEL_ID, GOVERNANCE_CHANNEL_ID,
     ALERT_CHANNEL_ID, MONTHLY_REPORT_CHANNEL_ID  (real channel IDs; 0 disables)
3. If you do NOT have a DISCORD_TOKEN (e.g. testing on a machine without the
   bot's credentials), that's fine — the harness will auto-skip the two
   Discord-specific stages (S8 preflight, S12 command-sync audit). Note this
   in your final report.
4. NEVER print the full DISCORD_TOKEN back to me. If you must reference it,
   show only the last 4 characters.

=== STEP 4 — Run the automated E2E harness ===
From the repo root, run:

    .\scripts\run_e2e.ps1 -KeepRunning

Why -KeepRunning: it leaves the bot container up after the checks so the
manual Discord slash-command checklist (scripts/E2E_CHECKLIST.md) can be done
by a human afterwards. The harness still runs S14 teardown logic in SKIP mode
and writes the full report.

Notes:
- The script is PowerShell. Run it in PowerShell (not cmd). If execution policy
  blocks it:  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
  then re-run.
- The full run takes ~3-8 minutes (docker build is the slow part the first
  time; pytest ~5s; mypy ~15s).
- If the very first run fails at S0 with "python not found", re-run with
  the venv activated (Step 2).
- Capture the FULL console output to a variable or a file as it runs. I want
  to see every stage header + its [OK]/[FAIL] line.

=== STEP 5 — If a stage FAILED, attempt ONE targeted retry ===
For each FAILED stage (not SKIP), look at its output snippet in the report:

- S1 (ruff format) or S2 (ruff check) failing on a file YOU just touched:
    run  python -m ruff format .  then  python -m ruff check . --fix  and
    re-run that one stage:  .\scripts\run_e2e.ps1 -Stages S1,S2
- S3 (mypy) failing: do NOT add type:ignore annotations or rewrite code.
    paste the mypy errors verbatim in your report and leave it as FAIL.
- S4 (pytest) failing: do NOT edit test or source code. paste the failing
    test names + assertion lines in your report and leave as FAIL.
- S5 (docker build) failing: paste the build error. Most common cause is a
    pip resolution error in Dockerfile — paste it, do NOT edit requirements.
- S7 (healthz never healthy) failing: the bot started but /healthz returns
    503. Run `docker compose logs --tail 100 bot` and paste the error.
- S8 (preflight) failing: this is the MOST useful failure — it tells you
    exactly which Discord config is wrong (token, guild, role hierarchy,
    channel perms, command sync). Paste its full output. Do NOT edit code;
    fix the .env value it points at and re-run  .\scripts\run_e2e.ps1 -Stages S8
- S9 (seed) failing: usually DB not up. Confirm `docker compose ps` shows
    `db` healthy, then re-run S9.
- S10 (smoke) failing: /healthz or /metrics returned wrong shape. Paste the
    body. Do NOT edit code.
- S11 (db_verify) failing: a migration didn't land. Paste the missing
    table/column. Do NOT edit code.
- S12 (command_audit) failing: a slash command isn't synced. Most common fix:
    the bot needs to start ONCE to sync. It already did (S6). If still
    missing, the cog failed to load — run `docker compose logs bot | findstr
    ERROR` and paste it.

Do at most ONE fix-and-retry per failed stage. If the retry still fails, move
on and report it as FAIL.

=== STEP 6 — Produce the report ===
The harness writes:
- e2e_report.md      (the structured markdown report — READ THIS)
- e2e_report.txt     (same content, plain text)
- e2e_docker_logs.txt (last 200 lines of bot container logs, if docker ran)

Your final message to me MUST contain, in this order:

1. A one-line VERDICT:  "E2E: PASS"  or  "E2E: FAIL (N stages failed)".
2. The environment block from the top of e2e_report.md (Generated / Overall /
   Host / PowerShell / Python / Total wall time / Stages X passed Y failed).
3. The full Summary table (Stage | Name | Status | Exit | Duration).
4. For each FAIL stage: its name + the last ~30 lines of its output snippet
   from the "Stage details" section of e2e_report.md.
5. For each SKIP stage: one line saying why it was skipped.
6. If any stage failed, the last 30 lines of e2e_docker_logs.txt (if it
   exists).
7. If DISCORD_TOKEN was not set, explicitly note that S8 + S12 were skipped
   and that the manual Discord checklist (scripts/E2E_CHECKLIST.md) must be
   done by a human with a token.
8. The exact final state of the containers (output of `docker compose ps`).
9. A "Next steps" line: either "all automated checks green; a human can now
   run the slash-command checklist in Discord against the still-running bot"
   OR "see failed stages above; do NOT merge until fixed".

Do NOT paste the full e2e_report.md if it is over 400 lines — instead paste
the summary table + only the FAIL/SKIP stage detail sections (PASS stages can
be summarized as one line each).

=== REMINDERS ===
- The bot container is left running (-KeepRunning). Tell me how to stop it:
    docker compose down
- If you created a .venv, tell me where it is so I can reuse it.
- Do NOT commit anything. This is a test run, not a code change.
- Do NOT push to GitHub.
- If you are unsure about a step, STOP and ask me rather than guessing.
- Keep the bot's DISCORD_TOKEN secret. Never echo it.

Begin with STEP 0 now.
```

## END PROMPT — copy up to here

---

## What you (the human) should do after Cursor reports back

1. **If E2E: PASS** — the automated half is green. The bot is still running
   (`-KeepRunning`). Open Discord and work through the manual slash-command
   checklist in `scripts/E2E_CHECKLIST.md` (Phase 0 → Phase 4 sections).
   When done, stop the bot: `docker compose down`.

2. **If E2E: FAIL** — read Cursor's report. The most common real failures:
   - **S0**: Python/Docker not on PATH, or .env missing. Fix the prereq.
   - **S5 (docker build)**: a pip resolution error in `requirements.txt`.
     Paste the error to me.
   - **S7 (healthz never healthy)**: the bot crashed on startup. The
     `e2e_docker_logs.txt` excerpt will show why (usually a config validation
     error from `core/config.py`, or a missing role/channel ID).
   - **S8 (preflight)**: the single most useful failure — it pinpoints the
     exact Discord misconfiguration (token, guild, role hierarchy, channel
     perms, command sync). Fix the `.env` value it names and re-run
     `.\scripts\run_e2e.ps1 -Stages S8`.
   - **S12 (command_audit)**: a cog failed to load (its commands never
     registered). The docker logs will name the cog + the import error.

3. **If S8 + S12 were skipped** (no DISCORD_TOKEN on this machine): the
   automated run covered lint/types/tests/docker/seed/smoke/db — but NOT
   Discord. You'll need to re-run on a machine with the bot's token, OR do
   the Discord checklist manually.

4. Paste Cursor's report back to me and I'll tell you whether anything needs
   a code fix vs. just a config tweak.

---

## Quick reference — the scripts you now have

| File | Purpose |
|------|---------|
| `scripts/run_e2e.ps1` | **Master orchestrator** — runs all 15 stages, writes the report. |
| `scripts/smoke_check.ps1` | HTTP smoke check (/healthz + /metrics). Standalone or via S10. |
| `scripts/preflight.py` | Discord config checker (token/guild/roles/channels/sync). Via S8. |
| `scripts/seed.py` | Inserts 5 settlements + 6 test citizens. Via S9. `--reset` to wipe first. |
| `scripts/db_verify.py` | Verifies DB schema (8 tables, pg_trgm, Phase 4.6 column) + seed. Via S11. |
| `scripts/command_audit.py` | Verifies all 13 slash commands are synced to the guild. Via S12. |
| `scripts/E2E_CHECKLIST.md` | The manual Discord walk-through (Phase 0 → Phase 4). |

### Run any stage in isolation

```powershell
.\scripts\run_e2e.ps1 -Stages S1          # just ruff format check
.\scripts\run_e2e.ps1 -Stages S3,S4      # just mypy + pytest
.\scripts\run_e2e.ps1 -Stages S6,S7,S9   # just bring bot up + seed
.\scripts\run_e2e.ps1 -Stages S8         # just Discord preflight
.\scripts\run_e2e.ps1 -Stages S12        # just command-sync audit
```

### Skip halves you can't run

```powershell
.\scripts\run_e2e.ps1 -SkipDocker         # lint + types + tests only (no container)
.\scripts\run_e2e.ps1 -SkipDiscord        # everything except Discord-token stages
.\scripts\run_e2e.ps1 -SkipDocker -SkipDiscord   # pure local lint/types/tests
```
