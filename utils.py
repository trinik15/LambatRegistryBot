import discord
from datetime import datetime

def round_up_days(join_date_str: str, format="%d/%m/%Y") -> int:
    """Calculate days since join date, rounding up."""
    join = datetime.strptime(join_date_str, format)
    delta = datetime.now() - join
    days = delta.days
    if delta.seconds > 0:
        days += 1
    return days

def is_valid_date(date_str: str, format="%d/%m/%Y") -> bool:
    """Check if a string matches the expected date format."""
    try:
        datetime.strptime(date_str, format)
        return True
    except ValueError:
        return False

def status_emoji_from_days(days_ago: int) -> str:
    if days_ago < 30:
        return "🟢"
    elif days_ago < 60:
        return "🟠"
    else:
        return "🔴"

def format_discord_user(user_id: str) -> str:
    return f"<@{user_id}>"

def parse_recruiters(recruiter_ids_str: str) -> list:
    if not recruiter_ids_str:
        return []
    return recruiter_ids_str.split(",")

class PaginationView(discord.ui.View):
    def __init__(self, embeds: list, user_id: int, timeout=60):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.user_id = user_id
        self.current = 0
        self.update_buttons()

    def update_buttons(self):
        self.children[0].disabled = self.current == 0
        self.children[1].disabled = self.current == 0
        self.children[2].disabled = self.current == len(self.embeds) - 1
        self.children[3].disabled = self.current == len(self.embeds) - 1

    @discord.ui.button(label="⏮️ First", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Not your pagination.", ephemeral=True)
        self.current = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)

    @discord.ui.button(label="◀️ Prev", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Not your pagination.", ephemeral=True)
        self.current -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)

    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Not your pagination.", ephemeral=True)
        self.current += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)

    @discord.ui.button(label="⏭️ Last", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Not your pagination.", ephemeral=True)
        self.current = len(self.embeds) - 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current], view=self)
