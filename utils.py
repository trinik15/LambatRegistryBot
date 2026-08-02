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


def parse_join_date(date_str: str):
    """Parse a DD/MM/YYYY string into a datetime.date object.

    Returns None if the input cannot be parsed. Accepts DD/MM/YYYY (the
    user-facing format) and ISO YYYY-MM-DD (the format asyncpg returns when
    a DATE column is accidentally stringified).
    """
    from datetime import datetime
    if date_str is None:
        return None
    if isinstance(date_str, str):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return None
    # Already a date/datetime
    if hasattr(date_str, "year"):
        try:
            return date_str.date() if hasattr(date_str, "date") else date_str
        except Exception:
            return None
    return None


def format_date(value, fmt: str = "%d/%m/%Y") -> str:
    """Format a date/datetime/string as DD/MM/YYYY for display.

    Robust to: date objects (from asyncpg DATE columns), datetime objects,
    DD/MM/YYYY strings, YYYY-MM-DD strings, and None.
    """
    from datetime import date, datetime
    if value is None:
        return "N/A"
    if isinstance(value, (date, datetime)):
        return value.strftime(fmt)
    if isinstance(value, str):
        parsed = parse_join_date(value)
        if parsed is not None:
            return parsed.strftime(fmt)
        return value
    return str(value)
