from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds, tools
from helpers.autocompletes import autocompletes
from helpers.views import LinkButtonView, SbugaView
from services.models import CurrentRankedResponse
from services.sbuga import SbugaError

if TYPE_CHECKING:
    from main import SbugaBot

RANKED_REGIONS = ["en", "jp", "tw", "kr"]

GRADES_EN = {
    1: "Beginner",
    2: "Bronze",
    3: "Silver",
    4: "Gold",
    5: "Platinum",
    6: "Diamond",
    7: "Master",
}

GRADE_IMAGES = {
    0: "data/assets/ranked/unknown.png",
    1: "data/assets/ranked/beginner.png",
    2: "data/assets/ranked/bronze.png",
    3: "data/assets/ranked/silver.png",
    4: "data/assets/ranked/gold.png",
    5: "data/assets/ranked/platinum.png",
    6: "data/assets/ranked/diamond.png",
    7: "data/assets/ranked/master.png",
}


def _grade_of(ranking: dict) -> tuple[str, int, int]:
    tier_id = ranking["userRankMatchSeason"]["rankMatchTierId"]
    tier_point = ranking["userRankMatchSeason"]["tierPoint"]
    grade = min(int((tier_id - 1) / 4) + 1, 7)
    kurasu = tier_id - 4 * (grade - 1) or 4
    name = GRADES_EN[grade]
    if grade == 7:
        return f"**{name}**\n♪ × {tier_point}", grade, kurasu
    return f"**{name}** Class {kurasu}\n{tier_point}/5", grade, kurasu


class RankedCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    async def _resolve_region(
        self, interaction: discord.Interaction, region: str
    ) -> str | None:
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(  # type: ignore[union-attr]
                interaction.user.id, "default_region"
            )
        if region not in RANKED_REGIONS:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Ranked isn't supported for region `{region.upper()}`."
                )
            )
            return None
        return region

    def _rank_embed(
        self, data: CurrentRankedResponse, rank: int, region: str, is_self: bool
    ) -> tuple[discord.Embed, discord.File, LinkButtonView]:
        rankings = (data.top_100 or {}).get("rankings", [])
        ranking = rankings[rank - 1]
        season = ranking["userRankMatchSeason"]
        grade_details, grade, _ = _grade_of(ranking)
        cheating = str(ranking["userId"]) in data.cheaters

        name = ranking["name"]
        if len(name) >= 50:
            name = name[:43] + "... ⚠️"
        embed = embeds.embed(
            title=f"Rank #{ranking['rank']} - {tools.escape_md(name)}",
            color=discord.Color.purple() if grade == 7 else discord.Color.dark_blue(),
        )
        gd0, gd1 = grade_details.split("\n")
        desc = ""
        if is_self:
            desc += "✅ This is you!\n"
        if cheating:
            desc += "\n```diff\n- 🚩 THIS USER IS A CONFIRMED CHEATER 🚩\n```\n"
        plays = season["playCount"]
        non_draw = plays - season["drawCount"]
        win_rate = (season["winCount"] / non_draw * 100) if non_draw else 0.0
        desc += (
            f"## {gd0} (`{gd1}`)\n\n"
            f"**Total Games:** `{plays}`\n"
            f"**Win Rate:** `{win_rate:.2f}`%\n-# Win rate does not include draws.\n\n"
            f"**Current Winstreak:** `{season['consecutiveWinCount']}`\n"
            f"**Max Winstreak:** `{season['maxConsecutiveWinCount']}`\n\n"
            f"### Web View: <https://sbuga.com/{region}/ranked/{ranking['userId']}>"
        )
        embed.description = desc
        file = discord.File(GRADE_IMAGES[grade], filename="image.png")
        embed.set_thumbnail(url="attachment://image.png")
        embed.add_field(name="Wins", value=season["winCount"], inline=True)
        embed.add_field(name="Losses", value=season["loseCount"], inline=True)
        embed.add_field(name="Draws", value=season["drawCount"], inline=True)
        embed.set_footer(
            text=f"Ranked - {region.upper()} - updated {round(time.time() - data.updated)}s ago"
        )
        view = LinkButtonView(
            [("Web View", f"https://sbuga.com/{region}/ranked/{ranking['userId']}")]
        )
        return embed, file, view

    @staticmethod
    def _leaderboard_embed(
        data: CurrentRankedResponse,
        page: int,
        region: str,
        pjsk_id: int | None,
        per_page: int = 25,
    ) -> discord.Embed:
        rankings = (data.top_100 or {}).get("rankings", [])
        cheaters = data.cheaters
        start = (page - 1) * per_page
        embed = embeds.embed(
            title=f"Ranked {data.season_name or 'Season'} Leaderboard - Page {page}",
            color=discord.Color.purple(),
        )
        lines = []
        for ranking in rankings[start : start + per_page]:
            gd0, _, _ = _grade_of(ranking)
            head = gd0.split("\n")[0].replace("**", "")
            you = "✅ " if ranking["userId"] == pjsk_id else ""
            name = tools.escape_md(ranking["name"].replace("\n", " "))
            line = f"{you}**#{ranking['rank']} - {name}** - {head}"
            if str(ranking["userId"]) in cheaters:
                line = f"```diff\n- CHEATER 🚩 {line.replace('**', '')}\n```"
            lines.append(line)
        lines.append(f"\n### Web View: <https://sbuga.com/{region}/ranked>")
        embed.description = "\n".join(lines).strip()
        f_rank = ""
        if pjsk_id:
            for ranking in rankings:
                if ranking["userId"] == pjsk_id:
                    f_rank = f" - You are #{ranking['rank']}"
                    break
        embed.set_footer(
            text=f"Ranked Leaderboard - {region.upper()} - updated {round(time.time() - data.updated)}s ago{f_rank}"
        )
        return embed

    ranked = app_commands.Group(
        name="ranked",
        description="PJSK rank match leaderboards.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @ranked.command(
        name="view",
        description="View a player's rank match stats by leaderboard position.",
    )
    @app_commands.describe(
        rank="Leaderboard position (1-100). Omit to find yourself.",
        region="Game server region.",
    )
    @app_commands.autocomplete(
        rank=autocompletes.range(1, 100),
        region=autocompletes.pjsk_region(RANKED_REGIONS),
    )
    async def view(
        self,
        interaction: discord.Interaction,
        rank: int | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return
        region = resolved

        try:
            data = await self.bot.sbuga.get_current_ranked(region)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't fetch ranked: {e.detail or e.status}"
                )
            )
            return
        rankings = (data.top_100 or {}).get("rankings", [])
        if data.season_id is None or not rankings:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active ranked season right now.")
            )
            return

        pjsk_id = await self.bot.user_data.get_pjsk_id(interaction.user.id, region)  # type: ignore[union-attr]
        is_self = False
        if rank is None:
            found = (
                next((r["rank"] for r in rankings if r["userId"] == pjsk_id), None)
                if pjsk_id
                else None
            )
            if found is None:
                msg = (
                    "You aren't on the leaderboards."
                    if pjsk_id
                    else "Specify a rank, or link a PJSK account to find yourself."
                )
                await interaction.followup.send(
                    embed=embeds.error_embed(msg, title="Not Found")
                )
                return
            rank, is_self = found, True
        elif not 1 <= rank <= len(rankings):
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "That rank couldn't be fetched.", title="Invalid Rank"
                )
            )
            return
        elif pjsk_id and rankings[rank - 1]["userId"] == pjsk_id:
            is_self = True

        assert rank is not None
        embed, file, link_view = self._rank_embed(data, rank, region, is_self)
        await interaction.followup.send(embed=embed, file=file, view=link_view)

    @ranked.command(name="leaderboard", description="View the rank match top 100.")
    @app_commands.describe(region="Game server region.")
    @app_commands.autocomplete(region=autocompletes.pjsk_region(RANKED_REGIONS))
    async def leaderboard(
        self, interaction: discord.Interaction, region: str = "default"
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return
        region = resolved

        try:
            data = await self.bot.sbuga.get_current_ranked(region)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't fetch ranked: {e.detail or e.status}"
                )
            )
            return
        rankings = (data.top_100 or {}).get("rankings", [])
        if data.season_id is None or not rankings:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active ranked season right now.")
            )
            return

        pjsk_id = await self.bot.user_data.get_pjsk_id(interaction.user.id, region)  # type: ignore[union-attr]
        total_pages = max(1, math.ceil(len(rankings) / 25))
        embed = self._leaderboard_embed(data, 1, region, pjsk_id)
        view = LeaderboardView(
            self, data, region, pjsk_id, total_pages, interaction.user.id
        )
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()


class LeaderboardView(SbugaView):
    def __init__(
        self,
        cog: RankedCog,
        data: CurrentRankedResponse,
        region: str,
        pjsk_id: int | None,
        total_pages: int,
        restriction_id: int,
    ) -> None:
        super().__init__(restrict_to=restriction_id)
        self.cog = cog
        self.data = data
        self.region = region
        self.pjsk_id = pjsk_id
        self.total_pages = total_pages
        self.current_page = 1
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.previous_page.disabled = self.current_page == 1
        self.next_page.disabled = self.current_page == self.total_pages

    async def _show_page(self, interaction: discord.Interaction) -> None:
        self._update_buttons()
        embed = self.cog._leaderboard_embed(
            self.data, self.current_page, self.region, self.pjsk_id
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page > 1:
            self.current_page -= 1
        await self._show_page(interaction)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page < self.total_pages:
            self.current_page += 1
        await self._show_page(interaction)


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(RankedCog(bot))
