"""Self-service applications cog — Phase 3.4.

Non-citizens run ``/apply ign settlement recruiter`` to submit a citizenship
application. The application is posted to APPLICATIONS_CHANNEL_ID with
Approve/Reject buttons. Council approval triggers the normal citizen_add path
(DB insert + role assignment + audit + governance notification).
"""

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core import database as db
from core.config import Config
from services import applications as apps
from services import audit

logger = logging.getLogger(__name__)

_IGN_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")


class ApplicationsCog(commands.Cog):
    """``/apply`` + ``/application list`` — self-service citizenship."""

    application_group = app_commands.Group(
        name="application", description="Manage citizen applications"
    )

    def __init__(self, bot):
        self.bot = bot

    def has_full_access(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == Config.OWNER_ID:
            return True
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        user_role_ids = [r.id for r in interaction.user.roles]
        return any(role_id in Config.FULL_ACCESS_ROLE_IDS for role_id in user_role_ids)

    @app_commands.command(name="apply", description="Apply for Lambat citizenship")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_SLOW, key=lambda i: (i.user.id, "apply"))
    @app_commands.describe(
        ign="Your Minecraft IGN (3-16 chars, alphanumeric + underscore)",
        settlement="The settlement you want to join",
        recruiter="The Discord user who recruited you (optional)",
    )
    async def apply(
        self,
        interaction: discord.Interaction,
        ign: str,
        settlement: str,
        recruiter: discord.User | None = None,
    ):
        """Phase 3.4: self-service application submission."""
        # Validate IGN.
        ign = ign.strip()
        if not _IGN_PATTERN.match(ign):
            await interaction.response.send_message(
                "❌ Invalid IGN. Must be 3-16 characters, alphanumeric + underscore.",
                ephemeral=True,
            )
            return

        # Check if already a citizen.
        existing = await db.execute_query(
            "SELECT ign FROM citizens WHERE discord_id = $1",
            (str(interaction.user.id),),
            fetch_one=True,
        )
        if existing:
            await interaction.response.send_message(
                "❌ You are already a registered citizen.", ephemeral=True
            )
            return

        # Check if already has a pending application.
        if await apps.has_pending_application(str(interaction.user.id)):
            await interaction.response.send_message(
                "❌ You already have a pending application. Please wait for council to review it.",
                ephemeral=True,
            )
            return

        # Validate settlement exists.
        settlement_row = await db.execute_query(
            "SELECT name, duchy FROM settlements WHERE name = $1",
            (settlement,),
            fetch_one=True,
        )
        if not settlement_row:
            await interaction.response.send_message(
                f"❌ Settlement `{settlement}` does not exist. Use `/settlement list` to see valid settlements.",
                ephemeral=True,
            )
            return

        # Check IGN not already taken.
        ign_taken = await db.execute_query(
            "SELECT ign FROM citizens WHERE ign = $1", (ign,), fetch_one=True
        )
        if ign_taken:
            await interaction.response.send_message(
                f"❌ IGN `{ign}` is already registered to another citizen. "
                "Contact a council member if this is your account.",
                ephemeral=True,
            )
            return

        # Submit.
        recruiter_id = str(recruiter.id) if recruiter else None
        app_row = await apps.submit_application(
            ign,
            str(interaction.user.id),
            settlement,
            recruiter_id,
        )
        if not app_row:
            await interaction.response.send_message(
                "❌ Could not submit application (you may already have one pending).",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="✅ Application Submitted",
            description=(
                f"Your application for citizenship has been submitted!\n\n"
                f"**IGN:** {ign}\n"
                f"**Settlement:** {settlement}\n"
                f"**Recruiter:** {recruiter.mention if recruiter else '—'}\n"
                f"**Application ID:** #{app_row['id']}\n\n"
                f"Council will review your application shortly."
            ),
            color=0x43B581,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Post to the applications channel for council review.
        await self._post_application_for_review(app_row, interaction.user, settlement_row)

    async def _post_application_for_review(
        self, app_row: dict, applicant: discord.User, settlement_row: dict
    ) -> None:
        """Post the application to APPLICATIONS_CHANNEL_ID with Approve/Reject buttons."""
        if not Config.APPLICATIONS_CHANNEL_ID:
            return

        try:
            channel = self.bot.get_channel(Config.APPLICATIONS_CHANNEL_ID)
            if channel is None:
                channel = await self.bot.fetch_channel(Config.APPLICATIONS_CHANNEL_ID)
        except Exception as e:
            logger.warning(f"Could not fetch applications channel: {e}")
            return

        if channel is None:
            return

        embed = discord.Embed(
            title=f"📋 New Application #{app_row['id']}",
            description=(
                f"**Applicant:** {applicant.mention} (`{applicant.id}`)\n"
                f"**IGN:** `{app_row['ign']}`\n"
                f"**Settlement:** {app_row['settlement']} ({settlement_row['duchy']})\n"
                f"**Recruiter:** <@{app_row['recruiter_discord_id']}>"
                if app_row["recruiter_discord_id"]
                else ""
            ),
            color=0x7289DA,
        )
        embed.timestamp = app_row["submitted_at"]
        embed.set_footer(text=f"Application ID: {app_row['id']}")

        view = ApplicationReviewView(app_row["id"], self.bot)
        msg = await channel.send(embed=embed, view=view)
        view.message = msg

    @application_group.command(name="list", description="List pending applications")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "application_list")
    )
    async def application_list(self, interaction: discord.Interaction):
        if not self.has_full_access(interaction):
            return await interaction.response.send_message(
                "❌ You need the Council role to use this command.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        pending = await apps.get_pending_applications(limit=25)
        if not pending:
            await interaction.followup.send("No pending applications.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📋 Pending Applications",
            description=f"**{len(pending)}** application(s) awaiting review.",
            color=0x7289DA,
        )
        for app in pending[:15]:
            recruiter_str = (
                f" (via <@{app['recruiter_discord_id']}>)" if app["recruiter_discord_id"] else ""
            )
            embed.add_field(
                name=f"#{app['id']} — {app['ign']}",
                value=f"<@{app['applicant_discord_id']}> → {app['settlement']}{recruiter_str}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


class ApplicationReviewView(discord.ui.View):
    """Approve/Reject buttons on the application review message (Phase 3.4)."""

    def __init__(self, app_id: int, bot):
        super().__init__(timeout=None)
        self.app_id = app_id
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only council members can approve/reject.
        if interaction.user.id == Config.OWNER_ID:
            return True
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        user_role_ids = [r.id for r in interaction.user.roles]
        if not any(role_id in Config.FULL_ACCESS_ROLE_IDS for role_id in user_role_ids):
            await interaction.response.send_message(
                "❌ Only Council members can approve/reject applications.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        app_row = await apps.get_application(self.app_id)
        if not app_row or app_row["status"] != "pending":
            await interaction.followup.send(
                "❌ Application not found or already decided.", ephemeral=True
            )
            return

        # Run the citizen_add path: insert citizen + recruiters + audit.
        try:
            await _approve_and_create_citizen(app_row, interaction.user.id, self.bot)
        except Exception as e:
            logger.error(f"Failed to approve application #{self.app_id}: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Failed to approve: {e}", ephemeral=True)
            return

        # Mark as approved.
        await apps.approve_application(self.app_id, str(interaction.user.id))

        # Update the embed.
        embed = interaction.message.embeds[0]
        embed.color = 0x43B581
        embed.title = f"✅ Approved: {embed.title}"
        embed.add_field(
            name="Decision", value=f"Approved by {interaction.user.mention}", inline=False
        )
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="✖️")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        app_row = await apps.get_application(self.app_id)
        if not app_row or app_row["status"] != "pending":
            await interaction.followup.send(
                "❌ Application not found or already decided.", ephemeral=True
            )
            return

        await apps.reject_application(self.app_id, str(interaction.user.id))

        embed = interaction.message.embeds[0]
        embed.color = 0xED4245
        embed.title = f"❌ Rejected: {embed.title}"
        embed.add_field(
            name="Decision", value=f"Rejected by {interaction.user.mention}", inline=False
        )
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(embed=embed, view=self)


async def _approve_and_create_citizen(app_row: dict, approver_id: int, bot) -> None:
    """Run the citizen_add path for an approved application (Phase 3.4).

    Inserts the citizen, writes recruiters junction rows, emits audit + governance.
    Role assignment is handled by the role_sync weekly task (or council can
    run /citizen add manually). This keeps the approve flow fast and avoids
    Discord rate limits on bulk approvals.
    """
    from datetime import UTC, datetime

    from services import recruiters as recruiters_svc

    ign = app_row["ign"]
    discord_id = app_row["applicant_discord_id"]
    settlement = app_row["settlement"]
    recruiter_id = app_row.get("recruiter_discord_id")
    recruiters_list = [recruiter_id] if recruiter_id else []

    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO citizens (ign, discord_id, settlement, recruiter_ids, join_date) "
            "VALUES ($1, $2, $3, $4, $5)",
            ign,
            discord_id,
            settlement,
            ",".join(recruiters_list) if recruiters_list else "",
            datetime.now(UTC).date(),
        )
        if recruiters_list:
            await recruiters_svc.set_recruiters(ign, recruiters_list, connection=conn)
        # Audit the add atomically.
        await audit.emit(
            audit.CITIZEN_ADD,
            approver_id,
            ign,
            {
                "discord_id": discord_id,
                "settlement": settlement,
                "recruiters": recruiters_list,
                "source": "application",
            },
            connection=conn,
        )

    # Post-commit: mirror to audit channel + governance channel (Phase 3.7).
    audit_details = {
        "discord_id": discord_id,
        "settlement": settlement,
        "recruiters": recruiters_list,
    }
    await audit.post_to_channel(bot, audit.CITIZEN_ADD, str(approver_id), ign, audit_details)
    await audit.post_to_governance_channel(
        bot, audit.CITIZEN_ADD, str(approver_id), ign, audit_details
    )


async def setup(bot):
    await bot.add_cog(ApplicationsCog(bot))
