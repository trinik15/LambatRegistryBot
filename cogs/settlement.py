import discord
from discord import app_commands
from discord.ext import commands
from core import database as db           # <-- MODIFICATO
from core.config import Config            # <-- MODIFICATO
import logging
import asyncpg

logger = logging.getLogger(__name__)

class SettlementCog(commands.Cog):
    settlement_group = app_commands.Group(name="settlement", description="Settlement management commands")

    def __init__(self, bot):
        self.bot = bot

    async def settlement_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await db.execute_query(
            "SELECT name FROM settlements WHERE name LIKE $1 LIMIT 25",
            (f"{current}%",),
            fetch_all=True
        )
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

    def has_full_access(self, interaction):
        if interaction.user.id == Config.OWNER_ID:
            return True
        role_ids = [r.id for r in interaction.user.roles]
        return Config.FULL_ACCESS_ROLE_ID in role_ids

    def has_view_access(self, interaction):
        if self.has_full_access(interaction):
            return True
        role_ids = [r.id for r in interaction.user.roles]
        return Config.VIEW_ACCESS_ROLE_ID in role_ids

    @settlement_group.command(name="add", description="Add a new settlement")
    async def settlement_add(self, interaction: discord.Interaction, name: str):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message("❌ You need the Council role to use this command.", ephemeral=True)

        if len(name) > 100:
            return await interaction.response.send_message("❌ Name too long.", ephemeral=True)

        try:
            await db.execute_query("INSERT INTO settlements (name) VALUES ($1)", (name.strip(),))
            await interaction.response.send_message(f"✅ Added settlement **{name}**.")
            logger.info(f"Settlement {name} added by {interaction.user}")
        except asyncpg.UniqueViolationError:
            await interaction.response.send_message("❌ Settlement already exists.", ephemeral=True)

    @settlement_group.command(name="remove", description="Remove a settlement (must be empty)")
    @app_commands.autocomplete(name=settlement_autocomplete)
    async def settlement_remove(self, interaction: discord.Interaction, name: str):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message("❌ You need the Council role to use this command.", ephemeral=True)

        citizens = await db.execute_query("SELECT ign FROM citizens WHERE settlement = $1", (name,), fetch_all=True)
        if citizens:
            citizen_list = ", ".join([f"`{c['ign']}`" for c in citizens[:10]])
            if len(citizens) > 10:
                citizen_list += f" and {len(citizens)-10} more..."
            return await interaction.response.send_message(
                f"❌ Cannot remove `{name}` because it has {len(citizens)} citizens:\n{citizen_list}",
                ephemeral=True
            )

        view = SettlementRemoveConfirm(name, interaction.user.id)
        await interaction.response.send_message(
            f"Are you sure you want to remove settlement **{name}**? This action cannot be undone.",
            view=view,
            ephemeral=True
        )

    @settlement_group.command(name="list", description="List all settlements")
    async def settlement_list(self, interaction: discord.Interaction):
        if not self.has_view_access(interaction):
            return await interaction.response.send_message("❌ You don't have permission to view settlements.", ephemeral=True)

        # Defer because this command might take time
        await interaction.response.defer()

        rows = await db.execute_query("SELECT name FROM settlements ORDER BY name", fetch_all=True)
        if not rows:
            await interaction.followup.send(
                "ℹ️ No settlements registered. Add one with `/settlement add`.",
                ephemeral=True
            )
            return

        embed = discord.Embed(title="📍 Settlements", color=0x2F3136)
        names = "\n".join([f"• **{r['name']}**" for r in rows])
        embed.add_field(name=f"Total: {len(rows)}", value=names, inline=False)
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(SettlementCog(bot))


class SettlementRemoveConfirm(discord.ui.View):
    def __init__(self, settlement_name: str, user_id: int):
        super().__init__(timeout=60)
        self.settlement_name = settlement_name
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot confirm this action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        citizens = await db.execute_query("SELECT ign FROM citizens WHERE settlement = $1", (self.settlement_name,), fetch_all=True)
        if citizens:
            await interaction.response.edit_message(
                content=f"❌ Cannot remove `{self.settlement_name}` because it now has {len(citizens)} citizens. Action cancelled.",
                view=None
            )
            return

        affected = await db.execute_query("DELETE FROM settlements WHERE name = $1", (self.settlement_name,))
        if affected:
            await interaction.response.edit_message(
                content=f"✅ Settlement **{self.settlement_name}** has been removed.",
                view=None
            )
            logger.info(f"Settlement {self.settlement_name} removed by {interaction.user}")
        else:
            await interaction.response.edit_message(
                content=f"❌ Settlement **{self.settlement_name}** not found.",
                view=None
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"❌ Removal of **{self.settlement_name}** cancelled.",
            view=None
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
