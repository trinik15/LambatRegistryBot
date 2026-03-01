import discord
from discord import app_commands
from discord.ext import commands
from core import database as db
from services import backup
from core.config import Config
import logging
from datetime import datetime
import asyncio

logger = logging.getLogger(__name__)

class DataCog(commands.Cog):
    data_group = app_commands.Group(name="data", description="Data management commands")

    def __init__(self, bot):
        self.bot = bot

    def is_owner(self, interaction):
        return interaction.user.id == Config.OWNER_ID

    def has_full_access(self, interaction):
        if self.is_owner(interaction):
            return True
        role_ids = [r.id for r in interaction.user.roles]
        return Config.FULL_ACCESS_ROLE_ID in role_ids

    @data_group.command(name="backup", description="Create a manual backup")
    async def backup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not self.has_full_access(interaction):
            await interaction.followup.send("❌ You need the Council role to use this command.", ephemeral=True)
            return

        filename = await backup.create_backup("manual", f"by_{interaction.user.name}")
        logger.info(f"Manual backup created by {interaction.user} (ID: {interaction.user.id}): {filename}")
        await interaction.followup.send(f"✅ Backup created: `{filename}`", ephemeral=True)

    @data_group.command(name="list", description="List all available backups")
    async def list_backups(self, interaction: discord.Interaction):
        start_time = datetime.now()
        logger.info(f"list_backups started by {interaction.user.id} at {start_time}")

        await interaction.response.defer(ephemeral=True)

        if not self.has_full_access(interaction):
            await interaction.followup.send("❌ You need the Council role to use this command.", ephemeral=True)
            return

        backups = await backup.list_backups()
        logger.info(f"list_backups found {len(backups)} backups, elapsed {datetime.now() - start_time}")

        if not backups:
            await interaction.followup.send("ℹ️ No backups found. Create one with `/data backup`.", ephemeral=True)
            return

        lines = []
        for b in backups[:10]:
            age = (datetime.now() - b["created"]).days
            size = b["size"]
            if size > 1024 * 1024:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            elif size > 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} B"
            lines.append(f"`{b['filename']}` ({b['type']}) – {age}d ago, {size_str}")

        embed = discord.Embed(title="Backups", description="\n".join(lines))
        logger.info(f"list_backups completed, sending embed")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @data_group.command(name="restore", description="Restore a backup (owner only)")
    async def restore(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not self.is_owner(interaction):
            await interaction.followup.send("❌ Only the server owner can restore backups.", ephemeral=True)
            return

        backups = await backup.list_backups()
        if not backups:
            await interaction.followup.send("ℹ️ No backups available to restore. Create one with `/data backup`.", ephemeral=True)
            return

        view = BackupSelectView(backups, interaction.user.id)
        await interaction.followup.send("Select a backup to restore:", view=view, ephemeral=True)

    @data_group.command(name="reset", description="⚠️ WIPE ALL DATA (owner only) – creates a backup first")
    async def reset(self, interaction: discord.Interaction):
        """Reset the entire database: deletes all citizens and settlements."""
        await interaction.response.defer(ephemeral=True)

        if not self.is_owner(interaction):
            await interaction.followup.send("❌ Only the server owner can reset the database.", ephemeral=True)
            return

        # 1. Create a pre‑reset backup
        try:
            backup_filename = await backup.create_backup("pre_reset", f"by_{interaction.user.name}")
            logger.info(f"Pre‑reset backup created: {backup_filename}")
        except Exception as e:
            logger.error(f"Failed to create pre‑reset backup: {e}")
            await interaction.followup.send("❌ Failed to create backup. Reset cancelled.", ephemeral=True)
            return

        # 2. Show confirmation with backup info
        embed = discord.Embed(
            title="⚠️ Confirm Database Reset",
            description=(
                f"Are you sure you want to **delete all citizens and settlements**?\n\n"
                f"**Backup created:** `{backup_filename}`\n"
                f"**Type:** pre_reset\n"
                f"**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"This action is **irreversible**. You can restore later using `/data restore`."
            ),
            color=0xff9900
        )
        view = ResetConfirmView(self, backup_filename, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

async def setup(bot):
    await bot.add_cog(DataCog(bot))


class BackupSelectView(discord.ui.View):
    def __init__(self, backups: list, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.backups = backups

        options = []
        for b in backups[:25]:
            label = f"{b['filename'][:50]}"
            desc = f"{b['type']} - {b['created'].strftime('%d/%m/%Y')} ({b['size']/1024:.1f} KB)"
            options.append(discord.SelectOption(label=label, description=desc[:100], value=b['filename']))

        select = discord.ui.Select(placeholder="Choose a backup...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        selected_file = interaction.data['values'][0]
        backup_entry = next((b for b in self.backups if b['filename'] == selected_file), None)
        if not backup_entry:
            await interaction.response.edit_message(content="Backup not found.", view=None)
            return

        view = RestoreConfirmView(backup_entry['filename'], self.user_id)
        embed = discord.Embed(
            title="Confirm Restore",
            description=f"Are you sure you want to restore **{backup_entry['filename']}**?\n\n"
                        f"**Type:** {backup_entry['type']}\n"
                        f"**Created:** {backup_entry['created'].strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"**Size:** {backup_entry['size'] / 1024:.1f} KB\n\n"
                        f"⚠️ This will overwrite the current database. A backup of the current state will be created automatically.",
            color=0xff9900
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)


class RestoreConfirmView(discord.ui.View):
    def __init__(self, filename: str, user_id: int):
        super().__init__(timeout=60)
        self.filename = filename
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot confirm this action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirm.disabled = True
        self.cancel.disabled = True
        await interaction.response.edit_message(view=self)

        success = await backup.restore_backup(self.filename)

        if success:
            # Chiudi il pool e attendi che il database si stabilizzi
            await db.close_pool()
            await asyncio.sleep(3)  # Aumentato a 3 secondi
            # Forza la creazione del nuovo pool per la prossima query
            await db.get_pool()
            logger.info(f"Database restored by {interaction.user} (ID: {interaction.user.id}) from {self.filename}")
            await interaction.followup.send(f"✅ Database restored from `{self.filename}`.", ephemeral=True)
        else:
            logger.warning(f"Restore attempted by {interaction.user} (ID: {interaction.user.id}) from {self.filename} – FAILED")
            await interaction.followup.send(f"❌ Restore failed.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Restore cancelled.", embed=None, view=None)


class ResetConfirmView(discord.ui.View):
    def __init__(self, cog: DataCog, backup_filename: str, user_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.backup_filename = backup_filename
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("You cannot confirm this action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirm.disabled = True
        self.cancel.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            await db.reset_db()
            logger.info(f"Database reset by {interaction.user} (ID: {interaction.user.id}) – backup: {self.backup_filename}")
            await interaction.followup.send(
                f"✅ Database has been reset. Backup saved as `{self.backup_filename}`.\n"
                f"You can restore it anytime with `/data restore`.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Reset failed: {e}")
            await interaction.followup.send(f"❌ Reset failed: {e}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Reset cancelled.", embed=None, view=None)
