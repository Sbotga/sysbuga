from __future__ import annotations

import math
from io import BytesIO
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from data.models import Music
from helpers import converters, embeds, tools
from helpers.autocompletes import autocompletes
from helpers.emojis import emojis
from helpers.views import LinkButtonView, SbugaView
from services.sbuga import SbugaError, SbugaNotFound

if TYPE_CHECKING:
    from main import SbugaBot

CHART_REGIONS = ["en", "jp"]
DIFFICULTY_ORDER = ["append", "master", "expert", "hard", "normal", "easy"]
STRATEGY_META = (
    "https://raw.githubusercontent.com/Sbotga/strategies/refs/heads/main/meta.json"
)


class SongInfo(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    song = app_commands.Group(
        name="song",
        description="Commands related to PJSK songs.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    async def _resolve_song(
        self, interaction: discord.Interaction, song: str
    ) -> Music | None:
        music = converters.match_song(self.bot.pjsk, song)  # type: ignore[arg-type]
        if music is None:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find a song matching `{song}`.")
            )
        return music

    async def _default_difficulty(self, user_id: int, difficulty: str) -> str:
        if difficulty == "default":
            return await self.bot.user_data.get_settings(user_id, "default_difficulty")  # type: ignore[union-attr]
        return difficulty

    async def _chart_region(self, music: Music, user_id: int) -> list[str]:
        default = await self.bot.user_data.get_settings(user_id, "default_region")  # type: ignore[union-attr]
        have = self.bot.pjsk.regions_for_music(music.id)  # type: ignore[union-attr]
        order = [default, "en", "jp"]
        return [
            r
            for r in dict.fromkeys(order)
            if r in CHART_REGIONS and (not have or r in have)
        ] or ["en"]

    @song.command(name="jacket", description="View a song's jacket.")
    @app_commands.autocomplete(song=autocompletes.pjsk_song)
    @app_commands.describe(song="Song name or ID.")
    async def jacket(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        music = await self._resolve_song(interaction, song)
        if not music:
            return
        embed = embeds.embed(title=music.title)
        embed.set_image(url=music.jacket_url)
        await interaction.followup.send(embed=embed)

    @song.command(name="constant", description="View a song's 39s constant.")
    @app_commands.autocomplete(
        song=autocompletes.pjsk_song,
        difficulty=autocompletes.custom_values(
            {"Expert": "expert", "Master": "master", "Append": "append"}
        ),
    )
    @app_commands.describe(
        song="Song name or ID.", difficulty="Expert, Master, or Append."
    )
    async def constant(
        self, interaction: discord.Interaction, song: str, difficulty: str = "default"
    ) -> None:
        await interaction.response.defer(thinking=True)
        music = await self._resolve_song(interaction, song)
        if not music:
            return
        diff = converters.match_difficulty(
            await self._default_difficulty(interaction.user.id, difficulty)
        )
        if diff not in ("expert", "master", "append"):
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "Only Expert, Master, and Append charts have constants."
                )
            )
            return
        assert self.bot.constants
        embed = embeds.embed(title=music.title)
        embed.set_thumbnail(url=music.jacket_url)
        try:
            constant, source = await self.bot.constants.get(
                music.id, diff, True, error_on_not_found=True, include_source=True
            )  # type: ignore[misc]
        except IndexError:
            embed.description = (
                f"**{emojis.difficulty_colors[diff]} {diff.title()}** isn't rated yet "
                "(or doesn't exist for this song)."
            )
            embed.color = discord.Color.red()
            await interaction.followup.send(embed=embed)
            return
        level = self.bot.pjsk.get_play_level(music.id, diff)  # type: ignore[union-attr]
        constant_str = f"{math.ceil(float(constant) * 10) / 10:.1f}"
        embed.description = (
            f"**Difficulty:** {emojis.difficulty_colors[diff]} {diff.title()}\n\n"
            f"**Level:** `{level}`\n**Constant:** `{constant_str}`\n**Source:** `{source}`\n\n"
            "-# Constants are opinionated and will differ per person."
        )
        await interaction.followup.send(embed=embed)

    @song.command(name="chart", description="View a song's chart.")
    @app_commands.autocomplete(
        song=autocompletes.pjsk_song, difficulty=autocompletes.pjsk_difficulties
    )
    @app_commands.describe(
        song="Song name or ID.",
        difficulty="Chart difficulty.",
        mirror="Show the mirrored chart (defaults to your setting).",
    )
    async def chart(
        self,
        interaction: discord.Interaction,
        song: str,
        difficulty: str = "default",
        mirror: bool | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        music = await self._resolve_song(interaction, song)
        if not music:
            return
        if mirror is None:
            mirror = await self.bot.user_data.get_settings(interaction.user.id, "mirror_charts_by_default")  # type: ignore[union-attr]
        diff = converters.match_difficulty(
            await self._default_difficulty(interaction.user.id, difficulty)
        )
        if not diff:
            await interaction.followup.send(
                embed=embeds.error_embed(f"`{difficulty}` isn't a valid difficulty.")
            )
            return

        embed = embeds.embed(title=music.title)
        chart_bytes = None
        used_region = None
        for region in await self._chart_region(music, interaction.user.id):
            try:
                chart_bytes = await self.bot.sbuga.get_chart_image(music.id, diff, region, mirrored=bool(mirror))  # type: ignore[union-attr,arg-type]
                used_region = region
                break
            except SbugaNotFound:
                continue
            except SbugaError as e:
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        f"Couldn't render the chart: {e.detail or e.status}"
                    )
                )
                return
        if not chart_bytes:
            embed.description = f"**{emojis.difficulty_colors[diff]} {diff.title()}** doesn't exist for this song."
            embed.color = discord.Color.red()
            await interaction.followup.send(embed=embed)
            return

        embed.set_image(url="attachment://chart.png")
        embed.description = (
            f"**Difficulty:** {emojis.difficulty_colors[diff]} {diff.title()}"
        )
        if mirror:
            embed.description += "\n\n**MIRRORED CHART**"
        url = (
            f"{self.bot.sbuga.base}/api/tools/chart_viewer"  # type: ignore[union-attr]
            f"?music_id={music.id}&difficulty={diff}&region={used_region}&mirrored={str(bool(mirror)).lower()}"
        )
        view = LinkButtonView([("Open Chart", url)])
        await interaction.followup.send(
            embed=embed, file=discord.File(BytesIO(chart_bytes), "chart.png"), view=view
        )
        view.message = await interaction.original_response()

    @song.command(name="info", description="View a song's data.")
    @app_commands.autocomplete(song=autocompletes.pjsk_song)
    @app_commands.describe(song="Song name or ID.")
    async def info(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        music = await self._resolve_song(interaction, song)
        if not music:
            return

        by = ", ".join(
            sorted(
                {
                    n.strip()
                    for n in (music.composer, music.arranger, music.lyricist)
                    if n and n != "-"
                }
            )
        )
        regions = self.bot.pjsk.regions_for_music(music.id)  # type: ignore[union-attr]
        diff_map = {d.difficulty: d for d in music.difficulties}
        diff_lines = []
        for d in ["easy", "normal", "hard", "expert", "master", "append"]:
            if d in diff_map:
                entry = diff_map[d]
                diff_lines.append(
                    f"**{emojis.difficulty_colors[d]} {d.title()}:** Lvl {entry.play_level} "
                    f"`({entry.total_note_count} notes)`"
                )

        lines = [", ".join(music.categories) if music.categories else ""]
        lines.append(
            f"**Server Availability:** `{', '.join(r.upper() for r in regions) or 'None'}`"
        )
        lines.append(f"**ID:** `{music.id}`")
        if by:
            lines.append(f"**By:** {by}")
        if music.artist:
            lines.append(f"**Artist:** {music.artist.name}")
        if music.released_at:
            lines.append(f"**Released:** <t:{int(music.released_at / 1000)}:D>")
        if music.original_video:
            lines.append(f"**Original Song:** <{music.original_video}>")
        lines.append("")
        lines.extend(diff_lines)

        embed = embeds.embed(
            title=music.title, description="\n".join(filter(None, lines)).strip()
        )
        embed.set_thumbnail(url=music.jacket_url)
        await interaction.followup.send(embed=embed)

    @song.command(name="aliases", description="View a song's aliases.")
    @app_commands.autocomplete(song=autocompletes.pjsk_song)
    @app_commands.describe(song="Song name or ID.")
    async def aliases(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        music = await self._resolve_song(interaction, song)
        if not music:
            return
        embed = embeds.embed(
            title="Aliases",
            description=(
                f"Aliases for `{music.title}` (ID `{music.id}`)\n"
                f"Aliases: `{', '.join(music.title_variants) or 'None'}`"
            ),
        )
        await interaction.followup.send(embed=embed)

    @song.command(name="difficulty", description="Find all songs of a level.")
    @app_commands.describe(level="Level to search (1-39).")
    async def difficulty(self, interaction: discord.Interaction, level: int) -> None:
        await interaction.response.defer(thinking=True)
        if not 0 < level < 40:
            await interaction.followup.send(
                embed=embeds.error_embed("Level must be between 1 and 39.")
            )
            return
        found: list[tuple[Music, str]] = []
        for music in self.bot.pjsk.musics():  # type: ignore[union-attr]
            for d in music.difficulties:
                if d.play_level == level:
                    found.append((music, d.difficulty))
        found.sort(key=lambda x: (DIFFICULTY_ORDER.index(x[1]), x[0].title.lower()))

        per_page = 25
        total_pages = max(1, math.ceil(len(found) / per_page))

        def render(page: int) -> discord.Embed:
            start = (page - 1) * per_page
            embed = embeds.embed(
                title=f"Level {level} Songs", color=discord.Color.blue()
            )
            embed.description = (
                "\n".join(
                    f"**{diff.capitalize()} {emojis.difficulty_colors[diff]}** - {m.title}"
                    for m, diff in found[start : start + per_page]
                )
                or "No songs found."
            ) + f"\n\n-# Page {page}/{total_pages}"
            return embed

        view = _Paginator(render, total_pages, interaction.user.id)
        await interaction.followup.send(embed=render(1), view=view)
        view.message = await interaction.original_response()

    @song.command(name="strategy", description="View a song's play strategy (FC/AP).")
    @app_commands.autocomplete(
        song=autocompletes.pjsk_song, difficulty=autocompletes.pjsk_difficulties
    )
    @app_commands.describe(song="Song name or ID.", difficulty="Chart difficulty.")
    async def strategy(
        self, interaction: discord.Interaction, song: str, difficulty: str = "default"
    ) -> None:
        await interaction.response.defer(thinking=True)
        music = await self._resolve_song(interaction, song)
        if not music:
            return
        diff = converters.match_difficulty(
            await self._default_difficulty(interaction.user.id, difficulty)
        )
        if not diff:
            await interaction.followup.send(
                embed=embeds.error_embed(f"`{difficulty}` isn't a valid difficulty.")
            )
            return

        async with aiohttp.ClientSession() as cs:
            async with cs.get(STRATEGY_META) as resp:
                meta = await resp.json(content_type=None)
            song_meta = None
            if music.id in meta.get("exists", []):
                async with cs.get(f"{meta['root']}{music.id}/meta.json") as resp:
                    song_meta = (await resp.json(content_type=None)).get(diff)

        embed = embeds.embed(title=music.title)
        if not song_meta:
            embed.description = (
                f"No strategy for **{emojis.difficulty_colors[diff]} {diff.title()}** on this song.\n\n"
                f"-# Contribute via the support server: {tools.command_mention(self.bot, 'help')}"
            )
            embed.color = discord.Color.red()
            await interaction.followup.send(embed=embed)
            return

        strat = song_meta["strats"][0]
        async with aiohttp.ClientSession() as cs:
            async with cs.get(f"{meta['root']}{music.id}/{strat['path']}") as resp:
                img = BytesIO(await resp.read())
        embed.set_image(url="attachment://strat.png")
        embed.set_thumbnail(url=music.jacket_url)
        embed.set_author(name=f"Strategy made by {strat['author']}")
        embed.description = (
            "-# Red = right hand, blue = left hand. Fingers labeled 1-5 (thumb-pinky).\n\n"
            f"**Difficulty:** {emojis.difficulty_colors[diff]} {diff.title()}\n"
            f"**Fingers Required:** `{strat['fingers']}`"
            + (f"\n\n**{strat['title']}**" if strat.get("title") else "")
            + (f"\n{strat['description']}" if strat.get("description") else "")
        )
        await interaction.followup.send(
            embed=embed, file=discord.File(img, "strat.png")
        )


class _Paginator(SbugaView):
    def __init__(self, render, total_pages: int, restriction_id: int) -> None:
        super().__init__(restrict_to=restriction_id)
        self.render = render
        self.total_pages = total_pages
        self.current_page = 1
        self._update()

    def _update(self) -> None:
        self.previous_page.disabled = self.current_page == 1
        self.next_page.disabled = self.current_page == self.total_pages

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page > 1:
            self.current_page -= 1
        self._update()
        await interaction.response.edit_message(
            embed=self.render(self.current_page), view=self
        )

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page < self.total_pages:
            self.current_page += 1
        self._update()
        await interaction.response.edit_message(
            embed=self.render(self.current_page), view=self
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(SongInfo(bot))
