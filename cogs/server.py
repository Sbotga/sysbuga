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

    leaks = app_commands.Group(
        name="leaks",
        description="Channels where leaked (unreleased) content is shown.",
        parent=server,
    )

    @server.command(name="settings", description="View this server's settings.")
    async def settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        channels = await self.bot.user_data.leak_channels(interaction.guild_id)  # type: ignore[union-attr,arg-type]
        listed = ", ".join(f"<#{c}>" for c in channels) if channels else "None"
        embed = embeds.embed(title="Server Settings", color=discord.Color.blurple())
        embed.description = f"**Leak Channels:** {listed}"
        await interaction.followup.send(embed=embed)

    @leaks.command(
        name="add", description="Allow leaked content to be shown in a channel."
    )
    @app_commands.describe(channel="The channel to allow leaks in.")
    async def leaks_add(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await interaction.response.defer()
        if not await self._needs_manage(interaction):
            return
        await self.bot.user_data.add_leak_channel(interaction.guild_id, channel.id)  # type: ignore[union-attr,arg-type]
        await interaction.followup.send(
            embed=embeds.success_embed(f"Leaks are now shown in {channel.mention}.")
        )

    @leaks.command(
        name="remove", description="Stop showing leaked content in a channel."
    )
    @app_commands.describe(channel="The channel to block leaks in again.")
    async def leaks_remove(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await interaction.response.defer()
        if not await self._needs_manage(interaction):
            return
        removed = await self.bot.user_data.remove_leak_channel(channel.id)  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=embeds.success_embed(
                f"Leaks are no longer shown in {channel.mention}."
                if removed
                else f"{channel.mention} wasn't a leak channel."
            )
        )

    @leaks.command(name="list", description="List this server's leak channels.")
    async def leaks_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        channels = await self.bot.user_data.leak_channels(interaction.guild_id)  # type: ignore[union-attr,arg-type]
        embed = embeds.embed(title="Leak Channels", color=discord.Color.blurple())
        embed.description = (
            "\n".join(f"- <#{c}>" for c in channels)
            if channels
            else "No channels show leaks. Add one with `/server leaks add`."
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(ServerCog(bot))
