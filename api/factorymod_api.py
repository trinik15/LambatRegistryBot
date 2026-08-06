"""FactoryMod config client — fetches recipe data from the CivMC/Civ GitHub repo.

Phase B (WS-6): the ``factorymod.civinfo.net`` website has no API — it's a
static React SPA that fetches ``config.yml`` directly from GitHub and parses
it client-side. We do the same server-side, caching the parsed config for 1h
(it only changes on CivMC plugin updates, which are rare).

No API key needed. No auth. No rate limit beyond GitHub's generous raw quota.
The config is ~482 KB of YAML; parsing takes ~50ms; we cache the parsed dict.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import yaml

logger = logging.getLogger(__name__)

FACTORYMOD_CONFIG_URL = (
    "https://raw.githubusercontent.com/CivMC/Civ/refs/heads/main/"
    "ansible/files/paper-config/plugins/FactoryMod/config.yml"
)

# Cache the parsed config for 1h. The YAML only changes when CivMC updates
# their FactoryMod plugin — a rare event. If GitHub is unreachable, we serve
# stale data (better than failing the /factory command entirely).
CACHE_TTL = timedelta(hours=1)

# Module-level cache. Stores (parsed_config, fetch_timestamp).
# Typed as Any to avoid mypy complaints about comparing datetime to the
# union type — the dict only ever holds these two specific shapes.
_cache: dict[str, Any] = {"data": None, "expires": datetime.min.replace(tzinfo=UTC)}


async def get_factorymod_config(session: aiohttp.ClientSession) -> dict | None:
    """Fetch and parse the FactoryMod config.yml from the CivMC/Civ GitHub repo.

    Returns the parsed YAML dict with top-level keys including ``factories``
    (61 entries) and ``recipes`` (641 entries). Cached for 1h. On fetch
    failure, serves stale cached data if available, else returns None.

    Structure (simplified)::

        {
          'factories': {
            'oxygen': {
              'type': 'FCC', 'name': 'Oxygen Factory',
              'setupcost': {item_key: {type, amount, ...}, ...},
              'recipes': ['create_basic_oxygen_tank', ...]
            }, ...
          },
          'recipes': {
            'create_basic_oxygen_tank': {
              'type': 'PRODUCTION', 'name': '...',
              'input': {item_key: {type, amount, ...}},
              'outputs': {out_key: {type, amount, ...} | {chance, item_key: {...}}}
            }, ...
          }
        }
    """
    if _cache["data"] is not None and datetime.now(UTC) < _cache["expires"]:
        return _cache["data"]

    try:
        async with session.get(FACTORYMOD_CONFIG_URL) as resp:
            if resp.status != 200:
                logger.warning(f"GitHub returned {resp.status} for FactoryMod config")
                # Serve stale if we have it (better than nothing).
                return _cache["data"]

            raw = await resp.text()
    except TimeoutError:
        logger.warning("Timeout fetching FactoryMod config from GitHub")
        return _cache["data"]
    except Exception as e:
        logger.error(f"Error fetching FactoryMod config: {e}", exc_info=True)
        return _cache["data"]

    try:
        config = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse FactoryMod config YAML: {e}")
        return _cache["data"]

    if not isinstance(config, dict) or "factories" not in config:
        logger.error("FactoryMod config missing 'factories' key — unexpected format.")
        return _cache["data"]

    _cache["data"] = config
    _cache["expires"] = datetime.now(UTC) + CACHE_TTL
    logger.info(
        f"Loaded FactoryMod config: {len(config.get('factories', {}))} factories, "
        f"{len(config.get('recipes', {}))} recipes."
    )
    return config


def invalidate_cache():
    """Drop the cached config so the next call re-fetches from GitHub."""
    _cache["data"] = None
    _cache["expires"] = datetime.min.replace(tzinfo=UTC)


def find_factory(config: dict, name: str) -> tuple[str, dict] | None:
    """Case-insensitive factory lookup by display name or YAML key.

    Returns ``(yaml_key, factory_dict)`` or ``None`` if not found. The YAML key
    is returned separately because it's used to look up the factory's recipes
    in the top-level ``recipes`` dict.
    """
    name_lower = name.lower().strip()
    if not name_lower:
        return None
    factories = config.get("factories", {})
    # First try exact key match (fastest).
    for key, factory in factories.items():
        if key.lower() == name_lower:
            return key, factory
    # Then try display name match.
    for key, factory in factories.items():
        display = factory.get("name", "")
        if display.lower() == name_lower:
            return key, factory
    # Finally try partial (contains) match on display name.
    for key, factory in factories.items():
        display = factory.get("name", "")
        if name_lower in display.lower():
            return key, factory
    return None


def find_recipe(config: dict, recipe_id: str) -> dict | None:
    """Look up a recipe by its YAML key (case-insensitive)."""
    recipes = config.get("recipes", {})
    rid_lower = recipe_id.lower().strip()
    # Exact key match.
    for _key, recipe in recipes.items():
        if _key.lower() == rid_lower:
            return recipe
    # Partial match on recipe name (not ID).
    for _key, recipe in recipes.items():
        name = recipe.get("name", "")
        if name and rid_lower in name.lower():
            return recipe
    return None


def format_item(item: dict) -> str:
    """Format a Bukkit ItemStack dict as a human-readable string.

    Example: ``{type: IRON_INGOT, amount: 64}`` → ``"64× Iron Ingot"``
    """
    import re

    if not isinstance(item, dict):
        return str(item)
    amount = item.get("amount", 1)
    material = item.get("type", "unknown")
    # Convert UPPER_SNAKE to Title Case for display: IRON_INGOT → Iron Ingot
    display = material.replace("_", " ").title()
    # Check for custom display name (named items like "Player Essence").
    meta = item.get("meta") or {}
    custom_name = meta.get("display-name") if isinstance(meta, dict) else None
    if custom_name:
        # Strip Minecraft color/format codes (§ followed by [0-9a-fk-orA-FK-OR]).
        clean = re.sub(r"§[0-9a-fk-orA-FK-OR]", "", custom_name)
        if clean.strip():
            display = clean.strip()
    return f"{amount}× {display}"


def get_factory_recipes(config: dict, factory_key: str) -> list[dict[str, object]]:
    """Return the list of recipe dicts for a factory, keyed by recipe_id.

    Each entry is ``{"id": recipe_id, "recipe": recipe_dict_or_none}``.
    If a recipe ID referenced by the factory doesn't exist in the top-level
    recipes dict (shouldn't happen, but defensive), ``recipe`` is None.
    """
    factory = config.get("factories", {}).get(factory_key, {})
    recipe_ids = factory.get("recipes", [])
    recipes = config.get("recipes", {})
    result = []
    for rid in recipe_ids:
        result.append({"id": rid, "recipe": recipes.get(rid)})
    return result
