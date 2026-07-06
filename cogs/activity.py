from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from discord.interactions import InteractionCallbackActivityInstance

from helpers import embeds
from helpers.autocompletes import autocompletes
from webserver.activity import MODES, stage_launch

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

    @activity.command(name="guess", description="Launch the guessing activity.")
    @app_commands.autocomplete(mode=autocompletes.pjsk_guessing_types)
    @app_commands.describe(mode="Guess mode to start in (omit to pick in-app).")
    async def guess(
        self, interaction: discord.Interaction, mode: str | None = None
    ) -> None:
        if mode is not None and mode not in MODES:
            await interaction.response.send_message(
                embed=embeds.error_embed("Pick a mode from the autocomplete."),
                ephemeral=True,
            )
            return
        try:
            resp = await interaction.response.launch_activity()
        except discord.HTTPException as e:
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    f"Couldn't launch the activity here: {e.text or e.status}"
                ),
                ephemeral=True,
            )
            return
        if isinstance(resp.resource, InteractionCallbackActivityInstance):
            stage_launch(resp.resource.id, mode)


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(ActivityCog(bot))
