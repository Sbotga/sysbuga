from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.autocompletes import autocompletes
from helpers.views import SbugaView
from services import heatmap
from services.event_store import EVENT_REGIONS, iter_snapshots, read_current_event
from services.models import CurrentEventResponse

if TYPE_CHECKING:
    from cogs.information import InfoCog
    from data.models import Event
    from main import SbugaBot

_HEATMAP_MAX_TIER = 100  # heatmap only covers the top 100


def _parse_tier(text: str) -> int | None:
    """accept a tier written as 100, t100 or T100 -> 100; None if it isn't a plain number"""
    text = text.strip().lstrip("tT").strip()
    return int(text) if text.isdigit() else None


def _border_ranks(data: CurrentEventResponse) -> list[int]:
    """the border tiers the api is currently returning for this event, ascending"""
    border = data.border or {}
    ranks = {
        r.get("rank")
        for r in border.get("borderRankings", [])
        if r.get("rank") is not None
    }
    return sorted(ranks)


def _tier_options(data: CurrentEventResponse, limit: int | None = None) -> list[int]:
    """valid tiers, highest rank first: the top-100 ranks plus the border tiers the api is
    returning, capped at `limit` and deduplicated"""
    tiers = set(range(1, 101)) | set(_border_ranks(data))
    if limit is not None:
        tiers = {tier for tier in tiers if tier <= limit}
    return sorted(tiers, reverse=True)


def _tier_autocomplete(limit: int | None = None):
    """an autocomplete that suggests tiers up to `limit`, highest first, filtered by what's typed"""

    async def autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        region = getattr(interaction.namespace, "region", None)
        if region not in EVENT_REGIONS:
            region = "en"  # border tiers are the same across regions; just need any live event
        data = await read_current_event(region)
        if data and data.event_id:
            options = _tier_options(data, limit)
        else:
            options = sorted(
                {tier for tier in range(1, 101) if limit is None or tier <= limit},
                reverse=True,
            )
        query = current.strip().lstrip("tT").strip()
        if query:
            options = [tier for tier in options if str(tier).startswith(query)]
        return [
            app_commands.Choice(name=f"T{tier}", value=str(tier))
            for tier in options[:25]
        ]

    return autocomplete


async def _timezone_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=tz, value=tz)
        for tz in heatmap.timezone_suggestions(current)
    ]


@dataclass
class _Selection:
    """one view of an event: the overall ranking, or a single world-link chapter."""

    character_id: int | None  # focus character of a chapter; None for overall/finale
    button_label: (
        str  # "Overall" or the character's name; also the image's section line
    )
    start_at: int
    end_at: int
    progressed: bool  # the chapter has started (complete or in progress)
    is_chapter: bool


@dataclass
class _HeatmapState:
    """everything needed to (re)render any selection of one heatmap, minus the selection."""

    resolved: str
    tz: object
    tz_label: str
    tz_overridden: bool
    data: CurrentEventResponse
    mode: str
    key: int
    label: str
    event_name: str
    event_id: int
    username: str | None
    thumb_png: bytes | None
    world_link: bool  # show the chapter/"Overall" section line and chapter buttons


def _overall_rank(data: CurrentEventResponse, key: int) -> int | None:
    for row in (data.top_100 or {}).get("rankings", []):
        if str(row.get("userId")) == str(key):
            return row.get("rank")
    return None


def _chapter_rank(data: CurrentEventResponse, key: int, cid: int | None) -> int | None:
    for chap in (data.top_100 or {}).get("userWorldBloomChapterRankings", []):
        if chap.get("gameCharacterId") == cid:
            for row in chap.get("rankings", []):
                if str(row.get("userId")) == str(key):
                    return row.get("rank")
    return None


async def _render_selection(
    state: _HeatmapState, sel: _Selection
) -> tuple[discord.Embed, discord.File]:
    """render one selection to (embed, attached png). No bot access needed."""
    current_rank: int | None = None
    if state.mode == "user":
        current_rank = (
            _chapter_rank(state.data, state.key, sel.character_id)
            if sel.is_chapter
            else _overall_rank(state.data, state.key)
        )
    # the title is constant across selections; the chapter name goes on its own line in the
    # image (the section) so switching chapters only changes the graph, not the embed
    section = sel.button_label if state.world_link else None
    graph_title = " ".join(
        p
        for p in (
            f"({state.resolved.upper()})",
            state.event_name,
            state.label,
            "Heatmap",
        )
        if p
    )
    embed = embeds.embed(
        title=" ".join(p for p in (state.event_name, state.label, "Heatmap") if p),
        color=discord.Color.purple(),
    )
    embed.description = f"**Last Data Update:** <t:{int(state.data.updated)}:R>"
    embed.set_footer(text=state.resolved.upper())
    # a lazy generator - streamed + parsed inside the worker thread, so a full event's
    # snapshots never all sit in memory at once
    png = await asyncio.to_thread(
        heatmap.render_heatmap,
        sel.start_at,
        sel.end_at,
        int(time.time() * 1000),
        graph_title,
        state.tz,
        state.tz_label,
        state.tz_overridden,
        iter_snapshots(state.resolved, state.event_id),
        state.mode,
        state.key,
        current_rank,
        state.username,
        state.thumb_png,
        section,
        sel.character_id,
        sel.is_chapter,
    )
    embed.set_image(url="attachment://heatmap.png")
    return embed, discord.File(io.BytesIO(png), filename="heatmap.png")


