from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds

if TYPE_CHECKING:
    from main import SbugaBot


class ActivityCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    activity = app_commands.Group(
        name="activity",
        description="Launch SYSbuga activities.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @activity.command(name="launch", description="Launch the SYSbuga activity.")
    async def launch(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.launch_activity()
        except discord.HTTPException as e:
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"Couldn't launch the activity here: {e.text or e.status}"
                ),
                ephemeral=True,
            )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(ActivityCog(bot))
