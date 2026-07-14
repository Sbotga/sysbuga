from __future__ import annotations

import asyncio
import io
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import Event
from data.pjsk import character_display_name
from data.search import preprocess
from helpers import converters, embeds, tools
from helpers.autocompletes import autocompletes
from helpers.emojis import emojis
from helpers.views import Paginator, SbugaView
from services import event_story, heatmap
from services.event_store import EVENT_REGIONS, iter_snapshots, read_current_event
from services.models import CurrentEventResponse

if TYPE_CHECKING:
    from main import SbugaBot

_FIELD_LIMIT = 1024


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
        return not await self.bot.user_data.channel_leaks_allowed(interaction.channel_id)  # type: ignore[union-attr,arg-type]

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

    @event.command(
        name="heatmap", description="View a tier's score heatmap for the current event."
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
    async def heatmap(
        self,
        interaction: discord.Interaction,
        tier: str,
        region: str = "default",
        timezone: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return

        if timezone and not heatmap.is_valid_tz(timezone):
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"`{timezone}` isn't a valid timezone. Use a common one "
                    f"({', '.join(heatmap.TIMEZONES)}) or an IANA name like `Europe/Paris`."
                )
            )
            return
        tz_name = timezone or await self.bot.user_data.get_settings(  # type: ignore[union-attr]
            interaction.user.id, "timezone"
        )
        tz, tz_label = heatmap.resolve_tz(tz_name)

        data = await read_current_event(resolved)
        if data is None or data.event_id is None:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active event right now.")
            )
            return

        parsed = _parse_tier(tier)
        if parsed is None or parsed not in set(_tier_options(data, _HEATMAP_MAX_TIER)):
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"`{tier}` isn't a valid tier. Pick a rank from 1-100 (e.g. `T50`)."
                )
            )
            return

        event_obj = self.bot.pjsk.get_event(data.event_id)  # type: ignore[union-attr]
        event_name = event_obj.name if event_obj else "Event"

        embed = embeds.embed(
            title=f"{event_name} T{parsed} Heatmap", color=discord.Color.purple()
        )
        embed.description = f"**Last Data Update:** <t:{int(data.updated)}:R>"
        embed.set_footer(text=resolved.upper())

        files: list[discord.File] = []

        # main image: the event activity heatmap for this event's whole run
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
        if timing and timing.start_at and timing.aggregate_at:
            graph_title = f"({resolved.upper()}) {event_name} T{parsed} Heatmap"
            # a lazy generator - the file is streamed + parsed inside the worker thread, so a full
            # event's snapshots never all sit in memory at once
            png = await asyncio.to_thread(
                heatmap.render_heatmap,
                timing.start_at,
                timing.aggregate_at,
                int(time.time() * 1000),
                graph_title,
                tz,
                tz_label,
                timezone is not None,  # hard override via the command option
                iter_snapshots(resolved, data.event_id),
                parsed,
            )
            files.append(discord.File(io.BytesIO(png), filename="heatmap.png"))
            embed.set_image(url="attachment://heatmap.png")

        await interaction.followup.send(embed=embed, files=files)

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

    @event.command(name="leaderboard", description="View the current event's top 100.")
    @app_commands.autocomplete(region=autocompletes.pjsk_region(EVENT_REGIONS))
    @app_commands.describe(region="Game server region.")
    async def leaderboard(
        self, interaction: discord.Interaction, region: str = "default"
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
        rankings = (data.top_100 or {}).get("rankings", [])
        if not data.event_id or not rankings:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active event right now.")
            )
            return

        event_obj = self.bot.pjsk.get_event(data.event_id)  # type: ignore[union-attr]
        title = event_obj.name if event_obj else f"Event {data.event_id}"
        pjsk_id = await self.bot.user_data.get_pjsk_id(interaction.user.id, resolved)  # type: ignore[union-attr]
        per_page = 20
        total_pages = max(1, (len(rankings) + per_page - 1) // per_page)

        def render(page: int) -> discord.Embed:
            start = (page - 1) * per_page
            embed = embeds.embed(
                title=f"{title} - Top 100 (Page {page})", color=discord.Color.purple()
            )
            lines = []
            for r in rankings[start : start + per_page]:
                you = "✅ " if r.get("userId") == pjsk_id else ""
                name = tools.escape_md(str(r.get("name", "?")).replace("\n", " "))
                lines.append(
                    f"{you}**#{r.get('rank')}** - {name} — `{r.get('score', 0):,}`"
                )
            embed.description = "\n".join(lines)
            embed.set_footer(
                text=f"{resolved.upper()} - {data.event_status or ''} - updated {round(time.time() - data.updated)}s ago"
            )
            return embed

        view = Paginator(render, total_pages, interaction.user.id)
        await interaction.followup.send(embed=render(1), view=view)
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
            self.bot.pjsk.region_events(region), key=lambda e: e.start_at or 0  # type: ignore[union-attr]
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


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(EventsCog(bot))
