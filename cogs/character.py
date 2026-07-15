from __future__ import annotations

import random
import time
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

_UNIT_NAMES = {
    "light_sound": "Leo/need",
    "idol": "MORE MORE JUMP!",
    "street": "Vivid BAD SQUAD",
    "theme_park": "Wonderlands×Showtime",
    "school_refusal": "Nightcord at 25:00",
    "piapro": "VIRTUAL SINGER",
    "vocaloid": "VIRTUAL SINGER",
}


def _char_name(char: Character) -> str:
    """Display name: given + family, reversed for Virtual Singers, no title-casing (so MEIKO
    stays MEIKO)."""
    if char.first_name and char.unit != "piapro":
        return f"{char.given_name} {char.first_name}"
    if char.first_name:
        return f"{char.first_name} {char.given_name}"
    return char.given_name


def _unit_name(unit: str | None) -> str:
    return _UNIT_NAMES.get(unit or "", unit or "?")


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

        card = self._random_card(char_obj.id)
        image_url = card.card_url_normal if card else None
        footer = self.bot.pjsk.card_display_name(card) if card else None  # type: ignore[union-attr]
        embed = self._info_embed(char_obj, image_url, footer)
        view = CharacterInfoView(
            self, char_obj, image_url, footer, restrict_to=interaction.user.id
        )
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()

    def _random_card(self, character_id: int) -> Card | None:
        """A random already-released 3★/4★/birthday card of the character, for the big art."""
        now = int(time.time() * 1000)
        pool = [
            c
            for c in self.bot.pjsk.cards()  # type: ignore[union-attr]
            if c.character_id == character_id
            and c.card_rarity_type in ("rarity_3", "rarity_4", "rarity_birthday")
            and c.card_url_normal
            and (c.release_at is None or c.release_at <= now)
        ]
        return random.choice(pool) if pool else None

    def _info_embed(
        self, char: Character, image_url: str | None, footer: str | None
    ) -> discord.Embed:
        embed = embeds.embed(title=_char_name(char), color=discord.Color.teal())
        header = f"**{_unit_name(char.unit)}**"
        if char.support_unit:
            header += (
                f"\n**Support Unit:** {char.support_unit.replace('_', ' ').title()}"
            )
        mid = ""
        if char.voice_actor:
            mid += f"**Voice Actor:** {char.voice_actor}\n"
        if char.birthday:
            mid += f"**Birthday:** `{char.birthday.replace('.', ' ')}`"
        body = ""
        if char.gender:
            body += f"**Gender:** {char.gender.capitalize()}\n"
        if char.height:
            body += f"**Height:** {char.height}\n-# Height as of third anniversary."
        desc = "\n\n".join(p for p in (header, mid.rstrip(), body.rstrip()) if p)
        if char.school:
            year = char.school_year if char.school_year not in (None, "-") else "N/A"
            desc += f"\n\n**School:** {char.school.replace('HS', 'High School')} (Year {year})"
        embed.description = desc
        if image_url:
            embed.set_image(url=image_url)
        if footer:
            embed.set_footer(text=footer)
        return embed

    def _profile_embed(
        self, char: Character, image_url: str | None, footer: str | None
    ) -> discord.Embed:
        embed = embeds.embed(
            title=f"{_char_name(char)} Profile", color=discord.Color.teal()
        )
        embed.description = (
            f"**{_unit_name(char.unit)}**\n\n"
            f"**Hobbies:** {char.hobby}\n"
            f"**Special Skills:** {char.special_skill}\n"
            f"**Dislikes:** {char.weak_point}\n"
            f"**Hated Food:** {char.hated_food}\n"
            f"**Favorite Food:** {char.favorite_food}\n\n"
            f"**Introduction**\n```\n{char.introduction}\n```"
        )
        if image_url:
            embed.set_image(url=image_url)
        if footer:
            embed.set_footer(text=footer)
        return embed

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


class CharacterInfoView(SbugaView):
    """Toggles the character embed between the Info page and the Profile page, reusing the
    same randomly-picked card art across both (command invoker only)."""

    def __init__(
        self,
        cog: CharactersCog,
        char: Character,
        image_url: str | None,
        footer: str | None,
        restrict_to: int,
    ) -> None:
        super().__init__(restrict_to=restrict_to)
        self.cog = cog
        self.char = char
        self.image_url = image_url
        self.footer = footer
        self.profile = False

    @discord.ui.button(label="View Profile", style=discord.ButtonStyle.primary)
    async def toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.profile = not self.profile
        button.label = "Back" if self.profile else "View Profile"
        embed = (
            self.cog._profile_embed(self.char, self.image_url, self.footer)
            if self.profile
            else self.cog._info_embed(self.char, self.image_url, self.footer)
        )
        await interaction.response.edit_message(embed=embed, view=self)


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
