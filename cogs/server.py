"""Server status cog — live CivMC server health via mcsrvstat.us (no auth).

CivMC relevance: nations need to know if the server is up before scheduling
events, raids, or pearl defenses. The mcsrvstat.us API is free, requires no
key, responds in ~40ms, and returns online count, max slots, version, MOTD,
and a base64 server icon.
"""

import discord
from discord import app_commands
from discord.ext import commands
from core.config import Config
import logging
import aiohttp
import io

logger = logging.getLogger(__name__)


class ServerCog(commands.Cog):
    """``/server status`` — live CivMC server health."""

    server_group = app_commands.Group(name="server", description="CivMC server status and uptime")

    def __init__(self, bot):
        self.bot = bot

    async def _fetch_status(self) -> dict:
        """Query mcsrvstat.us v3 for the configured server address.

        Returns a dict with keys: online, players_online, players_max,
        version, motd, icon (base64 data URI or None), software, hostname.
        Raises on network/parse failure so callers can show an honest error.
        """
        url = f"{Config.MCSRVSTAT_API_BASE}/{Config.SERVER_ADDRESS}"
        async with self.bot.http_session.get(url) as resp:
            if resp.status != 200:
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=resp.status, message=f"mcsrvstat.us HTTP {resp.status}"
                )
            data = await resp.json()

        players = data.get("players") or {}
        motd = data.get("motd") or {}
        motd_lines = motd.get("clean") or []
        return {
            "online": bool(data.get("online")),
            "players_online": players.get("online", 0),
            "players_max": players.get("max", 0),
            "version": data.get("version", "unknown"),
            "motd": " | ".join(motd_lines).strip() if motd_lines else "",
            "icon": data.get("icon"),  # base64 data URI, or None
            "software": data.get("software", "unknown"),
            "hostname": data.get("hostname", Config.SERVER_ADDRESS),
        }

    @server_group.command(name="status", description="Show live CivMC server status")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "server_status"))
    async def server_status(self, interaction: discord.Interaction):
        # Server status is public information (anyone can query mcsrvstat.us),
        # so this command is open to anyone who can see the bot in a guild.
        await interaction.response.defer()

        try:
            s = await self._fetch_status()
        except Exception as e:
            logger.error(f"Failed to fetch server status: {e}", exc_info=True)
            embed = discord.Embed(
                title="🖥️ CivMC Server Status",
                description="❌ Could not reach the status API. Please try again in a moment.",
                color=0xED4245
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if s["online"]:
            color = 0x3BAD4C  # CivMC green (matches the server MOTD colour)
            status_line = "🟢 **Online**"
        else:
            color = 0xED4245
            status_line = "🔴 **Offline**"

        embed = discord.Embed(
            title="🖥️ CivMC Server Status",
            color=color
        )
        embed.add_field(name="Status", value=status_line, inline=True)
        embed.add_field(
            name="Players",
            value=f"{s['players_online']} / {s['players_max']}",
            inline=True
        )
        embed.add_field(name="Version", value=s["version"], inline=True)

        if s["motd"]:
            # Discord field values can't be empty; cap MOTD to a reasonable length.
            motd = s["motd"][:250]
            embed.add_field(name="MOTD", value=f"```{motd}```", inline=False)

        embed.add_field(
            name="Address",
            value=f"`{Config.SERVER_ADDRESS}`",
            inline=True
        )
        embed.add_field(name="Software", value=s["software"], inline=True)

        # Attach the server icon as a thumbnail if present.
        file = None
        if s["icon"]:
            try:
                # mcsrvstat returns "data:image/png;base64,iVBOR..."
                header, _, b64 = s["icon"].partition(",")
                if b64:
                    import base64 as _b64
                    icon_bytes = _b64.b64decode(b64)
                    file = discord.File(
                        io.BytesIO(icon_bytes), filename="server_icon.png"
                    )
                    embed.set_thumbnail(url="attachment://server_icon.png")
            except Exception as e:
                logger.debug(f"Could not decode server icon: {e}")

        embed.set_footer(text="Data: mcsrvstat.us • Updates every few minutes")

        if file:
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)

    @server_group.command(name="ping", description="Quick server reachability check")
    @app_commands.checks.cooldown(1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "server_ping"))
    async def server_ping(self, interaction: discord.Interaction):
        """Lightweight one-line status."""
        # Defer in case the status API is slow — avoids Discord's 3-second
        # interaction timeout. The followup is still public (not ephemeral).
        await interaction.response.defer()
        try:
            s = await self._fetch_status()
        except Exception as e:
            logger.error(f"Server ping failed: {e}", exc_info=True)
            await interaction.followup.send(
                "🔴 **CivMC** — status API unreachable right now.",
                ephemeral=True
            )
            return

        if s["online"]:
            await interaction.followup.send(
                f"🟢 **CivMC** is online — {s['players_online']}/{s['players_max']} players "
                f"(v{s['version']})"
            )
        else:
            await interaction.followup.send(
                f"🔴 **CivMC** is offline right now."
            )


async def setup(bot):
    await bot.add_cog(ServerCog(bot))
