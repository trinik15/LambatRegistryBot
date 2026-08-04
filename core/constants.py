# constants.py


# ======================
# Settlement → Duchy seed mapping (Phase 2.3)
# ======================
# Historical source of truth for which duchy each settlement belongs to. Used
# ONLY as the migration back-fill seed for the new ``settlements.duchy`` column
# (and as a fallback if the DB column is somehow NULL). Once the column is
# populated, the DB is authoritative — this dict must not be read at runtime
# by any code path other than the migration / a defensive fallback.
SETTLEMENT_TO_DUCHY = {
    "New September": "Lambat City",
    "Pioneer": "Lambat City",
    "Sunnebourg": "Lambat City",
    "Poblacion": "Lambat City",
    "Timberbourg": "Florraine",
    "Immerheim": "Florraine",
    "Gulash": "Florraine",
    "Bazariskes": "Valle Occidental",
    "Mt. Abedul": "Valle Occidental",
    "Silenya": "Valle Occidental",
    "Heavensroost": "Valle Occidental",
    "Girasol": "Valle Occidental",
    "Tierra del Cabo": "Capeland",
    "Margaritaville": "Margaritaville",
    "Pampang": "San Canela",
}


class Colors:
    """Color codes for Discord embeds."""

    SUCCESS = 0x43B581  # green (citizen registered)
    INFO = 0x5865F2  # blurple (info, help)
    WARNING = 0xFF9900  # orange (confirm removal)
    DANGER = 0xED4245  # red (census)
    STATS = 0x57F287  # green (stats)
    SETTLEMENT = 0x2F3136  # dark grey (settlement list)
    ROSTER = 0xFEE75C  # yellow (citizen list)


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


# ======================
# Emoji e frecce per report
# ======================


class Emojis:
    """Emoji custom per province e distretti."""

    # Province (duchy)
    PROVINCE = {
        "Lambat City": "<:LCity:1410036718123483276>",
        "Florraine": "<:FL:1444959386979008653>",
        "Valle Occidental": "<:VO:1410036277629161493>",
        "Capeland": "<:Capeland:1410036380612169728>",
        "Margaritaville": "<:Margaritaville:1410036336555069471>",
        "San Canela": "",  # Nessuna emoji, come nel template
    }

    # Distretti (settlements)
    DISTRICT = {
        "New September": "<:LCity:1410036718123483276>",
        "Pioneer": "<:LCity:1410036718123483276>",
        "Timberbourg": "<:FL:1444959386979008653>",
        "Sunnebourg": "<:LCity:1410036718123483276>",
        "Immerheim": "<:FL:1444959386979008653>",
        "Bazariskes": "<:VO:1410036277629161493>",
        "Poblacion": "<:LCity:1410036718123483276>",
        "Mt. Abedul": "<:VO:1410036277629161493>",
        "Tierra del Cabo": "<:Capeland:1410036380612169728>",
        "Margaritaville": "<:Margaritaville:1410036336555069471>",
        "Silenya": "🌻",  # Nei template usano il fiore
        "Heavensroost": "<:VO:1410036277629161493>",
        "Pampang": "",  # Nessuna emoji
        "Gulash": "",  # Da verificare se serve
        "Girasol": "<:VO:1410036277629161493>",  # Da un template vecchio
    }

    # Frecce
    UP_ARROW = "<:uparrow:1400930788144447528>"
    DOWN_ARROW = "<:downarrow:1400930776773820467>"

    # Emoji speciali per i titoli
    LAMBAT = "<:Lambat:1410036577983397968>"
    LAMBAT_CHAD = "<:lambatchad:866080426435149874>"
    LAMBATAN_SALUDO = "<:lambatan_saludo:1444491661781766375>"
