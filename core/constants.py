# constants.py

class Colors:
    """Color codes for Discord embeds."""
    SUCCESS = 0x43B581      # green (citizen registered)
    INFO = 0x5865F2         # blurple (info, help)
    WARNING = 0xff9900      # orange (confirm removal)
    DANGER = 0xED4245       # red (census)
    STATS = 0x57F287        # green (stats)
    SETTLEMENT = 0x2F3136   # dark grey (settlement list)
    ROSTER = 0xFEE75C       # yellow (citizen list)

class Strings:
    """Default string values."""
    MAILBOX_DEFAULT = "Not provided"
    NOTES_DEFAULT = "None"

class Limits:
    """Character limits."""
    IGN_MAX_LENGTH = 16
    SETTLEMENT_NAME_MAX = 100
    ADDRESS_MAX = 200
    MAILBOX_MAX = 100
    NOTES_MAX = 500
