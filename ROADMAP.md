# Next steps

Short list of what's done and what I'd do next. The detailed per-feature
history lives in the git log; this file is just the working list.

## Done (see git log for the commits)

- Registry core: citizens, settlements, recruiters junction, CSV import,
  self-service /apply, dossiers, search
- Reports: census, stats, trends, activity series, recruiter leaderboard,
  monthly census auto-post with duchy/settlement breakdown
- CivInfo integration: mc-accounts/full for last_login/last_logout/online,
  mc-server-status for /server trends, honest degradation when the API key
  is missing (stale flag on activity_cache instead of fake "0 active")
- Ops: pg_dump backups with retention + pre-restore safety copy, uptime
  monitor with edge-triggered alerts, audit log + retention prune, role
  reconciliation (report-only by default), churn nudges (opt-in), /metrics
  + /healthz
- Tests: pytest suite covering the pure helpers and the degradation branches;
  ruff + mypy configured; GitHub Actions runs the whole thing

## Next (roughly in order)

1. Finish the Filipino locale - only /help and the monthly report headers are
   translated right now (locales/fil.json, 28 of 39 keys). Lambat being
   Filipino-themed, it'd be nice to have it complete.
2. The `recruiter_ids` TEXT column and the `recruiters` junction table both
   get written today (dual-write). Once nothing reads the TEXT column
   anymore, drop it and keep only the junction.
3. /server online relies on mcsrvstat's players.list, which vanilla servers
   only fill via query protocol - CivMC's list can be partial or missing.
   Cross-checking with CivInfo (last_login > last_logout) may be more
   reliable; worth testing during a busy evening.
4. Snapshot history lives in monthly_snapshots; older entries are never
   pruned. Fine for now, but a yearly archive or table partition would keep
   /report trends fast on a multi-year horizon.
5. Maybe: per-settlement activity chart in /settlement info (currently only
   the national + per-duchy trends exist).
