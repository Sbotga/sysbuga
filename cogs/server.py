from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds

if TYPE_CHECKING:
    from main import SbugaBot


class ServerCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    server = app_commands.Group(
        name="server",
        description="Server settings.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    async def _needs_manage(self, interaction: discord.Interaction) -> bool:
        if (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        ):
            return True
        await interaction.followup.send(
            embed=embeds.error_embed("You need the `Manage Server` permission.")
        )
        return False

    @server.command(name="settings", description="View this server's settings.")
    async def settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        allow_leaks = await self.bot.user_data.allow_leaks(interaction.guild_id)  # type: ignore[union-attr,arg-type]
        embed = embeds.embed(title="Server Settings", color=discord.Color.blurple())
        embed.description = f"**Allow Leaks:** {'ON' if allow_leaks else 'OFF'}"
        await interaction.followup.send(embed=embed)

    @server.command(
        name="allow_leaks",
        description="Allow unreleased (leaked) content, shown behind spoilers.",
    )
    @app_commands.describe(on="Whether leaks are allowed in this server.")
    async def allow_leaks(self, interaction: discord.Interaction, on: bool) -> None:
        await interaction.response.defer()
        if not await self._needs_manage(interaction):
            return
        state = await self.bot.user_data.set_allow_leaks(interaction.guild_id, on)  # type: ignore[union-attr,arg-type]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Leaks are now **{'ALLOWED (spoilered)' if state else 'BLOCKED'}** "
                "in this server."
            )
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(ServerCog(bot))
