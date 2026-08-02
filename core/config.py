import os
import logging

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
    FULL_ACCESS_ROLE_IDS = [int(x.strip()) for x in os.getenv("FULL_ACCESS_ROLE_IDS", "").split(",") if x.strip()]
    VIEW_ACCESS_ROLE_ID = int(os.getenv("VIEW_ACCESS_ROLE_ID", 0))
    GUEST_ROLE_ID = int(os.getenv("GUEST_ROLE_ID", 0))
    SETTLER_ROLE_ID = int(os.getenv("SETTLER_ROLE_ID", 0))
    CITIZEN_ROLE_IDS = [int(x.strip()) for x in os.getenv("CITIZEN_ROLE_IDS", "").split(",") if x.strip()]

    # Monthly census report destination
    # Channel that receives the monthly population snapshot, and the role to ping.
    MONTHLY_REPORT_CHANNEL_ID = int(os.getenv("MONTHLY_REPORT_CHANNEL_ID", 0))
    MONTHLY_REPORT_ROLE_ID = int(os.getenv("MONTHLY_REPORT_ROLE_ID", 0))

    # Network & Performance
    AIOHTTP_TOTAL_TIMEOUT = int(os.getenv("AIOHTTP_TOTAL_TIMEOUT", 5))
    AIOHTTP_CONNECT_TIMEOUT = int(os.getenv("AIOHTTP_CONNECT_TIMEOUT", 3))

    # Cooldown configuration (in seconds)
    # These define per-user rate limits for different command categories
    COOLDOWN_FAST = int(os.getenv("COOLDOWN_FAST", 5))          # Quick commands (view, list)
    COOLDOWN_MEDIUM = int(os.getenv("COOLDOWN_MEDIUM", 15))     # Data modification commands
    COOLDOWN_SLOW = int(os.getenv("COOLDOWN_SLOW", 60))         # Expensive operations (reports, exports)
    COOLDOWN_CRITICAL = int(os.getenv("COOLDOWN_CRITICAL", 120)) # Very expensive ops (backup, restore)

    # CivInfo API key (optional but recommended).
    # The CivInfo API now requires authentication (contact
    # minecraft.gjum@gmail.com for a key). When set, the bot sends it as a
    # Bearer token. When unset or rejected, the bot degrades gracefully
    # instead of silently reporting "0 active" citizens.
    CIVINFO_API_KEY = os.getenv("CIVINFO_API_KEY", "").strip()

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

    # Paths
    BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")

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
            logger.warning("FULL_ACCESS_ROLE_IDS is empty. Admin commands will only work for OWNER_ID.")
        if cls.CITIZEN_ROLE_IDS is None or len(cls.CITIZEN_ROLE_IDS) == 0:
            raise ValueError("CITIZEN_ROLE_IDS environment variable is not set or empty. At least one role ID is required.")

        # Validate timeout values
        if cls.AIOHTTP_TOTAL_TIMEOUT <= 0:
            raise ValueError("AIOHTTP_TOTAL_TIMEOUT must be positive.")
        if cls.AIOHTTP_CONNECT_TIMEOUT <= 0:
            raise ValueError("AIOHTTP_CONNECT_TIMEOUT must be positive.")
        if cls.AIOHTTP_CONNECT_TIMEOUT >= cls.AIOHTTP_TOTAL_TIMEOUT:
            raise ValueError("AIOHTTP_CONNECT_TIMEOUT must be less than AIOHTTP_TOTAL_TIMEOUT.")

        # Validate cooldown values
        if any(x <= 0 for x in [cls.COOLDOWN_FAST, cls.COOLDOWN_MEDIUM, cls.COOLDOWN_SLOW, cls.COOLDOWN_CRITICAL]):
            raise ValueError("All COOLDOWN_* values must be positive integers.")

        # Validate uptime poll interval (must be >= 60s to respect mcsrvstat's
        # free-tier rate limit of ~5 req/min).
        if cls.UPTIME_CHECK_INTERVAL < 60:
            raise ValueError("UPTIME_CHECK_INTERVAL must be at least 60 seconds (mcsrvstat.us rate limit).")

        logger.info("Configuration validated successfully.")


# Validate configuration on import
try:
    Config.validate_config()
except ValueError as e:
    logger.critical(f"Configuration validation failed: {e}")
    raise
