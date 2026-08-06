"""Server status cog — live CivMC server health via mcsrvstat.us (no auth).

CivMC relevance: nations need to know if the server is up before scheduling
events, raids, or pearl defenses. The mcsrvstat.us API is free, requires no
key, responds in ~40ms, and returns online count, max slots, version, MOTD,
and a base64 server icon.
"""

import io
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import database as db
from core.config import Config

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
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=f"mcsrvstat.us HTTP {resp.status}",
                )
            data = await resp.json()

        players = data.get("players") or {}
        motd = data.get("motd") or {}
        motd_lines = motd.get("clean") or []
        return {
            "online": bool(data.get("online")),
            "players_online": players.get("online", 0),
            "players_max": players.get("max", 0),
            "players_list": players.get("list") or [],  # Phase 3.8: list of online IGNs
            "version": data.get("version", "unknown"),
            "motd": " | ".join(motd_lines).strip() if motd_lines else "",
            "icon": data.get("icon"),  # base64 data URI, or None
            "software": data.get("software", "unknown"),
            "hostname": data.get("hostname", Config.SERVER_ADDRESS),
        }

    @server_group.command(name="status", description="Show live CivMC server status")
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "server_status")
    )
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
                color=0xED4245,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if s["online"]:
            color = 0x3BAD4C  # CivMC green (matches the server MOTD colour)
            status_line = "🟢 **Online**"
        else:
            color = 0xED4245
            status_line = "🔴 **Offline**"

        embed = discord.Embed(title="🖥️ CivMC Server Status", color=color)
        embed.add_field(name="Status", value=status_line, inline=True)
        embed.add_field(
            name="Players", value=f"{s['players_online']} / {s['players_max']}", inline=True
        )
        embed.add_field(name="Version", value=s["version"], inline=True)

        if s["motd"]:
            # Discord field values can't be empty; cap MOTD to a reasonable length.
            motd = s["motd"][:250]
            embed.add_field(name="MOTD", value=f"```{motd}```", inline=False)

        embed.add_field(name="Address", value=f"`{Config.SERVER_ADDRESS}`", inline=True)
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
                    file = discord.File(io.BytesIO(icon_bytes), filename="server_icon.png")
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
                "🔴 **CivMC** — status API unreachable right now.", ephemeral=True
            )
            return

        if s["online"]:
            await interaction.followup.send(
                f"🟢 **CivMC** is online — {s['players_online']}/{s['players_max']} players "
                f"(v{s['version']})"
            )
        else:
            await interaction.followup.send("🔴 **CivMC** is offline right now.")

    @server_group.command(
        name="online", description="Show which Lambat citizens are online on CivMC right now"
    )
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "server_online")
    )
    async def server_online(self, interaction: discord.Interaction):
        """Phase 3.8: cross-reference online CivMC players with the registry."""
        await interaction.response.defer()

        try:
            s = await self._fetch_status()
        except Exception as e:
            logger.error(f"Server online check failed: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ Could not reach the status API. Please try again in a moment.",
                ephemeral=True,
            )
            return

        if not s["online"]:
            await interaction.followup.send("🔴 **CivMC** is offline right now.")
            return

        online_list = s["players_list"]
        if not online_list:
            await interaction.followup.send(
                f"🟢 CivMC is online ({s['players_online']}/{s['players_max']}) but no players are currently online."
            )
            return

        # Cross-reference: which online players are registered Lambat citizens?
        # CITEXT means the ILIKE match is case-insensitive at the DB level.
        online_lower = [p.lower() for p in online_list if p]
        if not online_lower:
            await interaction.followup.send(
                f"🟢 CivMC is online ({s['players_online']}/{s['players_max']})."
            )
            return

        # Build a parameterized IN-clause ($1, $2, ...) for the registry lookup.
        placeholders = ", ".join(f"${i + 1}" for i in range(len(online_lower)))
        citizen_rows = await db.execute_query(
            f"SELECT ign, settlement FROM citizens WHERE LOWER(ign) IN ({placeholders})",
            tuple(online_lower),
            fetch_all=True,
        )

        citizens_online, non_citizens_online = _partition_citizens(online_list, citizen_rows)

        embed = _build_online_embed(
            s["players_online"],
            s["players_max"],
            citizens_online,
            non_citizens_online,
        )
        await interaction.followup.send(embed=embed)

    @server_group.command(
        name="trends", description="Show CivMC player-count trends (last 24h / hour / minute)"
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="Last 24 hours", value="day"),
            app_commands.Choice(name="Last hour", value="hour"),
            app_commands.Choice(name="Last minute", value="minute"),
        ]
    )
    @app_commands.checks.cooldown(
        1, Config.COOLDOWN_FAST, key=lambda i: (i.user.id, "server_trends")
    )
    async def server_trends(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ):
        """Phase B (WS-5): historical player-count sparkline via CivInfo mc-server-status.

        Different from /server status (live, via mcsrvstat.us) — this shows
        trends over time, so leadership can see when CivMC is busy.
        """
        await interaction.response.defer()

        # Default to "day" (last 24h) if no period selected.
        period_val = period.value if period else "day"
        period_label = period.name if period else "Last 24 hours"

        # Fetch historical data from CivInfo (shared auth with activity API).
        from api import civinfo_api

        if civinfo_api.is_auth_broken():
            embed = discord.Embed(
                title="📈 CivMC Player Count Trends",
                description=(
                    "⚠️ **Activity data unavailable**\n"
                    "CivInfo API auth required — contact an admin.\n"
                    "_(Set `CIVINFO_API_KEY` to enable trends.)_"
                ),
                color=0xED4245,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        history = await civinfo_api.get_server_status_history(period_val, self.bot.http_session)

        if not history:
            embed = discord.Embed(
                title="📈 CivMC Player Count Trends",
                description=(
                    "⚠️ No trend data available right now.\n"
                    "This could mean CivInfo is temporarily unreachable, or the "
                    "CivMC server hasn't had any recorded activity in this period."
                ),
                color=0xFAA61A,
            )
            await interaction.followup.send(embed=embed)
            return

        # Render the chart in an executor (matplotlib is blocking).
        import asyncio

        from services import charts

        title = f"CivMC Player Count — {period_label}"
        png_bytes = await asyncio.get_event_loop().run_in_executor(
            None, charts.render_server_trends, title, history, period_val
        )

        if not png_bytes:
            embed = discord.Embed(
                title="📈 CivMC Player Count Trends",
                description="⚠️ Not enough data to render a chart.",
                color=0xFAA61A,
            )
            await interaction.followup.send(embed=embed)
            return

        # Compute summary stats for the embed.
        counts = [c for _, c in history]
        peak = max(counts)
        low = min(counts)
        avg = sum(counts) / len(counts)

        embed = discord.Embed(
            title="📈 CivMC Player Count Trends",
            description=f"**{period_label}** — {len(history)} data points",
            color=0x3BAD4C,
        )
        embed.add_field(name="Peak", value=f"👥 {peak}", inline=True)
        embed.add_field(name="Low", value=f"👤 {low}", inline=True)
        embed.add_field(name="Average", value=f"📊 {avg:.1f}", inline=True)
        embed.set_footer(text="Data: api.civinfo.net • Cached 60s")

        file = discord.File(io.BytesIO(png_bytes), filename="server_trends.png")
        embed.set_image(url="attachment://server_trends.png")
        await interaction.followup.send(embed=embed, file=file)


# ---------------------------------------------------------------------------
# Pure helpers (testable without Discord / DB)
# ---------------------------------------------------------------------------


def _partition_citizens(
    online_list: list[str], citizen_rows: list[dict] | None
) -> tuple[list[tuple[str, str]], list[str]]:
    """Split the online player list into (citizen, settlement) pairs and non-citizens.

    Case-insensitive match: mcsrvstat returns exact-case IGNs, the registry
    uses CITEXT so "SteveB" and "steveb" are the same citizen.
    """
    citizen_map: dict[str, str] = {}
    if citizen_rows:
        for row in citizen_rows:
            citizen_map[row["ign"].lower()] = row["settlement"]

    citizens: list[tuple[str, str]] = []
    non_citizens: list[str] = []
    for player in online_list:
        if not player:
            continue
        settlement = citizen_map.get(player.lower())
        if settlement is not None:
            citizens.append((player, settlement))
        else:
            non_citizens.append(player)

    # Sort citizens alphabetically by IGN for stable display.
    citizens.sort(key=lambda c: c[0].lower())
    non_citizens.sort(key=str.lower)
    return citizens, non_citizens


def _build_online_embed(
    players_online: int,
    players_max: int,
    citizens_online: list[tuple[str, str]],
    non_citizens_online: list[str],
) -> discord.Embed:
    """Build the embed for /server online."""
    total_online = players_online
    lambat_count = len(citizens_online)
    other_count = len(non_citizens_online)

    embed = discord.Embed(
        title="🟢 CivMC Online Players",
        description=f"**{total_online}/{players_max}** players online — **{lambat_count}** Lambat citizen(s).",
        color=0x3BAD4C,
    )

    if citizens_online:
        lines = [f"• **{ign}** — {settlement}" for ign, settlement in citizens_online[:25]]
        value = "\n".join(lines)
        if len(citizens_online) > 25:
            value += f"\n*...and {len(citizens_online) - 25} more*"
        embed.add_field(
            name=f"🇵🇭 Lambat Citizens ({lambat_count})",
            value=value[:1024],
            inline=False,
        )

    if non_citizens_online:
        # Show non-citizens in chunks of 25 (Discord field value cap is 1024 chars).
        lines = [f"• {ign}" for ign in non_citizens_online[:25]]
        value = "\n".join(lines)
        if len(non_citizens_online) > 25:
            value += f"\n*...and {len(non_citizens_online) - 25} more*"
        embed.add_field(
            name=f"🌐 Other Players ({other_count})",
            value=value[:1024],
            inline=False,
        )

    embed.set_footer(text="Data: mcsrvstat.us • Cross-referenced with Lambat registry")
    return embed


async def setup(bot):
    await bot.add_cog(ServerCog(bot))
