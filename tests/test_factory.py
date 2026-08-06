"""Tests for /factory commands + factorymod_api helpers — Phase B (WS-6)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api import factorymod_api

# ---------------------------------------------------------------------------
# format_item — Bukkit ItemStack dict → human-readable string
# ---------------------------------------------------------------------------


class TestFormatItem:
    """format_item converts a Bukkit ItemStack dict to a display string."""

    def test_simple_iron_ingot(self):
        item = {"type": "IRON_INGOT", "amount": 64}
        assert factorymod_api.format_item(item) == "64× Iron Ingot"

    def test_amount_defaults_to_1(self):
        item = {"type": "OAK_LOG"}
        assert factorymod_api.format_item(item) == "1× Oak Log"

    def test_custom_display_name_overrides_material(self):
        """Items with a meta.display-name show the custom name (e.g. 'Player Essence')."""
        item = {
            "type": "ENDER_EYE",
            "amount": 16,
            "meta": {"display-name": "Player Essence"},
        }
        assert factorymod_api.format_item(item) == "16× Player Essence"

    def test_strips_color_codes_from_custom_name(self):
        item = {
            "type": "EGG",
            "amount": 1,
            "meta": {"display-name": "§dEaster Egg"},
        }
        assert factorymod_api.format_item(item) == "1× Easter Egg"

    def test_unknown_type_shows_unknown(self):
        item = {"amount": 5}
        assert factorymod_api.format_item(item) == "5× Unknown"

    def test_non_dict_returns_string_repr(self):
        assert factorymod_api.format_item("plain string") == "plain string"
        assert factorymod_api.format_item(42) == "42"


# ---------------------------------------------------------------------------
# find_factory — case-insensitive factory lookup
# ---------------------------------------------------------------------------


class TestFindFactory:
    """find_factory locates a factory by key, display name, or partial match."""

    def _config(self):
        return {
            "factories": {
                "oxygen": {
                    "type": "FCC",
                    "name": "Oxygen Factory",
                    "setupcost": {},
                    "recipes": [],
                },
                "space_chem": {
                    "type": "FCC",
                    "name": "Space Chemistry Factory",
                    "setupcost": {},
                    "recipes": [],
                },
            }
        }

    def test_exact_key_match(self):
        result = factorymod_api.find_factory(self._config(), "oxygen")
        assert result is not None
        key, factory = result
        assert key == "oxygen"
        assert factory["name"] == "Oxygen Factory"

    def test_case_insensitive_key_match(self):
        result = factorymod_api.find_factory(self._config(), "OXYGEN")
        assert result is not None
        assert result[0] == "oxygen"

    def test_display_name_match(self):
        result = factorymod_api.find_factory(self._config(), "Oxygen Factory")
        assert result is not None
        assert result[0] == "oxygen"

    def test_partial_display_name_match(self):
        result = factorymod_api.find_factory(self._config(), "oxygen")
        assert result is not None
        # "space" should match "Space Chemistry Factory"
        result = factorymod_api.find_factory(self._config(), "space")
        assert result is not None
        assert result[0] == "space_chem"

    def test_not_found_returns_none(self):
        assert factorymod_api.find_factory(self._config(), "nonexistent") is None

    def test_empty_name_returns_none(self):
        assert factorymod_api.find_factory(self._config(), "") is None


# ---------------------------------------------------------------------------
# find_recipe — recipe lookup by ID or name
# ---------------------------------------------------------------------------


class TestFindRecipe:
    """find_recipe locates a recipe by ID (case-insensitive) or name."""

    def _config(self):
        return {
            "recipes": {
                "create_basic_oxygen_tank": {
                    "type": "PRODUCTION",
                    "name": "Create Basic Oxygen Tank",
                    "input": {},
                    "outputs": {},
                }
            }
        }

    def test_exact_id_match(self):
        result = factorymod_api.find_recipe(self._config(), "create_basic_oxygen_tank")
        assert result is not None
        assert result["name"] == "Create Basic Oxygen Tank"

    def test_case_insensitive_id_match(self):
        result = factorymod_api.find_recipe(self._config(), "CREATE_BASIC_OXYGEN_TANK")
        assert result is not None

    def test_partial_name_match(self):
        result = factorymod_api.find_recipe(self._config(), "basic oxygen")
        assert result is not None
        assert result["type"] == "PRODUCTION"

    def test_not_found_returns_none(self):
        assert factorymod_api.find_recipe(self._config(), "nonexistent_recipe") is None


# ---------------------------------------------------------------------------
# get_factory_recipes — resolve recipe IDs to recipe dicts
# ---------------------------------------------------------------------------


class TestGetFactoryRecipes:
    """get_factory_recipes resolves a factory's recipe ID list to recipe dicts."""

    def test_resolves_all_recipes(self):
        config = {
            "factories": {"oxygen": {"recipes": ["recipe_a", "recipe_b"]}},
            "recipes": {
                "recipe_a": {"name": "Recipe A", "type": "PRODUCTION"},
                "recipe_b": {"name": "Recipe B", "type": "PRODUCTION"},
            },
        }
        result = factorymod_api.get_factory_recipes(config, "oxygen")
        assert len(result) == 2
        assert result[0] == {"id": "recipe_a", "recipe": {"name": "Recipe A", "type": "PRODUCTION"}}
        assert result[1] == {"id": "recipe_b", "recipe": {"name": "Recipe B", "type": "PRODUCTION"}}

    def test_missing_recipe_returns_none_for_recipe(self):
        """If a recipe ID is referenced but doesn't exist, recipe is None (defensive)."""
        config = {
            "factories": {"oxygen": {"recipes": ["recipe_a", "ghost_recipe"]}},
            "recipes": {"recipe_a": {"name": "Recipe A"}},
        }
        result = factorymod_api.get_factory_recipes(config, "oxygen")
        assert len(result) == 2
        assert result[0]["recipe"] is not None
        assert result[1]["recipe"] is None

    def test_factory_with_no_recipes_returns_empty_list(self):
        config = {"factories": {"oxygen": {"recipes": []}}, "recipes": {}}
        result = factorymod_api.get_factory_recipes(config, "oxygen")
        assert result == []