class _ChapterButton(discord.ui.Button):
    """switches the heatmap to one selection. Grayed out when it's the current view or an
    unavailable (not-yet-started) chapter; clickable otherwise."""

    def __init__(self, index: int, sel: _Selection, current: bool) -> None:
        available = sel.progressed and not current
        super().__init__(
            label=sel.button_label,
            style=(
                discord.ButtonStyle.primary
                if available
                else discord.ButtonStyle.secondary
            ),
            disabled=not available,
            row=index // 5,
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, _HeatmapView)
        await self.view.show(interaction, self.index)


class _HeatmapView(SbugaView):
    """chapter navigation for a world-link heatmap: an Overall button plus one per chapter."""

    def __init__(
        self, state: _HeatmapState, selections: list[_Selection], restrict_to: int
    ) -> None:
        super().__init__(timeout=600, restrict_to=restrict_to)
        self.state = state
        self.selections = selections
        self.selected = 0
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for i, sel in enumerate(self.selections):
            self.add_item(_ChapterButton(i, sel, current=i == self.selected))

    async def show(self, interaction: discord.Interaction, index: int) -> None:
        self.selected = index
        self._build()
        await interaction.response.defer()  # render may take a moment; ack first
        embed, file = await _render_selection(self.state, self.selections[index])
        await interaction.edit_original_response(
            embed=embed, attachments=[file], view=self
        )


class HeatmapCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot

    async def _resolve_region(
        self, interaction: discord.Interaction, region: str
    ) -> str | None:
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(interaction.user.id, "default_region")  # type: ignore[union-attr]
        if region not in EVENT_REGIONS:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Region `{region.upper()}` isn't supported.")
            )
            return None
        return region

    heatmap_group = app_commands.Group(
        name="heatmap",
        description="Games-per-hour heatmaps for the current event (top 100 only).",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    async def _heatmap_setup(
        self, interaction: discord.Interaction, region: str, timezone: str | None
    ) -> tuple[str, object, str, bool, CurrentEventResponse] | None:
        """resolve region + timezone + current event, sending an error and returning None on
        failure. returns (region, tz, tz_label, tz_overridden, current_event) on success.
        """
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return None
        if timezone and not heatmap.is_valid_tz(timezone):
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"`{timezone}` isn't a valid timezone. Use a common one "
                    f"({', '.join(heatmap.TIMEZONES)}) or an IANA name like `Europe/Paris`."
                )
            )
            return None
        tz_name = timezone or await self.bot.user_data.get_settings(  # type: ignore[union-attr]
            interaction.user.id, "timezone"
        )
        tz, tz_label = heatmap.resolve_tz(tz_name)
        data = await read_current_event(resolved)
        if data is None or data.event_id is None:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active event right now.")
            )
            return None
        return resolved, tz, tz_label, timezone is not None, data

    def _selections(self, timing: Event, event_name: str) -> list[_Selection]:
        """the Overall view plus one per world-link chapter (empty of chapters for a normal
        event). chapters are ordered by chapter number and labelled by their focus character.
        """
        now = int(time.time() * 1000)
        selections = [
            _Selection(
                character_id=None,
                button_label="Overall",
                start_at=timing.start_at,  # type: ignore[arg-type]
                end_at=timing.aggregate_at,  # type: ignore[arg-type]
                progressed=True,
                is_chapter=False,
            )
        ]
        for wb in timing.world_blooms:
            char = self.bot.pjsk.get_character(wb.game_character_id) if wb.game_character_id else None  # type: ignore[union-attr,arg-type]
            name = (
                char.given_name
                if char and char.given_name
                else f"Chapter {wb.chapter_no}"
            )
            selections.append(
                _Selection(
                    character_id=wb.game_character_id,
                    button_label=name,
                    start_at=wb.start_at,
                    end_at=wb.aggregate_at,
                    progressed=wb.start_at <= now,
                    is_chapter=True,
                )
            )
        return selections

    async def _send_heatmap(
        self,
        interaction: discord.Interaction,
        resolved: str,
        tz: object,
        tz_label: str,
        tz_overridden: bool,
        data: CurrentEventResponse,
        mode: str,
        key: int,
        label: str,
        *,
        username: str | None = None,
        thumb_png: bytes | None = None,
    ) -> None:
        event_obj = self.bot.pjsk.get_event(data.event_id)  # type: ignore[union-attr,arg-type]
        event_name = event_obj.name if event_obj else "Event"
        # the region's own event carries region-specific chapter timing; fall back to the merge
        timing = (
            next(
                (
                    e
                    for e in self.bot.pjsk.region_events(resolved)  # type: ignore[union-attr]
                    if e.id == data.event_id
                ),
                None,
            )
            or event_obj
        )
        if not (timing and timing.start_at and timing.aggregate_at):
            embed = embeds.embed(
                title=" ".join(p for p in (event_name, label, "Heatmap") if p),
                color=discord.Color.purple(),
            )
            embed.description = f"**Last Data Update:** <t:{int(data.updated)}:R>"
            embed.set_footer(text=resolved.upper())
            await interaction.followup.send(embed=embed)
            return

        selections = self._selections(timing, event_name)
        world_link = len(selections) > 1  # only world-link events have chapters
        state = _HeatmapState(
            resolved=resolved,
            tz=tz,
            tz_label=tz_label,
            tz_overridden=tz_overridden,
            data=data,
            mode=mode,
            key=key,
            label=label,
            event_name=event_name,
            event_id=data.event_id,  # type: ignore[arg-type]
            username=username,
            thumb_png=thumb_png,
            world_link=world_link,
        )
        embed, file = await _render_selection(state, selections[0])
        view = (
            _HeatmapView(state, selections, restrict_to=interaction.user.id)
            if world_link
            else None
        )
        await interaction.followup.send(
            embed=embed, files=[file], view=view or discord.utils.MISSING
        )
        if view is not None:
            view.message = await interaction.original_response()

    async def _user_identity(
        self, resolved: str, user_id: int, data: CurrentEventResponse
    ) -> tuple[str | None, bytes | None]:
        """(display name, leader-card thumbnail png) for a tracked player: name and thumbnail
        from their profile, falling back to the live ranking row's name if it can't be fetched.
        """
        row_name: str | None = None
        for row in (data.top_100 or {}).get("rankings", []):
            if str(row.get("userId")) == str(user_id):
                row_name = row.get("name")
                break

        username: str | None = None
        thumb: bytes | None = None
        try:
            resp = await self.bot.sbuga.get_profile(user_id, resolved, fresh=False)  # type: ignore[arg-type,union-attr]
            profile = resp.profile
            username = (profile.get("user") or {}).get("name")
            info = cast("InfoCog | None", self.bot.get_cog("InfoCog"))
            if info is not None:
                thumb = await info._leader_thumbnail_bytes(profile)
        except Exception:
            pass
        return username or row_name, thumb

    @heatmap_group.command(
        name="cutoff",
        description="A tier's games-per-hour heatmap for the current event.",
    )
    @app_commands.autocomplete(
        tier=_tier_autocomplete(_HEATMAP_MAX_TIER),
        region=autocompletes.pjsk_region(EVENT_REGIONS),
        timezone=_timezone_autocomplete,
    )
    @app_commands.describe(
        tier="Rank 1-100 (e.g. 5, 100, T100).",
        region="Game server region.",
        timezone="Timezone for the hours/days (defaults to your setting, or ET).",
    )
    async def heatmap_cutoff(
        self,
        interaction: discord.Interaction,
        tier: str,
        region: str = "default",
        timezone: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        setup = await self._heatmap_setup(interaction, region, timezone)
        if setup is None:
            return
        resolved, tz, tz_label, tz_overridden, data = setup

        parsed = _parse_tier(tier)
        if parsed is None or parsed not in set(_tier_options(data, _HEATMAP_MAX_TIER)):
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"`{tier}` isn't a valid tier. Pick a rank from 1-100 (e.g. `T50`)."
                )
            )
            return
        await self._send_heatmap(
            interaction,
            resolved,
            tz,
            tz_label,
            tz_overridden,
            data,
            "cutoff",
            parsed,
            f"T{parsed}",
        )

    @heatmap_group.command(
        name="user",
        description="A player's games-per-hour heatmap (while in the top 100).",
    )
    @app_commands.autocomplete(
        region=autocompletes.pjsk_region(EVENT_REGIONS),
        timezone=_timezone_autocomplete,
    )
    @app_commands.describe(
        user_id="PJSK user ID (omit to use your linked account).",
        region="Game server region.",
        timezone="Timezone for the hours/days (defaults to your setting, or ET).",
    )
    async def heatmap_user(
        self,
        interaction: discord.Interaction,
        user_id: str | None = None,
        region: str = "default",
        timezone: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        setup = await self._heatmap_setup(interaction, region, timezone)
        if setup is None:
            return
        resolved, tz, tz_label, tz_overridden, data = setup

        if not user_id:
            linked = await self.bot.user_data.get_pjsk_id(interaction.user.id, resolved)  # type: ignore[union-attr]
            if not linked:
                await interaction.followup.send(
                    embed=embeds.error_embed(
                        f"Link your {resolved.upper()} PJSK account, or pass a user ID."
                    )
                )
                return
            user_id = str(linked)
        if not user_id.isdigit():
            await interaction.followup.send(
                embed=embeds.error_embed("Invalid user ID.")
            )
            return
        uid = int(user_id)
        username, thumb = await self._user_identity(resolved, uid, data)
        await self._send_heatmap(
            interaction,
            resolved,
            tz,
            tz_label,
            tz_overridden,
            data,
            "user",
            uid,
            "",
            username=username,
            thumb_png=thumb,
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(HeatmapCog(bot))
