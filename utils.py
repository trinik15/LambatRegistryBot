import discord
from discord import app_commands
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


class PaginationView(discord.ui.View):
    """A reusable pagination view for embeds."""
    def __init__(self, embeds: List[discord.Embed], user: discord.User, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.user = user
        self.current_page = 0
        self.total_pages = len(embeds)

        # Update button states based on page count
        self.update_buttons()

    def update_buttons(self):
        """Enable/disable navigation buttons based on current page."""
        self.children[0].disabled = self.current_page == 0  # Previous button
        self.children[1].disabled = self.current_page == self.total_pages - 1  # Next button

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the command user can interact with the buttons."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("You cannot control this pagination.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Explicitly stop the pagination session."""
        await interaction.response.edit_message(view=None)
        self.stop()

    async def on_timeout(self):
        """Clean up when the view times out."""
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception as e:
                logger.debug(f"Failed to remove view on timeout: {e}")


def is_valid_date(date_str: str) -> bool:
    """Validate a date string in DD/MM/YYYY format."""
    from datetime import datetime
    try:
        datetime.strptime(date_str, "%d/%m/%Y")
        return True
    except ValueError:
        return False
