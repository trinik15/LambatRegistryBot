import os

class Config:
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    OWNER_ID = int(os.getenv("OWNER_ID", 0))
    FULL_ACCESS_ROLE_ID = int(os.getenv("FULL_ACCESS_ROLE_ID", 0))      # Council
    VIEW_ACCESS_ROLE_ID = int(os.getenv("VIEW_ACCESS_ROLE_ID", 0))      # Nobility (view only)

    # Role IDs for automatic assignment
    GUEST_ROLE_ID = int(os.getenv("GUEST_ROLE_ID", 0))
    SETTLER_ROLE_ID = int(os.getenv("SETTLER_ROLE_ID", 0))
    # Multiple citizen roles (comma-separated IDs)
    CITIZEN_ROLE_IDS = [int(x.strip()) for x in os.getenv("CITIZEN_ROLE_IDS", "").split(",") if x.strip()]

    COUNCIL_CHANNEL_ID = int(os.getenv("COUNCIL_CHANNEL_ID", 0))
    REGISTRY_CHANNEL_ID = int(os.getenv("REGISTRY_CHANNEL_ID", 0))

    # Database
    DATABASE_URL = os.getenv("DATABASE_URL")          # PostgreSQL connection string
    BACKUP_DIR = "backups"

# Validate that the Discord token is set
if not Config.DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is not set. The bot cannot start.")

# Validate that the database URL is set
if not Config.DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set. The bot cannot start.")
