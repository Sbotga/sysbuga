from __future__ import annotations

import math
import time
from io import BytesIO
from typing import TYPE_CHECKING, Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from data.models import Music
from data.search import preprocess
from helpers import converters, embeds, tools
from helpers.autocompletes import autocompletes
from helpers.emojis import emojis
from helpers.views import LinkButtonView, SbugaView
from services.sbuga import SbugaError, SbugaNotFound

from helpers.config_loader import get_config

if TYPE_CHECKING:
    from main import SbugaBot

CHART_REGIONS = ["en", "jp"]
DIFFICULTY_ORDER = ["append", "master", "expert", "hard", "normal", "easy"]
STRATEGY_META = (
    "https://raw.githubusercontent.com/Sbotga/strategies/refs/heads/main/meta.json"
)

# Official PJSK difficulty colours (nxsk-chart-preview / OpenSekai PaletteStore).
# APPEND is the average of its gradient's two ends (171,147,255)+(255,124,217).
DIFFICULTY_COLORS = {
    "easy": discord.Color.from_rgb(17, 221, 119),
    "normal": discord.Color.from_rgb(51, 204, 255),
    "hard": discord.Color.from_rgb(255, 204, 0),
    "expert": discord.Color.from_rgb(255, 68, 119),
    "master": discord.Color.from_rgb(204, 51, 255),
    "append": discord.Color.from_rgb(213, 136, 236),
}

_FIELD_LIMIT = 1024


