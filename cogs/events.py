from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from data.models import Event
from helpers import embeds, tools
from helpers.autocompletes import autocompletes
from helpers.emojis import emojis
from helpers.views import Paginator
from services.sbuga import SbugaError

if TYPE_CHECKING:
    from main import SbugaBot

EVENT_REGIONS = ["en", "jp", "tw", "kr"]
EVENT_TYPE_NAMES = {
    "marathon": "Marathon",
    "cheerful_carnival": "Cheerful Carnival",
    "world_bloom": "World Link",
}


class EventsCog(commands.Cog):
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

    def _event_embed(self, event: Event) -> discord.Embed:
        embed = embeds.embed(title=event.name, color=discord.Color.purple())
        lines = [
            f"**Type:** {EVENT_TYPE_NAMES.get(event.event_type or '', event.event_type)}",
            f"**ID:** `{event.id}`",
        ]
        if event.unit:
            lines.append(f"**Unit:** {event.unit}")
        if event.bonus_attribute:
            attr_emoji = emojis.attributes.get(event.bonus_attribute, "")
            lines.append(
                f"**Bonus Attribute:** {attr_emoji} {event.bonus_attribute}".replace(
                    "  ", " "
                )
            )
        if event.bonus_character_ids:
            names = []
            for cid in event.bonus_character_ids:
                char = self.bot.pjsk.get_character(cid)  # type: ignore[union-attr]
                if char:
                    from data.pjsk import character_display_name

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
            try:
                current = await self.bot.sbuga.get_current_event(resolved)  # type: ignore[union-attr]
                if current.event_id:
                    event_obj = self.bot.pjsk.get_event(current.event_id)  # type: ignore[union-attr]
            except SbugaError:
                pass

        if not event_obj:
            await interaction.followup.send(
                embed=embeds.error_embed("Couldn't find that event.")
            )
            return
        await interaction.followup.send(embed=self._event_embed(event_obj))

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
        try:
            data = await self.bot.sbuga.get_current_event(resolved)  # type: ignore[union-attr]
        except SbugaError as e:
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"Couldn't fetch the event: {e.detail or e.status}"
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

    @event.command(name="schedule", description="View upcoming and current events.")
    @app_commands.autocomplete(region=autocompletes.pjsk_region(EVENT_REGIONS))
    @app_commands.describe(region="Game server region.")
    async def schedule(
        self, interaction: discord.Interaction, region: str = "default"
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return
        events = sorted(self.bot.pjsk.events(), key=lambda e: e.start_at or 0, reverse=True)  # type: ignore[union-attr]
        if not events:
            await interaction.followup.send(
                embed=embeds.error_embed("No event data is available yet.")
            )
            return

        now = int(time.time() * 1000)
        upcoming = [e for e in events if (e.closed_at or e.aggregate_at or 0) >= now][
            -10:
        ]
        recent = [e for e in events if (e.closed_at or e.aggregate_at or 0) < now][:5]
        embed = embeds.embed(
            title=f"{resolved.upper()} Event Schedule", color=discord.Color.purple()
        )

        def fmt(e: Event) -> str:
            start = f"<t:{int(e.start_at / 1000)}:D>" if e.start_at else "?"
            return f"**{e.name}** ({EVENT_TYPE_NAMES.get(e.event_type or '', e.event_type)}) — {start}"

        if upcoming:
            embed.add_field(
                name="Current / Upcoming",
                value="\n".join(fmt(e) for e in reversed(upcoming)),
                inline=False,
            )
        if recent:
            embed.add_field(
                name="Recent", value="\n".join(fmt(e) for e in recent), inline=False
            )
        await interaction.followup.send(embed=embed)


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(EventsCog(bot))
