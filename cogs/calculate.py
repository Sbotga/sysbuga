from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import converters, embeds
from helpers.autocompletes import autocompletes

if TYPE_CHECKING:
    from main import SbugaBot

# /calculate is exclusive to the 39s guild (same as old Sbotga); sync it with
# `?sync 986099686005960796`.
CALCULATE_GUILD_ID = 986099686005960796


class CalculateCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    def calculate_score(
        self, constant: float, score: str, note_count: int
    ) -> dict | None:
        score_data = score.split("/")
        if len(score_data) > 5 or len(score_data) < 1:
            return None

        score_values = self.convert_to_score_values(score_data, note_count)
        if score_values is None:
            return None

        accuracy = self.calculate_accuracy(score_values)
        constant_modifier = self.get_modifier_from_accuracy(accuracy)
        if constant_modifier is None:
            return None

        return {
            "result": constant + constant_modifier,
            "diff": f"{'+' if constant_modifier > 0 else ''}{constant_modifier:.2f}",
            "accuracy": accuracy,
            "score_values": score_values,
        }

    def convert_to_score_values(
        self, score: list[str], note_count: int
    ) -> list[int] | None:
        try:
            arr = [int(s) for s in score]
        except ValueError:
            return None
        if len(arr) == 5:
            return arr
        if not arr:
            return None
        great = arr[0]
        good = arr[1] if len(arr) > 1 else 0
        bad = arr[2] if len(arr) > 2 else 0
        miss = arr[3] if len(arr) > 3 else 0
        return [note_count - great - good - bad - miss, great, good, bad, miss]

    def calculate_accuracy(self, score: list[int]) -> float:
        total = sum(score)
        if total <= 0:
            return 0.0
        _perf, great, good, bad, miss = score
        negative = great + good * 2 + bad * 3 + miss * 3
        return (total * 3 - negative) / (total * 3)

    def get_modifier_from_accuracy(self, accuracy: float) -> float | None:
        if accuracy > 1.00:
            return None
        if accuracy >= 0.99:
            return (accuracy - 0.99) * 200 + 2
        if 0.97 <= accuracy < 0.99:
            return (accuracy - 0.97) * 100
        return (accuracy - 0.97) * 200 / 3

    @staticmethod
    def _rank(diff: float) -> str:
        thresholds = [
            (-4, "Troll"),
            (-2, "Novice"),
            (0, "Bronze"),
            (1, "Silver"),
            (2, "Gold"),
            (3, "Platinum"),
            (3.5, "Diamond"),
            (4, "Gorilla"),
        ]
        for limit, label in thresholds:
            if diff < limit:
                return label
        return "Space Gorilla"

    @app_commands.command(
        name="calculate",
        description="Calculate a song's play rating from your score (always 39s constants).",
    )
    @app_commands.guilds(CALCULATE_GUILD_ID)
    @app_commands.describe(
        song="Song name or ID.",
        difficulty="Chart difficulty.",
        score="PERFECT/GREAT/GOOD/BAD/MISS, or GREAT/GOOD/BAD/MISS.",
    )
    @app_commands.autocomplete(
        song=autocompletes.pjsk_song,
        difficulty=autocompletes.pjsk_difficulties,
    )
    async def calculate(
        self, interaction: discord.Interaction, song: str, difficulty: str, score: str
    ) -> None:
        await interaction.response.defer(thinking=True)
        assert self.bot.pjsk and self.bot.constants

        music = converters.match_song(self.bot.pjsk, song)
        if music is None:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
            return

        diff = converters.match_difficulty(difficulty)
        if not diff:
            await interaction.followup.send(
                embed=embeds.error_embed(f"`{difficulty}` isn't a valid difficulty.")
            )
            return

        note_count = next(
            (d.total_note_count for d in music.difficulties if d.difficulty == diff), 0
        )
        if not note_count:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"**{music.title}** has no {diff.upper()} chart."
                )
            )
            return

        constant = await self.bot.constants.get(music.id, diff, ap=True, force_39s=True)
        if not isinstance(constant, (int, float)):
            await interaction.followup.send(
                embed=embeds.error_embed("That chart isn't rated yet.")
            )
            return

        result = self.calculate_score(float(constant), score, note_count)
        if result is None:
            await interaction.followup.send(
                embed=embeds.error_embed("Invalid score input.")
            )
            return

        diff_value = float(result["diff"])
        rank = self._rank(diff_value)
        color = (
            discord.Color.green()
            if diff_value > 2
            else discord.Color.yellow() if diff_value > -2 else discord.Color.red()
        )
        await interaction.followup.send(
            embed=embeds.embed(
                description=(
                    f"**{music.title}** [{diff.upper()}]\n\n"
                    f"**Result:** {result['result']:.2f} (`{constant} {result['diff']}`) (*{rank}*)\n"
                    f"-# Always uses 39s constants.\n"
                    f"**Accuracy:** {result['accuracy']:.2%}\n"
                    f"**Score:** {'/'.join(str(v) for v in result['score_values'])}"
                ),
                color=color,
            )
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(CalculateCog(bot))
