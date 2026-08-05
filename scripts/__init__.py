"""Scripts package — E2E testing toolkit for the Lambat Registry Bot.

Contains:
  - preflight.py: pre-launch config/Discord checks
  - seed.py: idempotent test-data seeder
  - smoke_check.sh / .ps1: HTTP /healthz + /metrics smoke check
  - _env_loader.py: shared .env file loader (used by preflight + seed)
"""
