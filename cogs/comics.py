from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.autocompletes import autocompletes
from helpers.views import SbugaView
from services.models import Comic
from services.sbuga import SbugaError

if TYPE_CHECKING:
    from main import SbugaBot

COMIC_REGIONS = ["en", "jp"]
PAGE_SIZE = 23


class ComicsCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    @app_commands.command(name="comics", description="Browse PJSK one-frame comics.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(region="Game server region (en/jp).")
    @app_commands.autocomplete(region=autocompletes.pjsk_region(COMIC_REGIONS))
    async def comics(
        self, interaction: discord.Interaction, region: str = "default"
    ) -> None:
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(  # type: ignore[union-attr]
                interaction.user.id, "default_region"
            )
        if region not in COMIC_REGIONS:
            region = "en"

        await interaction.response.defer(thinking=True)
        try:
            comics = await self.bot.sbuga.get_comics(region)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't fetch comics: {e.detail or e.status}"
                )
            )
            return
        if not comics:
            await interaction.followup.send(
                embed=embeds.error_embed("No comics found.")
            )
            return

        view = ComicView(comics, region, interaction.user.id)
        embed = embeds.embed(
            title="Choose a Comic", description="Choose a comic to display!"
        )
        embed.set_footer(text=f"{region.upper()} Comics")
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()


class ComicView(SbugaView):
    def __init__(self, comics: list[Comic], region: str, restriction_id: int) -> None:
        super().__init__(restrict_to=restriction_id)
        self.comics = comics
        self.region = region
        self.current_page = 0
        self.total_pages = (len(comics) + PAGE_SIZE - 1) // PAGE_SIZE
        self._rebuild()

    def _rebuild(self) -> None:
        start = self.current_page * PAGE_SIZE
        page = self.comics[start : start + PAGE_SIZE]
        options = [
            discord.SelectOption(label=c.title[:100], value=str(start + i))
            for i, c in enumerate(page)
        ]
        if self.current_page > 0:
            options.insert(
                0, discord.SelectOption(label="⬅ Previous page", value="previous")
            )
        if self.current_page < self.total_pages - 1:
            options.append(discord.SelectOption(label="➡ Next page", value="next"))

        self.clear_items()
        self.add_item(ComicSelect(options, self))


class ComicSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], parent: ComicView) -> None:
        super().__init__(placeholder="Select a comic.", options=options)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "previous":
            self.parent_view.current_page -= 1
            self.parent_view._rebuild()
            await interaction.response.edit_message(view=self.parent_view)
            return
        if value == "next":
            self.parent_view.current_page += 1
            self.parent_view._rebuild()
            await interaction.response.edit_message(view=self.parent_view)
            return

        comic = self.parent_view.comics[int(value)]
        embed = embeds.embed(title=comic.title, color=discord.Color.blurple())
        embed.set_image(url=comic.image_url)
        embed.set_footer(text=f"{self.parent_view.region.upper()} Comics")
        await interaction.response.edit_message(embed=embed, attachments=[])


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(ComicsCog(bot))
