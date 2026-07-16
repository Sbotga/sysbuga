from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cogs.heatmap import _chapter_autocomplete
from cogs.information import _render_leader_card
from data.models import Card, Event
from data.pjsk import character_display_name
from data.search import preprocess
from helpers import converters, embeds, tools
from helpers.autocompletes import autocompletes
from helpers.emojis import emojis
from helpers.views import SbugaView
from services import event_story, graph, leaderboard as lb
from services.event_store import EVENT_REGIONS, iter_snapshots, read_current_event

if TYPE_CHECKING:
    from main import SbugaBot

_FIELD_LIMIT = 1024

_LB_PER_PAGE = 20
_LB_FOOTER = "inspired by GhostNeneRobo"
# the tiers /event cutoff offers - the top-100 ranks worth showing plus every border tier the
# api returns, which is exactly what we save per poll
_CUTOFF_TIERS = [
    10, 20, 30, 40, 50, 100, 200, 300, 400, 500, 1000, 1500, 2000, 2500, 3000,
    4000, 5000, 10000, 20000, 30000, 40000, 50000, 100000,
]  # fmt: skip
# rendered leader cards, keyed by everything that changes the art - the same players show up
# across page/ALT/OFFSET flips, so this saves re-fetching their card every render
_CARD_CACHE: dict[tuple, bytes] = {}


@dataclass
class _LBSel:
    """one leaderboard view: the overall ranking, or a single world-link chapter"""

    character_id: int | None
    label: str  # "Overall" or the chapter's focus character
    progressed: bool
    is_chapter: bool


def _delta(d: int) -> tuple[int, int]:
    """(direction, places) for a rank change since an hour ago"""
    return (1 if d > 0 else -1 if d < 0 else 0), abs(d)


def _alias_field(values: list[str]) -> str:
    """comma-joined aliases trimmed to fit an embed field"""
    if not values:
        return "*None*"
    text = ", ".join(values)
    if len(text) + 2 > _FIELD_LIMIT:
        text = text[: _FIELD_LIMIT - 6].rsplit(", ", 1)[0] + ", …"
    return f"`{text}`"


EVENT_TYPE_NAMES = {
    "marathon": "Marathon",
    "cheerful_carnival": "Cheerful Carnival",
    "world_bloom": "World Link",
}


