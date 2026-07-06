from typing import Callable, Coroutine

import discord
from discord import app_commands

from data.pjsk import PJSKData

ALLOWED_REGIONS = ["jp", "en", "tw", "kr", "cn", "all"]

GUESSING_TYPES = {
    "Jacket": "jacket",
    "Jacket 30px": "jacket_30px",
    "Jacket Black and White": "jacket_bw",
    "Jacket Challenge": "jacket_challenge",
    "Character": "character",
    "Character Black and White": "character_bw",
    "Chart": "chart",
    "Chart Append": "chart_append",
    "Event": "event",
    "Song Note Count": "notes",
}

DIFFICULTIES = {
    "Master": "master",
    "Expert": "expert",
    "Hard": "hard",
    "Normal": "normal",
    "Easy": "easy",
    "Append": "append",
}

AutocompleteFn = Callable[
    [discord.Interaction, str], Coroutine[None, None, list[app_commands.Choice[str]]]
]


class Autocompletes:
    def __init__(self, pjsk: PJSKData | None = None) -> None:
        self.pjsk = pjsk

    def range(self, min_value: int, max_value: int | str) -> AutocompleteFn:
        assert isinstance(max_value, int) or max_value == "inf"
        top = 500 if max_value == "inf" else max_value

        async def _range(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            return [
                app_commands.Choice(name=str(i), value=str(i))
                for i in range(min_value, top + 1)
                if str(i).startswith(current)
            ][:25]

        return _range

    def pjsk_region(
        self, allowed_regions: list[str], temp_allow_cn: bool = False
    ) -> AutocompleteFn:
        invalid = [r for r in allowed_regions if r not in ALLOWED_REGIONS]
        if invalid:
            raise ValueError(f"Invalid regions provided: {', '.join(invalid)}")
        if not temp_allow_cn:
            allowed_regions = [r for r in allowed_regions if r != "cn"]

        async def _region(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            current_lower = current.lower()
            return [
                app_commands.Choice(name=r.upper(), value=r)
                for r in allowed_regions
                if current_lower in r.lower()
            ][:25]

        return _region

    def custom_values(self, values: dict[str, str]) -> AutocompleteFn:
        async def _getvalue(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            current_lower = current.lower()
            return [
                app_commands.Choice(name=name, value=value)
                for name, value in values.items()
                if current_lower in name.lower()
            ][:25]

        return _getvalue

    async def pjsk_difficulties(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current_lower = current.lower()
        return [
            app_commands.Choice(name=key, value=value)
            for key, value in DIFFICULTIES.items()
            if current_lower in key.lower() or current_lower in value.lower()
        ][:25]

    async def pjsk_guessing_types(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current_lower = current.lower()
        return [
            app_commands.Choice(name=key, value=value)
            for key, value in GUESSING_TYPES.items()
            if current_lower in key.lower() or current_lower in value.lower()
        ][:25]

    async def pjsk_song(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not self.pjsk:
            return []
        if not current.strip():
            musics = self.pjsk.musics()[:25]
        else:
            ids = self.pjsk.search_songs(current, limit=25)
            musics = [m for m in (self.pjsk.get_music(i) for i in ids) if m]
        return [
            app_commands.Choice(name=m.title[:100], value=str(m.id)) for m in musics
        ][:25]

    async def pjsk_event(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not self.pjsk:
            return []
        if not current.strip():
            events = self.pjsk.events()[:25]
        else:
            ids = self.pjsk.search_events(current, limit=25)
            events = [e for e in (self.pjsk.get_event(i) for i in ids) if e]
        return [
            app_commands.Choice(name=e.name[:100], value=str(e.id)) for e in events
        ][:25]

    async def pjsk_character(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        from data.pjsk import character_display_name

        if not self.pjsk:
            return []
        cur = current.lower().replace(" ", "")
        out: list[app_commands.Choice[str]] = []
        for char in self.pjsk.characters():
            name = character_display_name(char)
            if not cur or cur in name.lower().replace(" ", ""):
                out.append(app_commands.Choice(name=name, value=str(char.id)))
            if len(out) >= 25:
                break
        return out

    RARITY_TOKENS = {
        "1*": "1☆",
        "2*": "2☆",
        "3*": "3☆",
        "4*": "4☆",
        "birthday": "🎀",
        "bday": "🎀",
        "bd": "🎀",
    }

    async def pjsk_card(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not self.pjsk:
            return []
        if not current.strip():
            return [
                app_commands.Choice(
                    name="Type a character, rarity (1*-4*, birthday), or attribute.",
                    value="0",
                )
            ]
        parts = [self.RARITY_TOKENS.get(p, p) for p in current.lower().split()]
        out: list[app_commands.Choice[str]] = []
        for card in self.pjsk.cards():
            name = self.pjsk.card_display_name(card)
            haystack = name.lower()
            if all(p in haystack for p in parts):
                out.append(
                    app_commands.Choice(
                        name=f"({card.id}) {name}"[:100], value=str(card.id)
                    )
                )
            if len(out) >= 25:
                break
        return out or [app_commands.Choice(name="No matches", value="0")]


autocompletes = Autocompletes()
