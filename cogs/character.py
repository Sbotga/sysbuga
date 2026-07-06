from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import Card, Character
from data.pjsk import PJSKData, character_display_name
from helpers import embeds
from helpers.autocompletes import autocompletes
from helpers.views import SbugaView

if TYPE_CHECKING:
    from main import SbugaBot

PROFILE_FIELDS = [
    ("unit", "Unit"),
    ("support_unit", "Support Unit"),
    ("gender", "Gender"),
    ("height", "Height"),
    ("birthday", "Birthday"),
    ("school", "School"),
    ("school_year", "School Year"),
    ("hobby", "Hobby"),
    ("special_skill", "Special Skill"),
    ("favorite_food", "Favorite Food"),
    ("hated_food", "Hated Food"),
    ("weak_point", "Weak Point"),
    ("voice_actor", "Voice Actor"),
]


class CharactersCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    char = app_commands.Group(
        name="char",
        description="PJSK character and card info.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @char.command(name="info", description="View a PJSK character's profile.")
    @app_commands.autocomplete(character=autocompletes.pjsk_character)
    @app_commands.describe(character="Character name.")
    async def info(self, interaction: discord.Interaction, character: str) -> None:
        await interaction.response.defer(thinking=True)
        char_obj = self._resolve_character(character)
        if not char_obj:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't find a character matching `{character}`."
                )
            )
            return

        embed = embeds.embed(
            title=character_display_name(char_obj), color=discord.Color.blurple()
        )
        for attr, label in PROFILE_FIELDS:
            value = getattr(char_obj, attr, None)
            if value:
                embed.add_field(name=label, value=value, inline=True)
        if char_obj.introduction:
            embed.description = char_obj.introduction
        thumb = next(
            (c.thumbnail_url_normal for c in self.bot.pjsk.cards() if c.character_id == char_obj.id and c.thumbnail_url_normal),  # type: ignore[union-attr]
            None,
        )
        if thumb:
            embed.set_thumbnail(url=thumb)
        await interaction.followup.send(embed=embed)

    @char.command(name="card", description="View a PJSK card's art.")
    @app_commands.autocomplete(card=autocompletes.pjsk_card)
    @app_commands.describe(card="Card (search by character, rarity, or attribute).")
    async def card(self, interaction: discord.Interaction, card: str) -> None:
        await interaction.response.defer(thinking=True)
        if not card.isdigit():
            await interaction.followup.send(
                embed=embeds.error_embed("Pick a card from the autocomplete.")
            )
            return
        card_obj = self.bot.pjsk.get_card(int(card))  # type: ignore[union-attr]
        if not card_obj:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find card `{card}`.")
            )
            return

        name = f"({card_obj.id}) {self.bot.pjsk.card_display_name(card_obj, use_emojis=True)}"  # type: ignore[union-attr]
        embed = embeds.embed(description=f"### {name}", color=discord.Color.blurple())
        embed.set_footer(text="Character Cards")
        view = discord.utils.MISSING
        if (
            card_obj.card_rarity_type in ("rarity_3", "rarity_4")
            and card_obj.card_url_trained
        ):
            view = CardTrainedView(self.bot.pjsk, card_obj, interaction.user.id)  # type: ignore[arg-type]
        embed.set_image(url=card_obj.card_url_normal)
        message = await interaction.followup.send(embed=embed, view=view)
        if view:
            view.message = message

    def _resolve_character(self, query: str) -> Character | None:
        if query.isdigit():
            return self.bot.pjsk.get_character(int(query))  # type: ignore[union-attr]
        q = query.lower().replace(" ", "")
        return next(
            (c for c in self.bot.pjsk.characters() if q in character_display_name(c).lower().replace(" ", "")),  # type: ignore[union-attr]
            None,
        )


class CardTrainedView(SbugaView):
    def __init__(self, pjsk: PJSKData, card: Card, restriction_id: int) -> None:
        super().__init__(restrict_to=restriction_id)
        self.pjsk = pjsk
        self.card = card
        self.trained = False

    @discord.ui.button(label="Toggle Trained", style=discord.ButtonStyle.primary)
    async def toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.trained = not self.trained
        url = self.card.card_url_trained if self.trained else self.card.card_url_normal
        embed = (
            interaction.message.embeds[0]
            if interaction.message and interaction.message.embeds
            else embeds.embed("")
        )
        name = f"({self.card.id}) {self.pjsk.card_display_name(self.card, use_emojis=True, trained=self.trained)}"
        embed.description = f"### {name}"
        embed.set_image(url=url)
        await interaction.response.edit_message(embed=embed, view=self)


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(CharactersCog(bot))
