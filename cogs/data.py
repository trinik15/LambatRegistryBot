# cogs/data.py
import discord
from discord import app_commands
from discord.ext import commands
from core.config import Config
from services import backup
from api import civinfo_api
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class DataCog(commands.Cog):
    data_group = app_commands.Group(name="data", description="Data management and backup commands")

    def __init__(self, bot):
        self.bot = bot

    def has_full_access(self, interaction: discord.Interaction) -> bool:
        """Check if a user has full access (owner or role-based)."""
        # Owner has full access
        if interaction.user.id == Config.OWNER_ID:
            return True
        # DM context: no roles available — deny (only owner passes above).
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        # Check if user has any of the full access roles
        user_role_ids = [r.id for r in interaction.user.roles]
        return any(role_id in Config.FULL_ACCESS_ROLE_IDS for role_id in user_role_ids)

    def invalidate_all_caches(self):
        """Drop every in-memory cache after a DB restore replaces the data.

        Without this the autocomplete lists, settlement cache and CivInfo
        activity cache would keep serving the *pre-restore* state until their
        TTLs expired — i.e. the bot would lie about data that no longer exists.
        """
        # CivInfo activity cache (per-IGN lookups).
        try:
            civinfo_api.cache.clear()
        except Exception as e:
            logger.warning(f"Could not clear CivInfo cache: {e}")

        # CitizenCog autocomplete cache (IGN + settlement name lists).
        citizen_cog = self.bot.get_cog("CitizenCog")
        if citizen_cog and getattr(citizen_cog, "autocomplete_cache", None):
            try:
                citizen_cog.autocomplete_cache.invalidate_citizen_cache()
                citizen_cog.autocomplete_cache.invalidate_settlement_cache()
            except Exception as e:
                logger.warning(f"Could not clear CitizenCog cache: {e}")

        # SettlementCog name cache.
        settlement_cog = self.bot.get_cog("SettlementCog")
        if settlement_cog and hasattr(settlement_cog, "invalidate_cache"):
            try:
                settlement_cog.invalidate_cache()
            except Exception as e:
                logger.warning(f"Could not clear SettlementCog cache: {e}")

        logger.info("All in-memory caches invalidated after restore.")

    async def backup_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for backup filenames."""
        backups = await backup.list_backups()
        filtered = [b for b in backups if current.lower() in b["filename"].lower()]
        return [
            app_commands.Choice(name=f"{b['filename']} ({b['created'].strftime('%Y-%m-%d')})", value=b["filename"])
            for b in filtered[:25]
        ]

    @data_group.command(name="backup", description="Create a manual database backup")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_CRITICAL, key=lambda i: (i.user.id, "data_backup"))
    async def data_backup(self, interaction: discord.Interaction, note: Optional[str] = None):
        """Create a manual backup."""
        # Check full access permission
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            filename = await backup.create_backup("manual", note or "manual_backup")
            embed = discord.Embed(
                title="✅ Backup Created",
                description=f"Backup file: `{filename}`",
                color=0x43B581
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Backup creation failed: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ Failed to create backup. Check logs for details.",
                ephemeral=True
            )

    @data_group.command(name="list_backups", description="List all available backups")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "data_list_backups"))
    async def data_list_backups(self, interaction: discord.Interaction):
        """List all available backups."""
        # Check full access permission
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        backups = await backup.list_backups()

        if not backups:
            await interaction.followup.send("No backups found.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📦 Available Backups",
            color=0x7289DA
        )

        backup_text = []
        for b in backups[:20]:
            backup_text.append(f"• `{b['filename']}`")
            backup_text.append(f"  {b['created'].strftime('%Y-%m-%d %H:%M')} - {b['size'] // 1024}KB")
            if b.get("note"):
                backup_text.append(f"  Note: {b['note']}")

        if backup_text:
            embed.description = "\n".join(backup_text)
        else:
            embed.description = "No backups found."

        await interaction.followup.send(embed=embed, ephemeral=True)

    @data_group.command(name="restore", description="Restore database from backup")
    @app_commands.autocomplete(filename=backup_autocomplete)
    @app_commands.checks.cooldown(1, Config.COOLDOWN_CRITICAL, key=lambda i: (i.user.id, "data_restore"))
    async def data_restore(self, interaction: discord.Interaction, filename: str):
        """Restore database from a backup file."""
        # Check full access permission (owner or role-based)
        # This is a critical operation - only users with full access can perform it
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # Verify backup exists
        backups = await backup.list_backups()
        backup_exists = any(b["filename"] == filename for b in backups)

        if not backup_exists:
            await interaction.followup.send(
                f"❌ Backup `{filename}` not found.",
                ephemeral=True
            )
            return

        # Confirmation view - checks full access for both confirm and cancel
        class RestoreConfirm(discord.ui.View):
            def __init__(self, cog, requester_id):
                super().__init__(timeout=30)
                self.cog = cog
                self.requester_id = requester_id

            @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
            async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
                # Verify the user still has full access
                if not self.cog.has_full_access(interaction):
                    return await interaction.response.send_message(
                        "❌ You no longer have permission to restore backups.",
                        ephemeral=True
                    )
                if interaction.user.id != self.requester_id:
                    return await interaction.response.send_message(
                        "You did not initiate this restore.",
                        ephemeral=True
                    )

                await interaction.response.defer(ephemeral=True)
                try:
                    # restore_backup now RAISES on any failure (missing file,
                    # pg_dump/psql error, or timeout) instead of returning False,
                    # so reaching the success branch means it really succeeded.
                    await backup.restore_backup(filename)
                    # The DB was replaced under us — drop every stale cache so
                    # autocomplete / activity lookups reflect the restored data.
                    self.cog.invalidate_all_caches()
                    embed = discord.Embed(
                        title="✅ Database Restored",
                        description=(
                            f"Successfully restored from `{filename}`.\n"
                            f"All in-memory caches have been cleared."
                        ),
                        color=0x43B581
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                except Exception as e:
                    logger.error(f"Restore failed: {e}", exc_info=True)
                    await interaction.followup.send(
                        f"❌ Restore failed: {e}\n\n"
                        f"An emergency backup was attempted before the restore; "
                        f"check the backups folder.",
                        ephemeral=True
                    )
                self.stop()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
                # Verify the user still has full access
                if not self.cog.has_full_access(interaction):
                    return await interaction.response.send_message(
                        "❌ You no longer have permission to restore backups.",
                        ephemeral=True
                    )
                if interaction.user.id != self.requester_id:
                    return await interaction.response.send_message(
                        "You did not initiate this restore.",
                        ephemeral=True
                    )
                await interaction.response.send_message("Restore cancelled.", ephemeral=True)
                self.stop()

        embed = discord.Embed(
            title="⚠️ Confirm Database Restore",
            description=f"Are you sure you want to restore from `{filename}`?\n\nThis will overwrite the current database and cannot be undone.",
            color=0xff9900
        )
        view = RestoreConfirm(self, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(DataCog(bot))
