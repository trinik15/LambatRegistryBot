"""FactoryMod recipe cog — /factory info|list|recipe (Phase B, WS-6).

CivMC relevance: FactoryMod is the core production mechanic on CivMC. Players
need to know setup costs and recipe inputs before committing resources. This
cog fetches the FactoryMod config.yml from the CivMC/Civ GitHub repo (the same
source ``factorymod.civinfo.net`` uses) and renders it in Discord.

No API key needed. Config is cached 1h (changes only on CivMC plugin updates).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from api import factorymod_api
from core.config import Config

logger = logging.getLogger(__name__)

# Cap the number of recipes shown in /factory info to avoid hitting Discord's
# 25-field embed limit. If a factory has more, we list the rest as a compact
# comma-separated string.
MAX_RECIPE_FIELDS = 15


class FactoryCog(commands.Cog):
    """``/factory info|list|recipe`` — FactoryMod recipe lookup."""

    factory_group = app_commands.Group(
        name="factory", description="FactoryMod recipe and setup-cost lookup"
    )

    def __init__(self, bot):
        self.bot = bot

    @factory_group.command(name="info", description="Show a factory's setup cost + recipes")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "factory_info")
    )
    async def factory_info(self, interaction: discord.Interaction, name: str):
        """Show factory type, setup cost, and recipe list."""
        await interaction.response.defer()

        config = await factorymod_api.get_factorymod_config(self.bot.http_session)
        if not config:
            await interaction.followup.send(
                "⚠️ Couldn't load the FactoryMod config from GitHub. Try again in a moment.",
                ephemeral=True,
            )
            return

        match = factorymod_api.find_factory(config, name)
        if not match:
            await interaction.followup.send(
                f"❌ No factory named '{name}'. Use `/factory list` to see all 61 factories.",
                ephemeral=True,
            )
            return

        factory_key, factory = match
        embed = _build_factory_embed(factory_key, factory, config)
        await interaction.followup.send(embed=embed)

    @factory_group.command(name="list", description="List all FactoryMod factories")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "factory_list")
    )
    async def factory_list(self, interaction: discord.Interaction):
        """List all 61 factories grouped by type."""
        await interaction.response.defer()

        config = await factorymod_api.get_factorymod_config(self.bot.http_session)
        if not config:
            await interaction.followup.send(
                "⚠️ Couldn't load the FactoryMod config from GitHub. Try again in a moment.",
                ephemeral=True,
            )
            return

        factories = config.get("factories", {})
        embed = _build_factory_list_embed(factories)
        await interaction.followup.send(embed=embed)

    @factory_group.command(name="recipe", description="Show a recipe's inputs and outputs")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "factory_recipe")
    )
    async def factory_recipe(self, interaction: discord.Interaction, name: str):
        """Show a specific recipe's inputs, outputs, and production time."""
        await interaction.response.defer()

        config = await factorymod_api.get_factorymod_config(self.bot.http_session)
        if not config:
            await interaction.followup.send(
                "⚠️ Couldn't load the FactoryMod config from GitHub. Try again in a moment.",
                ephemeral=True,
            )
            return

        recipe = factorymod_api.find_recipe(config, name)
        if not recipe:
            await interaction.followup.send(
                f"❌ No recipe named '{name}'. Recipe IDs use snake_case "
                "(e.g. `create_basic_oxygen_tank`).",
                ephemeral=True,
            )
            return

        embed = _build_recipe_embed(name, recipe)
        await interaction.followup.send(embed=embed)

    @factory_info.autocomplete("name")
    @factory_recipe.autocomplete("name")
    async def factory_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete factory + recipe names (case-insensitive partial match)."""
        config = await factorymod_api.get_factorymod_config(self.bot.http_session)
        if not config:
            return []

        current_lower = current.lower().strip()
        choices: list[app_commands.Choice[str]] = []

        # Search factories by display name.
        for key, factory in config.get("factories", {}).items():
            display = factory.get("name", key)
            if current_lower in display.lower() or current_lower in key.lower():
                choices.append(app_commands.Choice(name=display, value=display))
            if len(choices) >= 25:
                return choices

        # If few factory matches, also search recipe IDs.
        if len(choices) < 10:
            for rid, recipe in config.get("recipes", {}).items():
                rname = recipe.get("name", rid) if isinstance(recipe, dict) else rid
                if current_lower in rid.lower() or (rname and current_lower in rname.lower()):
                    choices.append(app_commands.Choice(name=rname or rid, value=rid))
                if len(choices) >= 25:
                    break

        return choices[:25]


# ---------------------------------------------------------------------------
# Pure helpers (testable without Discord / DB)
# ---------------------------------------------------------------------------


def _build_factory_embed(factory_key: str, factory: dict, config: dict) -> discord.Embed:
    """Build the embed for /factory info."""
    display_name = factory.get("name", factory_key)
    factory_type = factory.get("type", "unknown")
    recipes_list = factory.get("recipes", [])

    embed = discord.Embed(
        title=f"🏭 {display_name}",
        description=f"Type: `{factory_type}` • Key: `{factory_key}`",
        color=0xFAA61A,  # amber — industrial
    )

    # Setup cost
    setupcost = factory.get("setupcost") or {}
    if setupcost:
        cost_lines = []
        for _item_key, item_data in setupcost.items():
            cost_lines.append(f"📦 {factorymod_api.format_item(item_data)}")
        embed.add_field(
            name=f"🔧 Setup Cost ({len(setupcost)} items)",
            value="\n".join(cost_lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="🔧 Setup Cost", value="*(none — default factory)*", inline=False)

    # Recipes — show up to MAX_RECIPE_FIELDS as a list, rest as compact string.
    if recipes_list:
        recipe_details = factorymod_api.get_factory_recipes(config, factory_key)
        recipe_lines = []
        for rd in recipe_details[:MAX_RECIPE_FIELDS]:
            rid = rd["id"]
            recipe = rd["recipe"]
            if recipe and isinstance(recipe, dict):
                rname = recipe.get("name", rid)
                recipe_lines.append(f"• **{rname}** (`{rid}`)")
            else:
                recipe_lines.append(f"• `{rid}`")

        if len(recipe_details) > MAX_RECIPE_FIELDS:
            remaining = len(recipe_details) - MAX_RECIPE_FIELDS
            recipe_lines.append(f"*...and {remaining} more — use /factory recipe <id> for details.*")

        embed.add_field(
            name=f"📋 Recipes ({len(recipes_list)} total)",
            value="\n".join(recipe_lines)[:1024],
            inline=False,
        )

    embed.set_footer(text="Data: github.com/CivMC/Civ • Cached 1h")
    return embed


def _build_factory_list_embed(factories: dict) -> discord.Embed:
    """Build the embed for /factory list — all factories grouped by type."""
    # Group factories by type.
    by_type: dict[str, list[tuple[str, str]]] = {}
    for key, factory in factories.items():
        ftype = factory.get("type", "unknown")
        display = factory.get("name", key)
        by_type.setdefault(ftype, []).append((key, display))

    embed = discord.Embed(
        title="🏭 FactoryMod Factories",
        description=f"**{len(factories)} factories** in {len(by_type)} types. "
        "Use `/factory info <name>` for setup costs + recipes.",
        color=0xFAA61A,
    )

    for ftype, entries in sorted(by_type.items()):
        # Sort alphabetically by display name.
        entries.sort(key=lambda x: x[1].lower())
        lines = [f"• {display} (`{key}`)" for key, display in entries]
        embed.add_field(
            name=f"{ftype} ({len(entries)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )

    embed.set_footer(text="Data: github.com/CivMC/Civ • Cached 1h")
    return embed


def _build_recipe_embed(recipe_id: str, recipe: dict) -> discord.Embed:
    """Build the embed for /factory recipe."""
    rname = recipe.get("name", recipe_id)
    rtype = recipe.get("type", "unknown")
    prod_time = recipe.get("production_time", "unknown")

    embed = discord.Embed(
        title=f"📋 {rname}",
        description=f"Type: `{rtype}` • ID: `{recipe_id}` • Time: `{prod_time}`",
        color=0x3BAD4C,
    )

    # Input (singular in FactoryMod config — one input slot per recipe).
    input_data = recipe.get("input")
    if input_data:
        if isinstance(input_data, dict):
            input_lines = []
            for _item_key, item_data in input_data.items():
                input_lines.append(f"📥 {factorymod_api.format_item(item_data)}")
            embed.add_field(
                name="Input",
                value="\n".join(input_lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(name="Input", value=str(input_data), inline=False)
    else:
        embed.add_field(name="Input", value="*(none)*", inline=False)

    # Outputs (dict — for RANDOM type, each has a chance; for PRODUCTION, fixed).
    outputs = recipe.get("outputs")
    if outputs and isinstance(outputs, dict):
        output_lines = []
        for out_key, out_data in outputs.items():
            if isinstance(out_data, dict):
                # RANDOM type: {chance: 0.2, item_key: {type, amount, ...}}
                if "chance" in out_data:
                    chance = out_data.get("chance", 0)
                    # The actual item is nested under a key matching out_key.
                    item = out_data.get(out_key, out_data)
                    pct = f"{chance * 100:.0f}%" if chance <= 1 else f"{chance}%"
                    output_lines.append(f"📤 {factorymod_api.format_item(item)} ({pct})")
                else:
                    # PRODUCTION type: {type, amount, ...} directly.
                    output_lines.append(f"📤 {factorymod_api.format_item(out_data)}")
            else:
                output_lines.append(f"📤 {out_data}")
        embed.add_field(
            name=f"Outputs ({len(outputs)})",
            value="\n".join(output_lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Outputs", value="*(none)*", inline=False)

    embed.set_footer(text="Data: github.com/CivMC/Civ • Cached 1h")
    return embed


async def setup(bot):
    await bot.add_cog(FactoryCog(bot))
