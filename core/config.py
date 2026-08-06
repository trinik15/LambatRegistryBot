import logging
import os

logger = logging.getLogger(__name__)


class Config:
    """Configuration class for loading environment variables."""

    # Required: Discord Token
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

    # Required: Database URL
    DATABASE_URL = os.getenv("DATABASE_URL")

    # Discord Guild ID (optional but recommended for single-server bots).
    # When set, slash commands are synced to this guild INSTANTLY on startup
    # (instead of globally, which can take up to 1 hour to propagate). This is
    # the correct choice for a nation bot that only serves one Discord server.
    # Leave 0 to use global sync (slower, but works across all servers).
    GUILD_ID = int(os.getenv("GUILD_ID", 0))

    # Required: Owner ID
    OWNER_ID = int(os.getenv("OWNER_ID", 0))

    # Role configuration (comma-separated IDs)
    FULL_ACCESS_ROLE_IDS = [
        int(x.strip()) for x in os.getenv("FULL_ACCESS_ROLE_IDS", "").split(",") if x.strip()
    ]
    VIEW_ACCESS_ROLE_ID = int(os.getenv("VIEW_ACCESS_ROLE_ID", 0))
    GUEST_ROLE_ID = int(os.getenv("GUEST_ROLE_ID", 0))
    SETTLER_ROLE_ID = int(os.getenv("SETTLER_ROLE_ID", 0))
    CITIZEN_ROLE_IDS = [
        int(x.strip()) for x in os.getenv("CITIZEN_ROLE_IDS", "").split(",") if x.strip()
    ]

    # Monthly census report destination
    # Channel that receives the monthly population snapshot, and the role to ping.
    MONTHLY_REPORT_CHANNEL_ID = int(os.getenv("MONTHLY_REPORT_CHANNEL_ID", 0))
    MONTHLY_REPORT_ROLE_ID = int(os.getenv("MONTHLY_REPORT_ROLE_ID", 0))

    # Network & Performance
    AIOHTTP_TOTAL_TIMEOUT = int(os.getenv("AIOHTTP_TOTAL_TIMEOUT", 5))
    AIOHTTP_CONNECT_TIMEOUT = int(os.getenv("AIOHTTP_CONNECT_TIMEOUT", 3))

    # Phase 4.1: graceful shutdown grace period (seconds). When the container
    # orchestrator sends SIGTERM, the bot begins an orderly shutdown — cancel
    # loops, close the HTTP session, close the DB pool, close the gateway. This
    # value is the HARD ceiling for the whole shutdown sequence: if it's
    # exceeded, we force-exit so the orchestrator never has to SIGKILL us (a
    # SIGKILL would lose in-flight log flushes + leave the gateway session
    # dangling). 15s matches Render's default grace; Docker's default is 10s —
    # set this a couple seconds BELOW your orchestrator's grace to be safe.
    SHUTDOWN_GRACE_SECONDS = int(os.getenv("SHUTDOWN_GRACE_SECONDS", 15))

    # Phase 4.5: default UI language for i18n (core/i18n.tr). ``en`` is the
    # only fully-translated locale today; ``fil`` is a partial stretch goal
    # (Lambat is Filipino-themed). Individual tr() calls can override per-
    # message if a future per-user locale preference is added. The value must
    # match a file stem in ``locales/`` (without the .json), else tr() falls
    # back to English.
    LOCALE = os.getenv("LOCALE", "en").strip().lower()

    # Cooldown configuration (in seconds)
    # These define per-user rate limits for different command categories
    COOLDOWN_FAST = int(os.getenv("COOLDOWN_FAST", 5))  # Quick commands (view, list)
    COOLDOWN_MEDIUM = int(os.getenv("COOLDOWN_MEDIUM", 15))  # Data modification commands
    COOLDOWN_SLOW = int(os.getenv("COOLDOWN_SLOW", 60))  # Expensive operations (reports, exports)
    COOLDOWN_CRITICAL = int(
        os.getenv("COOLDOWN_CRITICAL", 120)
    )  # Very expensive ops (backup, restore)

    # CivInfo API (api.civinfo.net) — player activity + server-status history.
    # The API requires authentication (contact minecraft.gjum@gmail.com for a
    # key). When set, the bot sends it as a Bearer token. When unset or
    # rejected, the bot degrades gracefully instead of silently reporting
    # "0 active" citizens.
    CIVINFO_API_KEY = os.getenv("CIVINFO_API_KEY", "").strip()
    # Base URL for the CivInfo API. Configurable so tests can point at a mock.
    CIVINFO_API_BASE = os.getenv("CIVINFO_API_BASE", "https://api.civinfo.net").strip()
    # The mcServer value sent on every CivInfo request. The official frontend
    # (civmc.netlify.app) uses the full server address "play.civmc.net", not the
    # short form "civmc". We align with the frontend to avoid surprising the
    # backend's validation/routing.
    CIVINFO_MC_SERVER = os.getenv("CIVINFO_MC_SERVER", "play.civmc.net").strip()
    # The civinfo-version header value sent on every request (mirrors Gjum's
    # official frontend). Observational today (analytics / allowlisting); update
    # to match the latest frontend release if Gjum ever makes it required.
    CIVINFO_FRONTEND_VERSION = os.getenv(
        "CIVINFO_FRONTEND_VERSION", "1189803c19bf94a23f86a15d9ef2ab9f7654b929"
    ).strip()

    # --- CivMC server status (mcsrvstat.us, no auth required) ---
    # The Minecraft server address polled by /server status and the uptime
    # monitor. Defaults to CivMC's public address.
    SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "play.civmc.net").strip()
    # mcsrvstat.us API base (v3). No key needed; ~40ms per call, polite rate
    # limit of ~5 req/min on the free tier.
    MCSRVSTAT_API_BASE = os.getenv("MCSRVSTAT_API_BASE", "https://api.mcsrvstat.us/3").strip()

    # Channel that receives edge-triggered outage / recovery alerts when the
    # CivMC server goes down or comes back up. 0 = disabled.
    ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", 0))
    # How often (seconds) to poll the server status. Default 5 min.
    UPTIME_CHECK_INTERVAL = int(os.getenv("UPTIME_CHECK_INTERVAL", 300))

    # --- Audit log (Phase 2.1) ---
    # Channel that receives a concise embed for every registry mutation (citizen
    # add/update/remove, settlement add/remove, role-sync discrepancies). This
    # is a read-only mirror of the audit_log table for the wider council.
    # 0 = disabled (mutations are still recorded in the DB, just not posted).
    AUDIT_CHANNEL_ID = int(os.getenv("AUDIT_CHANNEL_ID", 0))
    # ROADMAP §6.2 (open decision, now resolved as opt-in): rolling retention for
    # the audit_log table. 0 = keep forever (the default, matching prior
    # behaviour — text is cheap). Set e.g. 730 for a 2-year rolling window: a
    # nightly task (tasks/audit_retention.py) DELETEs rows older than this many
    # days and emits an audit.prune entry recording how many were removed, so
    # the policy itself is visible in /audit search.
    AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", 0))

    # --- Churn alerts (ROADMAP Phase 5 speculative → implemented) ---
    # Weekly task that DMs a citizen's recruiter(s) when the citizen hasn't
    # logged into CivMC for CHURN_THRESHOLD_DAYS. Opt-in because it DMs real
    # humans — defaults off so a fresh deploy never surprises recruiters.
    # Reuses activity_cache (last_login) + recruiters junction + audit_log
    # (cooldown tracking) — no new tables.
    CHURN_NUDGES_ENABLED = os.getenv("CHURN_NUDGES_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    # How many days of inactivity before a citizen is flagged for a nudge.
    # 30 = Semi-Inactive tier and worse (matches civinfo_api._bucket_activity).
    CHURN_THRESHOLD_DAYS = int(os.getenv("CHURN_THRESHOLD_DAYS", 30))
    # Per-citizen cooldown: once a nudge is *delivered* about a citizen, skip
    # them for this many days so a recruiter isn't spammed weekly. Tracked via
    # audit_log (action=churn.nudge, details->>'delivered'='true').
    CHURN_NUDGE_COOLDOWN_DAYS = int(os.getenv("CHURN_NUDGE_COOLDOWN_DAYS", 14))
    # Weekly slot (default Monday 04:00 UTC — offset from the 02:00 daily_backup
    # / daily_check and the 03:30 audit prune so the four nightly jobs don't
    # contend for the asyncpg pool).
    CHURN_NUDGE_WEEKLY_DAY = int(os.getenv("CHURN_NUDGE_WEEKLY_DAY", 0))  # Monday
    CHURN_NUDGE_WEEKLY_HOUR = int(os.getenv("CHURN_NUDGE_WEEKLY_HOUR", 4))  # 04:00 UTC

    # --- Applications channel (Phase 3.4) ---
    # Channel that receives an embed for every new /apply submission so council
    # can review and approve/reject via the buttons. 0 = disabled (applications
    # are still recorded in the DB, but no Discord notification is sent —
    # council must use /application list to see them).
    APPLICATIONS_CHANNEL_ID = int(os.getenv("APPLICATIONS_CHANNEL_ID", 0))

    # --- Governance notifications channel (Phase 3.7) ---
    # A read-only mirror of registry mutations (citizen add/update/remove,
    # settlement add/remove) for the WIDER council — a less technical audience
    # than the audit channel. If this equals AUDIT_CHANNEL_ID or is 0, no
    # separate governance post is made (the audit channel already covers it).
    GOVERNANCE_CHANNEL_ID = int(os.getenv("GOVERNANCE_CHANNEL_ID", 0))

    # --- Role reconciliation (Phase 2.5) ---
    # Weekly task that checks every citizen's Discord member still holds the
    # citizen/settler/settlement roles and lacks the guest role. Discrepancies
    # are logged to the audit channel (and the audit_log table). When
    # ROLE_SYNC_AUTO=true the bot also re-applies the correct roles; otherwise
    # it only reports.
    ROLE_SYNC_AUTO = os.getenv("ROLE_SYNC_AUTO", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    # Day-of-week (0=Mon … 6=Sun) and hour (UTC) to run the weekly check.
    ROLE_SYNC_WEEKLY_DAY = int(os.getenv("ROLE_SYNC_WEEKLY_DAY", 0))  # Monday
    ROLE_SYNC_WEEKLY_HOUR = int(os.getenv("ROLE_SYNC_WEEKLY_HOUR", 3))  # 03:00 UTC

    # Paths
    BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")

    # --- Backups: off-site sink + retention (Phase 0 hardening) ---
    # Where backups are uploaded in addition to the local BACKUP_DIR copy.
    # One of: local, s3, gcs. "local" keeps the legacy behaviour (disk only).
    # S3 requires `pip install boto3` (creds via the standard AWS credential
    # chain — env vars, profile, or IAM role). GCS requires
    # `pip install google-cloud-storage` (creds via GOOGLE_APPLICATION_CREDENTIALS).
    BACKUP_SINK = os.getenv("BACKUP_SINK", "local").strip().lower()
    BACKUP_S3_BUCKET = os.getenv("BACKUP_S3_BUCKET", "").strip()
    BACKUP_S3_PREFIX = os.getenv("BACKUP_S3_PREFIX", "").strip()
    BACKUP_GCS_BUCKET = os.getenv("BACKUP_GCS_BUCKET", "").strip()
    BACKUP_GCS_PREFIX = os.getenv("BACKUP_GCS_PREFIX", "").strip()
    # How many "auto" (daily) backups to keep on disk. Older auto backups are
    # pruned after each create. "manual" and "pre_restore" backups are NEVER
    # pruned. Monthly backups (note="monthly", generated on the 1st) are also
    # preserved.
    BACKUP_RETENTION_DAILY = int(os.getenv("BACKUP_RETENTION_DAILY", 30))

    # --- Optional ---
    # HTTP proxy URL for outbound requests (blank/None = direct). Routed
    # through Config rather than read ad-hoc in main.py so it's validated
    # and consistent with the rest of the configuration.
    PROXY_URL = (os.getenv("PROXY_URL", "") or "").strip() or None
    # Port for the keep-alive/health server (Render/Railway liveness + /healthz
    # and /metrics). Render injects PORT automatically.
    PORT = int(os.getenv("PORT", 10000))

    @classmethod
    def validate_config(cls):
        """Validate that all required configuration is set."""
        # Check required environment variables
        if not cls.DISCORD_TOKEN:
            raise ValueError("DISCORD_TOKEN environment variable is not set. The bot cannot start.")
        if not cls.DATABASE_URL:
            raise ValueError("DATABASE_URL environment variable is not set. The bot cannot start.")
        if cls.OWNER_ID == 0:
            raise ValueError("OWNER_ID environment variable is not set. The bot cannot start.")

        # Check that at least one role ID is configured
        if not cls.FULL_ACCESS_ROLE_IDS:
            logger.warning(
                "FULL_ACCESS_ROLE_IDS is empty. Admin commands will only work for OWNER_ID."
            )
        if cls.CITIZEN_ROLE_IDS is None or len(cls.CITIZEN_ROLE_IDS) == 0:
            raise ValueError(
                "CITIZEN_ROLE_IDS environment variable is not set or empty. At least one role ID is required."
            )

        # Validate timeout values
        if cls.AIOHTTP_TOTAL_TIMEOUT <= 0:
            raise ValueError("AIOHTTP_TOTAL_TIMEOUT must be positive.")
        if cls.AIOHTTP_CONNECT_TIMEOUT <= 0:
            raise ValueError("AIOHTTP_CONNECT_TIMEOUT must be positive.")
        if cls.AIOHTTP_CONNECT_TIMEOUT >= cls.AIOHTTP_TOTAL_TIMEOUT:
            raise ValueError("AIOHTTP_CONNECT_TIMEOUT must be less than AIOHTTP_TOTAL_TIMEOUT.")

        # Phase 4.1: shutdown grace must be at least 3s — anything less risks
        # an incomplete close (loops not cancelled, DB pool not released) and
        # defeats the purpose of graceful shutdown. The container will just
        # SIGKILL us anyway in that case.
        if cls.SHUTDOWN_GRACE_SECONDS < 3:
            raise ValueError("SHUTDOWN_GRACE_SECONDS must be at least 3 seconds.")

        # Validate cooldown values
        if any(
            x <= 0
            for x in [
                cls.COOLDOWN_FAST,
                cls.COOLDOWN_MEDIUM,
                cls.COOLDOWN_SLOW,
                cls.COOLDOWN_CRITICAL,
            ]
        ):
            raise ValueError("All COOLDOWN_* values must be positive integers.")

        # Validate uptime poll interval (must be >= 60s to respect mcsrvstat's
        # free-tier rate limit of ~5 req/min).
        if cls.UPTIME_CHECK_INTERVAL < 60:
            raise ValueError(
                "UPTIME_CHECK_INTERVAL must be at least 60 seconds (mcsrvstat.us rate limit)."
            )

        # Validate backup sink configuration.
        if cls.BACKUP_SINK not in ("local", "s3", "gcs"):
            raise ValueError(
                f"BACKUP_SINK must be one of: local, s3, gcs (got {cls.BACKUP_SINK!r})."
            )
        if cls.BACKUP_SINK == "s3" and not cls.BACKUP_S3_BUCKET:
            raise ValueError("BACKUP_SINK=s3 requires BACKUP_S3_BUCKET to be set.")
        if cls.BACKUP_SINK == "gcs" and not cls.BACKUP_GCS_BUCKET:
            raise ValueError("BACKUP_SINK=gcs requires BACKUP_GCS_BUCKET to be set.")
        if cls.BACKUP_RETENTION_DAILY < 1:
            raise ValueError("BACKUP_RETENTION_DAILY must be at least 1.")

        # Validate role-sync schedule bounds.
        if not 0 <= cls.ROLE_SYNC_WEEKLY_DAY <= 6:
            raise ValueError("ROLE_SYNC_WEEKLY_DAY must be 0-6 (Mon-Sun).")
        if not 0 <= cls.ROLE_SYNC_WEEKLY_HOUR <= 23:
            raise ValueError("ROLE_SYNC_WEEKLY_HOUR must be 0-23 (UTC).")

        # Validate audit retention. 0 = disabled (keep forever); any positive
        # int enables the nightly prune. Negative values are meaningless.
        if cls.AUDIT_RETENTION_DAYS < 0:
            raise ValueError("AUDIT_RETENTION_DAYS must be >= 0 (0 = keep forever).")

        # Validate churn-alert config (ROADMAP Phase 5 → implemented).
        if cls.CHURN_THRESHOLD_DAYS < 1:
            raise ValueError("CHURN_THRESHOLD_DAYS must be at least 1.")
        if cls.CHURN_NUDGE_COOLDOWN_DAYS < 1:
            raise ValueError("CHURN_NUDGE_COOLDOWN_DAYS must be at least 1.")
        if not 0 <= cls.CHURN_NUDGE_WEEKLY_DAY <= 6:
            raise ValueError("CHURN_NUDGE_WEEKLY_DAY must be 0-6 (Mon-Sun).")
        if not 0 <= cls.CHURN_NUDGE_WEEKLY_HOUR <= 23:
            raise ValueError("CHURN_NUDGE_WEEKLY_HOUR must be 0-23 (UTC).")

        logger.info("Configuration validated successfully.")


# Validate configuration on import
try:
    Config.validate_config()
except ValueError as e:
    logger.critical(f"Configuration validation failed: {e}")
    raise