class EventsCog(commands.Cog):
    # the minutely event fetch runs in its own process (run_event_worker.py); this cog only reads
    # the files it writes, via services.event_store
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot
        self._vlive_cache: dict[str, tuple[float, list]] = (
            {}
        )  # region -> (fetched_at, data)

    async def _leak_blocked(
        self, interaction: discord.Interaction, event_id: int
    ) -> bool:
        """True when the event is a leak and this channel isn't whitelisted for leaks"""
        if not self.bot.pjsk.is_event_leaked(event_id):  # type: ignore[union-attr]
            return False
        return not await self.bot.user_data.channel_leaks_allowed(
            interaction.channel_id
        )  # type: ignore[union-attr,arg-type]

    async def _resolve_region(
        self, interaction: discord.Interaction, region: str
    ) -> str | None:
        region = region.lower().strip()
        if region == "default":
            region = await self.bot.user_data.get_settings(
                interaction.user.id, "default_region"
            )  # type: ignore[union-attr]
        if region not in EVENT_REGIONS:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Region `{region.upper()}` isn't supported.")
            )
            return None
        return region

    async def _event_embed(self, event: Event) -> discord.Embed:
        embed = embeds.embed(title=event.name, color=discord.Color.purple())
        lines = [
            f"**Type:** {EVENT_TYPE_NAMES.get(event.event_type or '', event.event_type)}",
            f"**ID:** `{event.id}`",
            f"**Unit:** {await event_story.unit_display(self.bot.sbuga, event.id)}",  # type: ignore[arg-type]
        ]
        if event.bonus_attribute:
            attr_emoji = emojis.attributes.get(event.bonus_attribute, "")
            lines.append(
                f"**Bonus Attribute:** {attr_emoji} {event.bonus_attribute.title()}".replace(
                    "  ", " "
                )
            )
        if event.bonus_character_ids:
            names = []
            for cid in event.bonus_character_ids:
                char = self.bot.pjsk.get_character(cid)  # type: ignore[union-attr]
                if char:
                    names.append(character_display_name(char))
            if names:
                lines.append(f"**Bonus Characters:** {', '.join(names)}")
        if event.start_at:
            lines.append(f"**Starts:** <t:{int(event.start_at / 1000)}:R>")
        if event.aggregate_at:
            lines.append(f"**Ends:** <t:{int(event.aggregate_at / 1000)}:R>")
        embed.description = "\n".join(lines)
        image = event.banner_url or event.background_url
        if image:
            embed.set_image(url=image)
        if event.logo_url:
            embed.set_thumbnail(url=event.logo_url)
        return embed

    event = app_commands.Group(
        name="event",
        description="PJSK event info and leaderboards.",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @event.command(name="info", description="View a PJSK event's details.")
    @app_commands.autocomplete(
        event=autocompletes.pjsk_event, region=autocompletes.pjsk_region(EVENT_REGIONS)
    )
    @app_commands.describe(
        event="Event name (omit for the current event).", region="Game server region."
    )
    async def info(
        self,
        interaction: discord.Interaction,
        event: str | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return

        event_obj: Event | None = None
        if event and event.isdigit():
            event_obj = self.bot.pjsk.get_event(int(event))  # type: ignore[union-attr]
        elif event:
            eid = self.bot.pjsk.best_event_id(event)  # type: ignore[union-attr]
            event_obj = self.bot.pjsk.get_event(eid) if eid is not None else None  # type: ignore[union-attr]
        else:
            current = await read_current_event(resolved)
            if current and current.event_id:
                event_obj = self.bot.pjsk.get_event(current.event_id)  # type: ignore[union-attr]

        if not event_obj:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't find that event.")
            )
            return
        if await self._leak_blocked(interaction, event_obj.id):
            await interaction.followup.send(embed=embeds.leak_embed())
            return
        await interaction.followup.send(embed=await self._event_embed(event_obj))

    @event.command(name="aliases", description="View an event's aliases.")
    @app_commands.autocomplete(event=autocompletes.pjsk_event)
    @app_commands.describe(event="Event name or ID.")
    async def aliases(self, interaction: discord.Interaction, event: str) -> None:
        await interaction.response.defer(thinking=True)
        ev = converters.match_event(self.bot.pjsk, event)  # type: ignore[arg-type]
        if not ev:
            await interaction.followup.send(
                embed=embeds.error_embed(f"Couldn't find an event matching `{event}`.")
            )
            return
        if await self._leak_blocked(interaction, ev.id):
            await interaction.followup.send(embed=embeds.leak_embed())
            return
        manual = sorted(self.bot.pjsk.event_aliases(ev.id))  # type: ignore[union-attr]
        # the keys the matcher accepts, minus the manual aliases, the name, and the bare id
        skip = {preprocess(a) for a in manual} | {preprocess(ev.name), str(ev.id)}
        auto = [k for k in self.bot.pjsk.event_keys(ev.id) if k not in skip]  # type: ignore[union-attr]
        embed = embeds.embed(
            title="Aliases", description=f"Aliases for `{ev.name}` (ID `{ev.id}`)"
        )
        embed.add_field(name="Manually Added", value=_alias_field(manual), inline=False)
        embed.add_field(
            name="Automatically Generated", value=_alias_field(auto), inline=False
        )
        await interaction.followup.send(embed=embed)

    def _lb_selections(self, region: str, event_id: int) -> list[_LBSel]:
        """Overall plus one entry per world-link chapter (empty of chapters on a normal event)."""
        timing = next(
            (e for e in self.bot.pjsk.region_events(region) if e.id == event_id),  # type: ignore[union-attr]
            None,
        ) or self.bot.pjsk.get_event(
            event_id
        )  # type: ignore[union-attr]
        sels = [_LBSel(None, "Overall", True, False)]
        now = int(time.time() * 1000)
        for wb in timing.world_blooms if timing else []:
            char = (
                self.bot.pjsk.get_character(wb.game_character_id)
                if wb.game_character_id
                else None
            )  # type: ignore[union-attr,arg-type]
            sels.append(
                _LBSel(
                    character_id=wb.game_character_id,
                    label=(
                        char.given_name
                        if char and char.given_name
                        else f"Chapter {wb.chapter_no}"
                    ),
                    progressed=wb.start_at <= now,
                    is_chapter=True,
                )
            )
        return sels

    async def _lb_card(self, user_card: dict) -> bytes | None:
        """The player's leader card art, straight off their ranking row - no profile fetch."""
        cid = user_card.get("cardId")
        card: Card | None = self.bot.pjsk.get_card(cid) if cid else None  # type: ignore[union-attr,arg-type]
        if not card or not card.attr:
            return None
        trained = user_card.get("defaultImage") == "special_training" and bool(
            card.thumbnail_url_trained
        )
        key = (cid, user_card.get("level"), user_card.get("masterRank", 0), trained)
        if key in _CARD_CACHE:
            return _CARD_CACHE[key]
        url = card.thumbnail_url_trained if trained else card.thumbnail_url_normal
        if not url:
            return None
        try:
            png = await asyncio.to_thread(
                _render_leader_card,
                url,
                card.card_rarity_type,
                card.attr,
                user_card.get("level"),
                user_card.get("masterRank", 0),
                trained,
            )
        except Exception:
            return None
        _CARD_CACHE[key] = png
        return png

    def _cutoff_window(
        self, region: str, event_id: int, cid: int | None
    ) -> tuple[int, int] | None:
        """(ranking start, ranking end) for the overall event, or for one world-link chapter -
        a chapter's cutoff is measured over its own window, not the whole event's"""
        timing = next(
            (e for e in self.bot.pjsk.region_events(region) if e.id == event_id),  # type: ignore[union-attr]
            None,
        ) or self.bot.pjsk.get_event(
            event_id
        )  # type: ignore[union-attr]
        if timing is None:
            return None
        if cid is None:
            if timing.start_at is None or timing.aggregate_at is None:
                return None
            return timing.start_at, timing.aggregate_at
        for wb in timing.world_blooms:
            if wb.game_character_id == cid:
                return wb.start_at, wb.aggregate_at
        return None

    @event.command(
        name="cutoff", description="Detailed information about a tier's cutoff."
    )
    @app_commands.choices(
        tier=[app_commands.Choice(name=f"T{t}", value=t) for t in _CUTOFF_TIERS]
    )
    @app_commands.autocomplete(
        chapter=_chapter_autocomplete,
        region=autocompletes.pjsk_region(EVENT_REGIONS),
    )
    @app_commands.describe(
        tier="The cutoff tier specified.",
        chapter="World Link chapter (defaults to the overall event).",
        region="Game server region.",
    )
    async def cutoff(
        self,
        interaction: discord.Interaction,
        tier: int,
        chapter: str | None = None,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return
        data = await read_current_event(resolved)
        if data is None or not data.event_id:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active event right now.")
            )
            return

        cid = int(chapter) if chapter and chapter.isdigit() else None
        series, _ = await asyncio.to_thread(
            graph.cutoff_series,
            iter_snapshots(resolved, data.event_id),
            tier=tier,
            chapter_cid=cid,
            chapter=cid is not None,
        )
        if not series:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"No data saved for T{tier} on this event yet."
                )
            )
            return

        event_obj = self.bot.pjsk.get_event(data.event_id)  # type: ignore[union-attr]
        window = self._cutoff_window(resolved, data.event_id, cid)
        if window is None:
            await interaction.followup.send(
                embed=embeds.error_embed("That event's timings aren't available.")
            )
            return
        start_at, aggregate_at = window

        ts, score = series[-1]
        # the last reading at least an hour back - polls are minutely, so allow a minute of
        # drift the same way RoboNene does rather than demanding an exact 60:00 spacing
        last_hour = series[0]
        for point in reversed(series):
            if ts - point[0] >= 3_600_000 - 60_000:
                last_hour = point
                break

        elapsed = ts - start_at
        score_ph = round(score * 3_600_000 / elapsed) if elapsed > 0 else 0
        if ts > aggregate_at:  # ranking is over; nothing is moving any more
            last_hour_ts, speed = ts, 0
        else:
            last_hour_ts = last_hour[0]
            delta = ts - last_hour_ts
            speed = round((score - last_hour[1]) * 3_600_000 / delta) if delta else 0
        duration = max(1, aggregate_at - start_at)
        pct = min(elapsed * 100 / duration, 100)

        name = event_obj.name if event_obj else f"Event {data.event_id}"
        char = self.bot.pjsk.get_character(cid) if cid is not None else None  # type: ignore[union-attr]
        if char is not None:
            name += f" - {character_display_name(char)}"
        embed = embeds.embed(
            title=f"{name} T{tier} Cutoff", color=discord.Color.purple()
        )
        embed.description = f"**Requested:** <t:{ts // 1000}:R>"
        if event_obj and event_obj.logo_url:
            embed.set_thumbnail(url=event_obj.logo_url)
        embed.add_field(
            name="Cutoff Statistics",
            value=(
                f"Points: `{score:,}`\n"
                f"Avg. Speed (Per Hour): `{score_ph:,}/h`\n"
                f"Avg. Speed [<t:{last_hour_ts // 1000}:R> to <t:{ts // 1000}:R>] "
                f"(Per Hour): `{speed:,}/h`\n"
            ),
            inline=False,
        )
        embed.add_field(
            name="Event Information",
            value=(
                f"Ranking Started: <t:{start_at // 1000}:R>\n"
                f"Ranking Ends: <t:{aggregate_at // 1000}:R>\n"
                f"Percentage Through Event: `{pct:.2f}%`\n"
            ),
            inline=False,
        )
        embed.set_footer(text=f"{resolved.upper()} - {_LB_FOOTER}")
        await interaction.followup.send(embed=embed)

    @event.command(name="leaderboard", description="View the current event's top 100.")
    @app_commands.autocomplete(region=autocompletes.pjsk_region(EVENT_REGIONS))
    @app_commands.describe(region="Game server region.", rank="Jump to a rank (1-100).")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        region: str = "default",
        rank: int | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return
        data = await read_current_event(resolved)
        if data is None:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    "Live event data isn't available yet - try again shortly."
                )
            )
            return
        if not data.event_id or not (data.top_100 or {}).get("rankings"):
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active event right now.")
            )
            return
        if rank is not None and not 1 <= rank <= 100:
            await interaction.followup.send(
                embed=embeds.error_embed("Pick a rank between 1 and 100.")
            )
            return

        event_obj = self.bot.pjsk.get_event(data.event_id)  # type: ignore[union-attr]
        view = _LeaderboardView(
            cog=self,
            region=resolved,
            data=data,
            event_name=event_obj.name if event_obj else f"Event {data.event_id}",
            event_logo=event_obj.logo_url if event_obj else None,
            event_id=data.event_id,
            selections=self._lb_selections(resolved, data.event_id),
            pjsk_id=await self.bot.user_data.get_pjsk_id(interaction.user.id, resolved),  # type: ignore[union-attr]
            target=rank,
            restrict_to=interaction.user.id,
        )
        if rank is not None:
            view.page = (rank - 1) // _LB_PER_PAGE
        embed, file = await view.render()
        view.rebuild()
        await interaction.followup.send(embed=embed, file=file, view=view)
        view.message = await interaction.original_response()

    @event.command(
        name="schedule",
        description="View the current and next event, plus running virtual lives.",
    )
    @app_commands.autocomplete(region=autocompletes.pjsk_region(EVENT_REGIONS))
    @app_commands.describe(region="Game server region.")
    async def schedule(
        self, interaction: discord.Interaction, region: str = "default"
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return
        await interaction.followup.send(
            embed=self._schedule_embed(resolved),
            view=_ScheduleView(self, resolved),
        )

    def _schedule_embed(self, region: str) -> discord.Embed:
        now = int(time.time() * 1000)
        events = sorted(
            self.bot.pjsk.region_events(region),
            key=lambda e: e.start_at or 0,  # type: ignore[union-attr]
        )
        embed = embeds.embed(
            title=f"{region.upper()} Event Schedule", color=discord.Color.purple()
        )
        current = next(
            (
                e
                for e in events
                if (e.start_at or 0) <= now < (e.closed_at or e.aggregate_at or 0)
            ),
            None,
        )
        upcoming = [e for e in events if (e.start_at or 0) > now]
        next_event = min(upcoming, key=lambda e: e.start_at or 0) if upcoming else None
        if current:
            self._schedule_event_fields(embed, current, "Current Event", now)
        if next_event:
            self._schedule_event_fields(embed, next_event, "Next Event", now)
        if not current and not next_event:
            embed.add_field(
                name="Events", value="No current or upcoming events.", inline=False
            )
        embed.set_footer(text=f"{region.upper()} - times are your local time")
        return embed

    def _schedule_event_fields(
        self, embed: discord.Embed, event: Event, label: str, now: int
    ) -> None:
        type_name = EVENT_TYPE_NAMES.get(event.event_type or "", event.event_type)
        embed.add_field(
            name=f"__{label}__",
            value=f"**{tools.escape_md(event.name)}** *[{type_name}]* (ID `{event.id}`)",
            inline=False,
        )
        if event.start_at:
            ts = int(event.start_at / 1000)
            embed.add_field(
                name="Started" if event.start_at <= now else "Starts",
                value=f"<t:{ts}:f>\n<t:{ts}:R>",
                inline=True,
            )
        if event.aggregate_at:
            ts = int(event.aggregate_at / 1000)
            embed.add_field(
                name="Ranking Closes", value=f"<t:{ts}:f>\n<t:{ts}:R>", inline=True
            )
        if event.event_type == "world_bloom" and event.world_blooms:
            lines = []
            for wb in sorted(event.world_blooms, key=lambda w: w.chapter_no):
                char = (
                    self.bot.pjsk.get_character(wb.game_character_id)  # type: ignore[union-attr]
                    if wb.game_character_id
                    else None
                )
                name = character_display_name(char) if char else "Finale"
                lines.append(f"**{name}:** <t:{int(wb.start_at / 1000)}:R>")
            embed.add_field(
                name="World Link Chapters", value="\n".join(lines), inline=False
            )

    async def _vlive_embed(self, region: str) -> discord.Embed:
        embed = embeds.embed(
            title=f"{region.upper()} Virtual Lives", color=discord.Color.purple()
        )
        # small per-region cache since the master file is large and clicked repeatedly
        cached = self._vlive_cache.get(region)
        if cached and time.time() - cached[0] < 600:
            data = cached[1]
        else:
            try:
                data = await self.bot.sbuga.get_master("virtualLives", region)  # type: ignore[union-attr]
            except Exception:
                embed.description = "Virtual live data isn't available right now."
                return embed
            self._vlive_cache[region] = (time.time(), data)

        now = int(time.time() * 1000)
        blocks = []
        for vlive in data:
            if not (vlive.get("startAt") or 0) <= now < (vlive.get("endAt") or 0):
                continue
            upcoming = [
                s
                for s in vlive.get("virtualLiveSchedules", [])
                if (s.get("startAt") or 0) > now
            ]
            if not upcoming:
                continue
            times = "\n".join(
                f"<t:{int(s['startAt'] / 1000)}:t> (<t:{int(s['startAt'] / 1000)}:R>)"
                for s in upcoming[:6]
            )
            more = f"\n-# +{len(upcoming) - 6} more shows" if len(upcoming) > 6 else ""
            blocks.append(f"**{tools.escape_md(vlive['name'])}**\n{times}{more}")
        embed.description = (
            "\n\n".join(blocks) if blocks else "No virtual lives are running right now."
        )
        return embed


class _ScheduleView(SbugaView):
    """the /event schedule embed's Virtual Lives button (ephemeral reply, usable by anyone)"""

    def __init__(self, cog: "EventsCog", region: str) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.region = region

    @discord.ui.button(label="Virtual Lives", style=discord.ButtonStyle.primary)
    async def virtual_lives(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            embed=await self.cog._vlive_embed(self.region), ephemeral=True
        )


class _LBButton(discord.ui.Button):
    """picks a leaderboard view (Overall / a chapter). Grayed out when it's the current view or
    a chapter that hasn't started."""

    def __init__(self, index: int, sel: _LBSel, current: bool, row: int) -> None:
        available = sel.progressed and not current
        super().__init__(
            label=sel.label,
            style=(
                discord.ButtonStyle.primary
                if available
                else discord.ButtonStyle.secondary
            ),
            disabled=not available,
            row=row,
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view: _LeaderboardView = self.view  # type: ignore[assignment]
        view.selected = self.index
        view.page = 0
        await view.refresh(interaction)


class _LeaderboardView(SbugaView):
    """The rendered T100 leaderboard: paging, ALT/OFFSET toggles, and per-chapter views."""

    def __init__(
        self,
        *,
        cog: EventsCog,
        region: str,
        data,
        event_name: str,
        event_logo: str | None,
        event_id: int,
        selections: list[_LBSel],
        pjsk_id: int | None,
        target: int | None,
        restrict_to: int,
    ) -> None:
        super().__init__(timeout=300, restrict_to=restrict_to)
        self.cog = cog
        self.region = region
        self.data = data
        self.event_name = event_name
        self.event_logo = event_logo
        self.event_id = event_id
        self.selections = selections
        self.pjsk_id = pjsk_id
        self.target = target
        self.selected = 0
        self.page = 0
        self.alt = False
        self.offset = False
        self._stats: dict[int, tuple] = {}  # selection index -> hour_stats result

    # --- data ---

    def _rows(self) -> list[dict]:
        sel = self.selections[self.selected]
        top = self.data.top_100 or {}
        if not sel.is_chapter:
            return top.get("rankings") or []
        return next(
            (
                c.get("rankings") or []
                for c in top.get("userWorldBloomChapterRankings", [])
                if c.get("gameCharacterId") == sel.character_id
            ),
            [],
        )

    async def _hour_stats(self) -> tuple:
        if self.selected not in self._stats:
            sel = self.selections[self.selected]
            rows = self._rows()
            self._stats[self.selected] = await asyncio.to_thread(
                lb.hour_stats,
                iter_snapshots(self.region, self.event_id),
                [r.get("userId") for r in rows],
                sel.character_id,
                sel.is_chapter,
            )
        return self._stats[self.selected]

    # --- ui ---

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(self.prev)
        self.add_item(self.next)
        self.add_item(self.alt_toggle)
        self.add_item(self.offset_toggle)
        if len(self.selections) > 1:  # world link only
            for i, sel in enumerate(self.selections):
                self.add_item(_LBButton(i, sel, i == self.selected, row=1 + i // 5))

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._disable_all()
        await interaction.response.edit_message(view=self)
        embed, file = await self.render()
        self._enable_all()
        self.rebuild()
        await interaction.edit_original_response(
            embed=embed, attachments=[file], view=self
        )

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, row=0)
    async def prev(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = (self.page - 1) % self._pages()
        await self.refresh(interaction)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, row=0)
    async def next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = (self.page + 1) % self._pages()
        await self.refresh(interaction)

    @discord.ui.button(
        emoji="🔀", label="ALT", style=discord.ButtonStyle.secondary, row=0
    )
    async def alt_toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.alt = not self.alt
        await self.refresh(interaction)

    @discord.ui.button(
        emoji="🔃", label="OFFSET", style=discord.ButtonStyle.secondary, row=0
    )
    async def offset_toggle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.offset = not self.offset
        await self.refresh(interaction)

    def _pages(self) -> int:
        return max(1, (len(self._rows()) + _LB_PER_PAGE - 1) // _LB_PER_PAGE)

    # --- render ---

    async def render(self) -> tuple[discord.Embed, discord.File]:
        rows = self._rows()
        games, last_score, baseline, baseline_ts = await self._hour_stats()

        start = self.page * _LB_PER_PAGE + (10 if self.offset else 0)
        total = len(rows)
        window = [rows[(start + i) % total] for i in range(min(_LB_PER_PAGE, total))]

        columns = ["Score/GH", "Games", "GPH"] if self.alt else ["Score", "Change Hr"]
        out: list[lb.LBRow] = []
        for r in window:
            uid, score, rk = r.get("userId"), r.get("score") or 0, r.get("rank") or 0
            played = games.get(uid, 0)
            if last_score.get(uid) is not None and score > last_score[uid]:
                played += 1  # they've scored since the newest snapshot
            base = baseline.get(uid)
            change = score - base[0] if base else None
            gph = max(played - base[2], 0) if base else None
            direction, places = _delta(base[1] - rk if base else 0)
            if self.alt:
                values = [
                    f"{change / gph:,.2f}" if change is not None and gph else "N/A",
                    f"{played:,}",
                    f"{gph:,}" if gph is not None else "N/A",
                ]
            else:
                values = [f"{score:,}", f"{change:,}" if change is not None else "N/A"]
            out.append(
                lb.LBRow(
                    rank=f"T{rk}",
                    delta_dir=direction,
                    delta_n=places,
                    card=await self.cog._lb_card(r.get("userCard") or {}),
                    name=str(r.get("name") or "?").replace("\n", " ").strip() or "?",
                    values=values,
                    is_you=(uid is not None and uid == self.pjsk_id)
                    or (self.target is not None and rk == self.target),
                )
            )

        png = await asyncio.to_thread(lb.render_leaderboard, out, columns)
        sel = self.selections[self.selected]
        title = f"{self.event_name} - Top 100"
        if sel.is_chapter:
            title += f" ({sel.label})"
        embed = embeds.embed(title=title, color=discord.Color.purple())
        desc = f"**Updated:** <t:{int(self.data.updated)}:R>"
        if baseline_ts:
            desc += f"\n**Change since:** <t:{int(baseline_ts / 1000)}:R>"
        embed.description = desc
        if self.event_logo:
            embed.set_thumbnail(url=self.event_logo)
        embed.set_image(url="attachment://leaderboard.png")
        embed.set_footer(
            text=f"{self.region.upper()} - Page {self.page + 1}/{self._pages()} - {_LB_FOOTER}"
        )
        return embed, discord.File(io.BytesIO(png), "leaderboard.png")


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(EventsCog(bot))
