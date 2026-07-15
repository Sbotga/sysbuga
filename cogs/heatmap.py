from __future__ import annotations

import asyncio
import io
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from helpers import embeds
from helpers.autocompletes import autocompletes
from services import heatmap
from services.event_store import EVENT_REGIONS, iter_snapshots, read_current_event
from services.models import CurrentEventResponse

if TYPE_CHECKING:
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

    async def _render_heatmap(
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
    ) -> None:
        event_obj = self.bot.pjsk.get_event(data.event_id)  # type: ignore[union-attr,arg-type]
        event_name = event_obj.name if event_obj else "Event"
        embed = embeds.embed(
            title=f"{event_name} {label} Heatmap", color=discord.Color.purple()
        )
        embed.description = f"**Last Data Update:** <t:{int(data.updated)}:R>"
        embed.set_footer(text=resolved.upper())

        files: list[discord.File] = []
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
            graph_title = f"({resolved.upper()}) {event_name} {label} Heatmap"
            # a lazy generator - streamed + parsed inside the worker thread, so a full event's
            # snapshots never all sit in memory at once
            png = await asyncio.to_thread(
                heatmap.render_heatmap,
                timing.start_at,
                timing.aggregate_at,
                int(time.time() * 1000),
                graph_title,
                tz,
                tz_label,
                tz_overridden,
                iter_snapshots(resolved, data.event_id),
                mode,
                key,
            )
            files.append(discord.File(io.BytesIO(png), filename="heatmap.png"))
            embed.set_image(url="attachment://heatmap.png")

        await interaction.followup.send(embed=embed, files=files)

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
        await self._render_heatmap(
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
        await self._render_heatmap(
            interaction,
            resolved,
            tz,
            tz_label,
            tz_overridden,
            data,
            "user",
            int(user_id),
            f"({user_id})",
        )


async def setup(bot: SbugaBot) -> None:
    await bot.add_cog(HeatmapCog(bot))
