"""Internationalisation (i18n) scaffolding (Phase 4.5).

Extracts user-facing strings into ``locales/{lang}.json`` so the bot can be
translated without touching Python. Filipino (``fil``) is the stretch goal —
Lambat is a Filipino-themed CivMC nation, so a partial Filipino translation of
``/help`` and the monthly report resonates with the community.

Usage::

    from core.i18n import tr

    embed = discord.Embed(title=tr("help.title"), description=tr("help.description"))

    # With interpolation (named fields in the JSON template):
    msg = tr("report.month_summary", month_name="February 2026", total=42)

Design notes
------------
* **Lazy load**: locale files are read on first ``tr()`` call (or
  ``available_langs()``) and cached in-process. Reloads only happen if the
  process restarts — same as every other module-level constant.
* **Fallback chain**: ``tr(key, lang='fil')`` → look up in ``fil``; if missing,
  fall back to ``en``; if still missing, return the key itself (so missing
  strings are *visible* during development rather than silently empty).
* **No exceptions**: a malformed template or missing key logs a warning and
  returns the best-available string, never raising. The bot must keep working
  even if a locale file has a typo.
* **``Config.LOCALE``**: the default language, read from the ``LOCALE`` env
  var (default ``en``). Individual ``tr()`` calls can override per-message if
  a future per-user locale preference is added.

The scaffolding is deliberately minimal — no pluralization rules, no ICU
MessageFormat, no CLDR. For a registry bot with ~100 user-facing strings and a
single-guild audience, a flat ``key → str.format(**kwargs)`` lookup is the
right amount of machinery. If Filipino translation ever gets serious, promote
this to ``babel`` (which handles plurals + gender).
"""

import json
import logging
from pathlib import Path
from typing import Any

from core.config import Config

logger = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_LANG = "en"

# In-process cache of loaded locale tables: {lang_code: {key: template}}.
# Populated on first use by _load(); never mutated after load.
_supported: dict[str, dict[str, str]] = {}
_loaded = False


def _load() -> None:
    """Load every ``locales/*.json`` file into ``_supported``.

    Called once on first ``tr()`` / ``available_langs()`` call. Failures to
    read a single locale are logged but don't abort the load — a broken
    ``fil.json`` shouldn't take down ``en``.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not LOCALES_DIR.is_dir():
        logger.warning("Locales directory %s does not exist; i18n is a no-op.", LOCALES_DIR)
        return
    for path in sorted(LOCALES_DIR.glob("*.json")):
        lang = path.stem
        try:
            table = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(table, dict) and all(isinstance(k, str) for k in table):
                # Flatten: only keep string values (skip nested objects).
                _supported[lang] = {k: v for k, v in table.items() if isinstance(v, str)}
                logger.debug("Loaded locale %s (%d keys).", lang, len(_supported[lang]))
            else:
                logger.error("Locale %s is not a flat str→str mapping; skipped.", lang)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to load locale %s: %s", lang, e)


def available_langs() -> list[str]:
    """Return the list of loaded language codes, sorted (en first)."""
    _load()
    langs = list(_supported.keys())
    # Keep 'en' first (the fallback), then the rest alphabetically so
    # /help and config dumps are deterministic.
    return sorted(langs, key=lambda lang: (lang != DEFAULT_LANG, lang))


def tr(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """Look up a localised string by key.

    Args:
        key: dotted-free identifier (e.g. ``"help.title"``). Dots are NOT
            traversed into nested JSON — the locale file is a flat dict whose
            keys happen to use dots as a namespace convention.
        lang: override the default ``Config.LOCALE`` for this call. None
            means "use Config.LOCALE" (the normal case).
        **kwargs: format fields substituted into the template via ``str.format``.

    Returns:
        The localised string. If the key is missing in both the requested
        lang and the ``en`` fallback, returns the key itself (so a typo is
        loud, not silent).
    """
    _load()
    resolved_lang = lang or Config.LOCALE
    table = _supported.get(resolved_lang)
    template = table.get(key) if table else None
    if template is None and resolved_lang != DEFAULT_LANG:
        # Fall back to the default lang (en) for missing keys.
        fallback = _supported.get(DEFAULT_LANG, {})
        template = fallback.get(key)
    if template is None:
        logger.warning("Missing i18n key %r (lang=%s); returning the key.", key, resolved_lang)
        return key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError) as e:
        logger.error("i18n format error for key %r (lang=%s): %s", key, resolved_lang, e)
        return template


def reload_for_tests() -> None:
    """Force a reload of locale files on the next ``tr()`` call.

    Test-only helper: the in-process cache means a test that edits
    ``_supported`` (or writes a new locale file) won't be seen otherwise.
    Production code never calls this.
    """
    global _loaded
    _supported.clear()
    _loaded = False