def _alias_field(values: list[str]) -> str:
    """Comma-joined aliases, trimmed to fit an embed field."""
    if not values:
        return "*None*"
    text = ", ".join(values)
    if len(text) + 2 > _FIELD_LIMIT:
        text = text[: _FIELD_LIMIT - 6].rsplit(", ", 1)[0] + ", …"
    return f"`{text}`"


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

    @song.command(name="constant", description="View a song's chart constant.")
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
        region="Which server's chart to show (default: auto).",
        mirror="Show the mirrored chart (defaults to your setting).",
    )
    async def chart(
        self,
        interaction: discord.Interaction,
        song: str,
        difficulty: str = "default",
        region: Literal["jp", "en"] | None = None,
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
        regions = (
            [region] if region else await self._chart_region(music, interaction.user.id)
        )
        for r in regions:
            try:
                chart_bytes = await self.bot.sbuga.get_chart_image(music.id, diff, r, mirrored=bool(mirror))  # type: ignore[union-attr,arg-type]
                used_region = r
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

    @song.command(name="custom", description="View a custom chart/score by its id.")
    @app_commands.describe(
        chart_id="The published custom chart/score id.",
        hide_chart="Hide the chart image (and combo count).",
        mirror="Show the mirrored chart (defaults to your setting).",
    )
    async def custom(
        self,
        interaction: discord.Interaction,
        chart_id: str,
        hide_chart: bool = False,
        mirror: bool | None = None,
    ) -> None:
        region = "jp"  # only jp is available for now
        await interaction.response.defer(thinking=True)

        if mirror is None:
            mirror = await self.bot.user_data.get_settings(interaction.user.id, "mirror_charts_by_default")  # type: ignore[union-attr]

        # render first (unless hidden): the image endpoint counts the combo from
        # the score and caches it onto the metadata, so the info fetch picks it up
        chart_bytes = None
        if not hide_chart:
            try:
                chart_bytes = await self.bot.sbuga.get_custom_chart_image(chart_id, region, mirrored=bool(mirror))  # type: ignore[union-attr]
            except SbugaNotFound:
                await interaction.followup.send(
                    embed=embeds.error_embed(f"No custom chart with id `{chart_id}`.")
                )
                return
            except SbugaError as e:
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        f"Couldn't render that custom chart: {e.detail or e.status}"
                    )
                )
                return

        try:
            info = await self.bot.sbuga.get_custom_chart_info(chart_id, region)  # type: ignore[union-attr]
        except SbugaNotFound:
            await interaction.followup.send(
                embed=embeds.error_embed(f"No custom chart with id `{chart_id}`.")
            )
            return
        except SbugaError:
            info = {}

        level1 = info.get("userCustomMusicScoreInfoJson") or {}
        inner = level1.get("userCustomMusicScoreInfoJson") or {}
        custom_title = inner.get("title") or "Custom Chart"
        music_id = inner.get("musicId") or level1.get("musicId")
        diff = (level1.get("musicDifficultyType") or "").lower()
        play_level = level1.get("playLevel")
        description = level1.get("description") or ""
        play_count = level1.get("playCount")
        fc_rate = level1.get("fullComboRate")
        like_count = level1.get("reviewCount")
        combo_count = info.get("combo_count")
        refreshed_at = info.get("refreshed_at")

        base = self.bot.pjsk.get_music(music_id) if music_id is not None else None  # type: ignore[union-attr]
        original = base.title if base else "Unknown"

        official_creator = (info.get("officialCreator") or {}).get("name")

        desc_lines = []
        if mirror and not hide_chart:
            desc_lines += ["***MIRRORED CHART***", ""]
        desc_lines.append(f"**Original Song:** {original}")
        if official_creator:
            desc_lines.append(f"**Official Creator:** {official_creator}")
        if description.strip():
            desc_lines += ["", description]
        else:
            desc_lines += ["", "*No description by user.*"]

        embed = embeds.embed(
            title=custom_title,
            description="\n".join(desc_lines),
            color=DIFFICULTY_COLORS.get(diff, discord.Color.blurple()),
        )
        diff_emoji = emojis.difficulty_colors.get(diff, "")
        embed.add_field(
            name="Play Level",
            value=str(play_level if play_level is not None else "?"),
            inline=False,
        )
        embed.add_field(
            name="Difficulty",
            value=f"{diff_emoji} {diff.title()}".strip() or "?",
            inline=False,
        )
        if not hide_chart and combo_count is not None:
            embed.add_field(name="Combo", value=f"{combo_count:,}", inline=True)
        if play_count is not None:
            embed.add_field(name="Plays", value=f"{play_count:,}", inline=True)
        if fc_rate is not None:
            embed.add_field(name="FC Rate", value=f"{fc_rate:.1f}%", inline=True)
        if like_count is not None:
            embed.add_field(name="Likes", value=f"{like_count:,}", inline=True)
        if base and base.jacket_url:
            embed.set_thumbnail(url=base.jacket_url)
        if chart_bytes:
            embed.set_image(url="attachment://chart.png")
        if refreshed_at:
            embed.set_footer(
                text=f"Last refreshed {round(time.time() - refreshed_at)}s ago"
            )

        url = (
            f"{self.bot.sbuga.base}/api/tools/custom_chart"  # type: ignore[union-attr]
            f"?chart_id={chart_id}&region={region}&chart_image=true"
            f"&mirrored={str(bool(mirror)).lower()}"
        )
        sonolus_url = None
        if get_config()["sbuga"]["sonolus_url"]:
            sonolus_url = f"{get_config()['sbuga']['sonolus_url']}/playlists/sss-custom-{region}-{chart_id}"
        buttons = []
        if not hide_chart:
            buttons.append(("Open Chart", url))
        # if sonolus_url:
        #     buttons.append(("Play On Sonolus", sonolus_url))
        if buttons:
            view = LinkButtonView(buttons)
        else:
            view = None
        file = (
            discord.File(BytesIO(chart_bytes), "chart.png")
            if chart_bytes
            else discord.utils.MISSING
        )
        if view:
            await interaction.followup.send(
                content=f"`{chart_id}`", embed=embed, file=file, view=view
            )
            view.message = await interaction.original_response()
        else:
            await interaction.followup.send(
                content=f"`{chart_id}`", embed=embed, file=file
            )

    @song.command(name="info", description="View a song's data.")
    @app_commands.autocomplete(song=autocompletes.pjsk_song)
    @app_commands.describe(song="Song name or ID.")
    async def info(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer(thinking=True)
        music = await self._resolve_song(interaction, song)
        if not music:
            return
        if self.bot.pjsk.is_music_leaked(music.id):  # type: ignore[union-attr]
            await interaction.followup.send(embed=embeds.leak_embed())
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
        if music.published_at:
            lines.append(f"**Released:** <t:{int(music.published_at / 1000)}:D>")
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

        manual = sorted(self.bot.pjsk.song_aliases(music.id))  # type: ignore[union-attr]

        # Show the keys the *matcher* actually accepts (not the backend's
        # title_variants), minus the manual aliases, the title, and the bare id.
        skip = {preprocess(a) for a in manual} | {
            preprocess(music.title),
            str(music.id),
        }
        auto = [k for k in self.bot.pjsk.song_keys(music.id) if k not in skip]  # type: ignore[union-attr]

        embed = embeds.embed(
            title="Aliases",
            description=f"Aliases for `{music.title}` (ID `{music.id}`)",
        )
        embed.add_field(name="Manually Added", value=_alias_field(manual), inline=False)
        embed.add_field(
            name="Automatically Generated", value=_alias_field(auto), inline=False
        )
        await interaction.followup.send(embed=embed)

    def _chart_constant(self, music_id: int, difficulty: str) -> float | None:
        """The chart constant, or None when the chart has no rating yet. Uses the same
        override chain as everything else (spreadsheet 2, then the 39s sheet) and never
        falls back to the play level."""
        try:
            value = self.bot.constants.get_sync(  # type: ignore[union-attr]
                music_id, difficulty, ap=True, error_on_not_found=True
            )
        except IndexError:
            return None
        return float(value) if isinstance(value, (int, float)) else None

    @song.command(name="difficulty", description="Find all songs of a level.")
    @app_commands.describe(
        level="Level to search (1-39).",
        by_constants="Sort each difficulty by chart constant, hardest first, and show it.",
    )
    async def difficulty(
        self, interaction: discord.Interaction, level: int, by_constants: bool = False
    ) -> None:
        await interaction.response.defer(thinking=True)
        if not 0 < level < 40:
            await interaction.followup.send(
                embed=embeds.error_embed("Level must be between 1 and 39.")
            )
            return
        found: list[tuple[Music, str, float | None]] = []
        for music in self.bot.pjsk.musics():  # type: ignore[union-attr]
            for d in music.difficulties:
                if d.play_level == level:
                    constant = (
                        self._chart_constant(music.id, d.difficulty)
                        if by_constants
                        else None
                    )
                    found.append((music, d.difficulty, constant))

        if by_constants:
            # within a difficulty: highest constant first, unrated charts last
            found.sort(
                key=lambda x: (
                    DIFFICULTY_ORDER.index(x[1]),
                    x[2] is None,
                    -(x[2] or 0.0),
                    x[0].title.lower(),
                )
            )
        else:
            found.sort(key=lambda x: (DIFFICULTY_ORDER.index(x[1]), x[0].title.lower()))

        def line(music: Music, diff: str, constant: float | None) -> str:
            label = f"**{diff.capitalize()} {emojis.difficulty_colors[diff]}**"
            if not by_constants:
                return f"{label} - {music.title}"
            shown = f"{constant:.1f}" if constant is not None else "??.?"
            return f"{label} {level} ({shown}) - {music.title}"

        per_page = 25
        total_pages = max(1, math.ceil(len(found) / per_page))

        def render(page: int) -> discord.Embed:
            start = (page - 1) * per_page
            embed = embeds.embed(
                title=f"Level {level} Songs", color=discord.Color.blue()
            )
            embed.description = (
                "\n".join(line(*row) for row in found[start : start + per_page])
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
