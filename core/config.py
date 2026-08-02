import os
import logging

logger = logging.getLogger(__name__)


class Config:
    """Configuration class for loading environment variables."""

    # Required: Discord Token
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

    # Required: Database URL
    DATABASE_URL = os.getenv("DATABASE_URL")

    # Required: Owner ID
    OWNER_ID = int(os.getenv("OWNER_ID", 0))

    # Role configuration (comma-separated IDs)
    FULL_ACCESS_ROLE_IDS = [int(x.strip()) for x in os.getenv("FULL_ACCESS_ROLE_IDS", "").split(",") if x.strip()]
    VIEW_ACCESS_ROLE_ID = int(os.getenv("VIEW_ACCESS_ROLE_ID", 0))
    GUEST_ROLE_ID = int(os.getenv("GUEST_ROLE_ID", 0))
    SETTLER_ROLE_ID = int(os.getenv("SETTLER_ROLE_ID", 0))
    CITIZEN_ROLE_IDS = [int(x.strip()) for x in os.getenv("CITIZEN_ROLE_IDS", "").split(",") if x.strip()]

    # Channel configuration
    COUNCIL_CHANNEL_ID = int(os.getenv("COUNCIL_CHANNEL_ID", 0))
    REGISTRY_CHANNEL_ID = int(os.getenv("REGISTRY_CHANNEL_ID", 0))
    AUDIT_LOG_CHANNEL_ID = int(os.getenv("AUDIT_LOG_CHANNEL_ID", 0))

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

    # CivInfo API rate limiting
    CIVINFO_API_RATE_LIMIT = int(os.getenv("CIVINFO_API_RATE_LIMIT", 10))

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

        logger.info("Configuration validated successfully.")


# Validate configuration on import
try:
    Config.validate_config()
except ValueError as e:
    logger.critical(f"Configuration validation failed: {e}")
    raise
