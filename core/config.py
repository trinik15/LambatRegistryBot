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
    
    # Network & Performance
    AIOHTTP_TOTAL_TIMEOUT = int(os.getenv("AIOHTTP_TOTAL_TIMEOUT", 5))
    AIOHTTP_CONNECT_TIMEOUT = int(os.getenv("AIOHTTP_CONNECT_TIMEOUT", 3))
    COMMAND_SEMAPHORE_LIMIT = int(os.getenv("COMMAND_SEMAPHORE_LIMIT", 3))
    CIVINFO_API_RATE_LIMIT = int(os.getenv("CIVINFO_API_RATE_LIMIT", 10))
    
    # Paths
    BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")


def validate_config():
    """Validate that all required configuration is set."""
    
    # Check required environment variables
    if not Config.DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is not set. The bot cannot start.")
    
    if not Config.DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set. The bot cannot start.")
    
    if Config.OWNER_ID == 0:
        raise ValueError("OWNER_ID environment variable is not set. The bot cannot start.")
    
    # Check that at least one role ID is configured
    if not Config.FULL_ACCESS_ROLE_IDS:
        logger.warning("FULL_ACCESS_ROLE_IDS is empty. Admin commands will only work for OWNER_ID.")
    
    if Config.CITIZEN_ROLE_IDS is None or len(Config.CITIZEN_ROLE_IDS) == 0:
        raise ValueError("CITIZEN_ROLE_IDS environment variable is not set or empty. At least one role ID is required.")
    
    # Validate timeout values
    if Config.AIOHTTP_TOTAL_TIMEOUT <= 0:
        raise ValueError("AIOHTTP_TOTAL_TIMEOUT must be positive.")
    
    if Config.AIOHTTP_CONNECT_TIMEOUT <= 0:
        raise ValueError("AIOHTTP_CONNECT_TIMEOUT must be positive.")
    
    if Config.AIOHTTP_CONNECT_TIMEOUT >= Config.AIOHTTP_TOTAL_TIMEOUT:
        raise ValueError("AIOHTTP_CONNECT_TIMEOUT must be less than AIOHTTP_TOTAL_TIMEOUT.")
    
    logger.info("Configuration validated successfully.")


# Validate configuration on import
try:
    validate_config()
except ValueError as e:
    logger.critical(f"Configuration validation failed: {e}")
    raise