# ---------------------------------------------------------------------------
# get_factorymod_config — async fetch + cache (mocked HTTP)
# ---------------------------------------------------------------------------


class TestGetFactorymodConfig:
    """get_factorymod_config fetches + parses the YAML from GitHub, cached 1h."""

    @pytest.mark.asyncio
    async def test_success_parses_yaml(self):
        factorymod_api.invalidate_cache()

        yaml_content = """
factories:
  oxygen:
    type: FCC
    name: Oxygen Factory
    setupcost:
      iron:
        type: IRON_INGOT
        amount: 64
    recipes:
      - create_oxygen
recipes:
  create_oxygen:
    type: PRODUCTION
    name: Create Oxygen
"""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value=yaml_content)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        config = await factorymod_api.get_factorymod_config(session)
        assert config is not None
        assert "oxygen" in config["factories"]
        assert config["factories"]["oxygen"]["name"] == "Oxygen Factory"

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_refetch(self):
        factorymod_api.invalidate_cache()

        yaml_content = "factories:\n  test:\n    name: Test\nrecipes: {}\n"
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value=yaml_content)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        await factorymod_api.get_factorymod_config(session)
        assert session.get.call_count == 1
        # Second call should be cached.
        await factorymod_api.get_factorymod_config(session)
        assert session.get.call_count == 1  # still 1 — served from cache

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        factorymod_api.invalidate_cache()

        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=mock_resp)

        config = await factorymod_api.get_factorymod_config(session)
        assert config is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        factorymod_api.invalidate_cache()

        session = MagicMock()
        session.get = MagicMock(side_effect=TimeoutError())
        config = await factorymod_api.get_factorymod_config(session)
        assert config is None


# ---------------------------------------------------------------------------
# Embed builders (pure functions, no I/O)
# ---------------------------------------------------------------------------


class TestBuildFactoryEmbed:
    """_build_factory_embed constructs the /factory info embed."""

    def test_builds_embed_with_setup_cost_and_recipes(self):
        from cogs.factory import _build_factory_embed

        config = {
            "factories": {
                "oxygen": {
                    "type": "FCC",
                    "name": "Oxygen Factory",
                    "setupcost": {
                        "iron": {"type": "IRON_INGOT", "amount": 64},
                        "copper": {"type": "COPPER_INGOT", "amount": 32},
                    },
                    "recipes": ["create_oxygen", "refill_oxygen"],
                }
            },
            "recipes": {
                "create_oxygen": {"name": "Create Oxygen", "type": "PRODUCTION"},
                "refill_oxygen": {"name": "Refill Oxygen", "type": "PRODUCTION"},
            },
        }
        embed = _build_factory_embed("oxygen", config["factories"]["oxygen"], config)
        assert "Oxygen Factory" in embed.title
        # Setup cost field + recipes field.
        assert len(embed.fields) >= 2

    def test_factory_with_no_setup_cost_shows_default_message(self):
        from cogs.factory import _build_factory_embed

        config = {
            "factories": {"basic": {"type": "FCC", "name": "Basic", "recipes": []}},
            "recipes": {},
        }
        embed = _build_factory_embed("basic", config["factories"]["basic"], config)
        setup_field = next(f for f in embed.fields if "Setup Cost" in f.name)
        assert "none" in setup_field.value.lower()


class TestBuildRecipeEmbed:
    """_build_recipe_embed constructs the /factory recipe embed."""

    def test_production_recipe_with_input_and_outputs(self):
        from cogs.factory import _build_recipe_embed

        recipe = {
            "type": "PRODUCTION",
            "name": "Create Basic Oxygen Tank",
            "production_time": "5s",
            "input": {"iron": {"type": "IRON_INGOT", "amount": 4}},
            "outputs": {"tank": {"type": "GLASS_BOTTLE", "amount": 1}},
        }
        embed = _build_recipe_embed("create_basic_oxygen_tank", recipe)
        assert "Create Basic Oxygen Tank" in embed.title
        assert "PRODUCTION" in embed.description

    def test_random_recipe_shows_chances(self):
        from cogs.factory import _build_recipe_embed

        recipe = {
            "type": "RANDOM",
            "name": "Forge Gold Pickaxes",
            "production_time": "2s",
            "input": {"gold": {"type": "GOLD_INGOT", "amount": 1}},
            "outputs": {
                "ub3_eff4": {
                    "chance": 0.2,
                    "ub3_eff4": {"type": "GOLDEN_PICKAXE", "amount": 1},
                },
            },
        }
        embed = _build_recipe_embed("forge_gold_pickaxes", recipe)
        # The chance should appear in the outputs field.
        outputs_field = next(f for f in embed.fields if "Outputs" in f.name)
        assert "20%" in outputs_field.value
