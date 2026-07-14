from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import discord
import zstandard
from discord import app_commands
from discord.ext import commands, tasks

from data.models import Event
from data.pjsk import character_display_name
from data.search import preprocess
from helpers import converters, embeds, tools
from helpers.autocompletes import autocompletes
from helpers.emojis import emojis
from helpers.views import Paginator, SbugaView
from services import event_story
from services.models import CurrentEventResponse

if TYPE_CHECKING:
    from main import SbugaBot

_FIELD_LIMIT = 1024

# every minute a task force-refreshes each region's current-event data (top 100 + borders). the
# latest full snapshot lives in the cache for the leaderboard; meaningful snapshots are also saved
# indefinitely under event_saves/{region}/{event_id}/ for predictions, next to a rolling copy of
# every user's profile (kept because the live user data is later deleted). while the event runs we
# snapshot every minute; once it has ended we keep polling but only snapshot on a real change (a
# user added/removed or their event points moving - cosmetic profile edits don't count). a finished
# event's files are zstd-compressed once the next event starts, and kept forever.
_EVENT_CACHE_DIR = Path("data/cache")
_EVENT_SAVES_DIR = Path("event_saves")
_IN_PROGRESS_STATUSES = {"going", "counting"}
_ZSTD_LEVEL = 19

_event_mem: dict[str, tuple[float, CurrentEventResponse]] = (
    {}
)  # region -> (mtime, parsed)


@dataclass
class _SaveState:
    """in-memory bookkeeping for the event a region is currently writing, so the big files aren't
    re-read every minute"""

    event_id: int
    profiles: dict[
        str, dict
    ]  # userId -> latest profile, every user ever seen this event
    last_signature: str | None  # {userId: score} of the last saved "end" snapshot


_save_states: dict[str, _SaveState] = {}  # region -> _SaveState


def _event_cache_path(region: str) -> Path:
    return _EVENT_CACHE_DIR / f"current_event_{region}.json"


def _event_save_dir(region: str, event_id: int) -> Path:
    return _EVENT_SAVES_DIR / region / str(event_id)


def _snapshots_path(region: str, event_id: int) -> Path:
    return _event_save_dir(region, event_id) / "snapshots.jsonl"


def _profiles_path(region: str, event_id: int) -> Path:
    return _event_save_dir(region, event_id) / "profiles.json"


def _zst_path(path: Path) -> Path:
    return path.with_name(path.name + ".zst")


def _read_saved_text(path: Path) -> str | None:
    """the file's text, transparently decompressing the .zst archive if the event is already past.
    None when neither exists"""
    if path.exists():
        return path.read_text(encoding="utf-8")
    archive = _zst_path(path)
    if archive.exists():
        with archive.open("rb") as f:
            return zstandard.ZstdDecompressor().stream_reader(f).read().decode("utf-8")
    return None


async def read_current_event(region: str) -> "CurrentEventResponse | None":
    """the freshest current-event data the poller wrote for a region, or None if we have none yet.
    parses lazily and caches by mtime so repeated reads are cheap. shared by event commands
    """
    path = _event_cache_path(region)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _event_mem.get(region)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        data = CurrentEventResponse.model_validate_json(raw)
    except Exception:
        return None
    _event_mem[region] = (mtime, data)
    return data


async def read_event_history(region: str, event_id: int) -> list[dict]:
    """every saved snapshot for an event, oldest first. each line is
    {"ranking": RankingSnapshot, "border": BorderSnapshot} - the leaderboard + border (userIds,
    names, cheerful-carnival teams, world-bloom chapters) predictions extrapolate from. reads the
    live file, or the compressed archive once the event is past
    """
    raw = await asyncio.to_thread(_read_saved_text, _snapshots_path(region, event_id))
    if raw is None:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass  # skip a partial trailing line still being appended
    return out


async def read_event_profiles(region: str, event_id: int) -> dict[str, dict]:
    """every profile ever seen during an event (userId -> profile), preserved after the live data
    is deleted. reads the live file or its compressed archive"""
    raw = await asyncio.to_thread(_read_saved_text, _profiles_path(region, event_id))
    if raw is None:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _fsync_dir(path: Path) -> None:
    """flush a directory entry so a rename/unlink inside it survives a power cut. a no-op where the
    platform can't fsync a directory (e.g. windows)"""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    """write text durably: the new bytes and the rename are both fsynced, so a power loss leaves
    either the whole old file or the whole new one - never a torn one"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)  # atomic swap so a reader never sees a partial file
    _fsync_dir(path.parent)


def _write_current_event(region: str, text: str) -> None:
    _atomic_write(_event_cache_path(region), text)


def _needs_leading_newline(path: Path) -> bool:
    """True if the file exists and doesn't end in a newline - so a line torn by an earlier power
    loss can't merge with the one we're about to append"""
    try:
        with path.open("rb") as f:
            if f.seek(0, 2) == 0:
                return False
            f.seek(-1, 2)
            return f.read(1) != b"\n"
    except OSError:
        return False


def _ranking_entry(r: dict) -> dict:
    """the RankingEntry fields predictions need, dropping the card/profile/honor art the raw pjsk
    row carries (that lives in the profile copy instead)"""
    entry: dict = {
        "userId": r.get("userId"),
        "rank": r.get("rank"),
        "score": r.get("score"),
    }
    if r.get("name") is not None:
        entry["name"] = r["name"]
    if r.get("userCheerfulCarnival") is not None:
        entry["userCheerfulCarnival"] = r["userCheerfulCarnival"]
    return entry


def _wb_ranking(chapter: dict) -> dict:
    """a world-bloom chapter's UserWorldBloomRanking, with its rankings slimmed to lean rows"""
    out: dict = {
        "eventId": chapter.get("eventId"),
        "gameCharacterId": chapter.get("gameCharacterId"),
    }
    if chapter.get("rankings") is not None:
        out["rankings"] = [_ranking_entry(r) for r in chapter["rankings"]]
    out["userRankingStatus"] = chapter.get("userRankingStatus")
    out["isWorldBloomChapterAggregate"] = chapter.get("isWorldBloomChapterAggregate")
    return out


def _wb_border(chapter: dict) -> dict:
    """a world-bloom chapter's UserWorldBloomChapterRankingBorder, with lean borderRankings"""
    return {
        "borderRankings": [
            _ranking_entry(r) for r in chapter.get("borderRankings", [])
        ],
        "eventId": chapter.get("eventId"),
        "gameCharacterId": chapter.get("gameCharacterId"),
        "isWorldBloomChapterAggregate": chapter.get("isWorldBloomChapterAggregate"),
    }


def _ranking_snapshot(
    event_id: int, created_at: str, final: bool, top_100: dict | None
) -> dict:
    top_100 = top_100 or {}
    snap: dict = {
        "eventId": event_id,
        "createdAt": created_at,
        "rankings": [_ranking_entry(r) for r in top_100.get("rankings", [])],
    }
    if final:
        snap["final"] = True
    if top_100.get("isEventAggregate") is not None:
        snap["isEventAggregate"] = top_100["isEventAggregate"]
    if top_100.get("userWorldBloomChapterRankings") is not None:
        snap["userWorldBloomChapterRankings"] = [
            _wb_ranking(chapter) for chapter in top_100["userWorldBloomChapterRankings"]
        ]
    return snap


def _border_snapshot(
    event_id: int, created_at: str, final: bool, border: dict | None
) -> dict:
    border = border or {}
    snap: dict = {
        "eventId": event_id,
        "createdAt": created_at,
        "borderRankings": [_ranking_entry(r) for r in border.get("borderRankings", [])],
    }
    if final:
        snap["final"] = True
    if border.get("isEventAggregate") is not None:
        snap["isEventAggregate"] = border["isEventAggregate"]
    if border.get("userWorldBloomChapterRankingBorders") is not None:
        snap["userWorldBloomChapterRankingBorders"] = [
            _wb_border(chapter)
            for chapter in border["userWorldBloomChapterRankingBorders"]
        ]
    return snap


def _iter_entries(data: CurrentEventResponse) -> Iterator[dict]:
    """every ranking/border row in a poll - top 100, borders, and world-bloom chapter rows"""
    top = data.top_100 or {}
    for row in top.get("rankings", []):
        yield row
    for chapter in top.get("userWorldBloomChapterRankings") or []:
        for row in chapter.get("rankings", []):
            yield row
    border = data.border or {}
    for row in border.get("borderRankings", []):
        yield row
    for chapter in border.get("userWorldBloomChapterRankingBorders") or []:
        for row in chapter.get("borderRankings", []):
            yield row


def _signature(data: CurrentEventResponse) -> str:
    """a fingerprint of {userId: score} across the whole poll. it moves when a user is added,
    removed, or their event points change - but not when only their profile (name/card) does
    """
    pairs = sorted(
        f"{row.get('userId')}:{row.get('score')}" for row in _iter_entries(data)
    )
    return hashlib.sha1("\n".join(pairs).encode("utf-8")).hexdigest()


def _extract_profiles(data: CurrentEventResponse) -> dict[str, dict]:
    """userId -> the row's profile data (everything but its volatile rank/score)"""
    profiles: dict[str, dict] = {}
    for row in _iter_entries(data):
        user_id = row.get("userId")
        if user_id is None:
            continue
        profiles[str(user_id)] = {
            k: v for k, v in row.items() if k not in ("rank", "score")
        }
    return profiles


def _store_current_event(region: str, data: CurrentEventResponse) -> None:
    """persist one poll (blocking I/O, off the event loop). always refresh the leaderboard's latest
    snapshot; then, for the live event, update the profile copy and append a snapshot when it's
    worth keeping - every minute while the event runs, only on a real change once it has ended
    """
    _write_current_event(region, data.model_dump_json())

    event_id = data.event_id
    if event_id is None:
        return  # between events with nothing to serve

    state = _save_states.get(region)
    if state is None or state.event_id != event_id:
        state = _SaveState(
            event_id=event_id,
            profiles=_load_profiles(region, event_id),
            last_signature=None,
        )
        _save_states[region] = state

    # keep one rolling copy of every user's profile, updated to the latest we've seen. this
    # accumulates users forever - they stay even after dropping off the leaderboard
    profiles_changed = False
    for user_id, profile in _extract_profiles(data).items():
        if state.profiles.get(user_id) != profile:
            state.profiles[user_id] = profile
            profiles_changed = True
    if profiles_changed:
        _atomic_write(
            _profiles_path(region, event_id),
            json.dumps(state.profiles, ensure_ascii=False),
        )

    status = data.event_status
    if status in _IN_PROGRESS_STATUSES:
        pass  # snapshot every minute while the event is live
    elif status == "end":
        signature = _signature(data)
        if state.last_signature is not None and signature == state.last_signature:
            return  # nothing meaningful changed since the last saved snapshot
        state.last_signature = signature
    else:
        return  # unknown status - not serving an event

    created_at = datetime.fromtimestamp(data.updated, tz=timezone.utc).isoformat()
    line = {
        "ranking": _ranking_snapshot(
            event_id, created_at, status == "end", data.top_100
        ),
        "border": _border_snapshot(event_id, created_at, status == "end", data.border),
    }
    path = _snapshots_path(region, event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n" if _needs_leading_newline(path) else ""
    with path.open("a", encoding="utf-8") as f:
        f.write(prefix + json.dumps(line, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())  # the snapshot is on disk before we move on


def _load_profiles(region: str, event_id: int) -> dict[str, dict]:
    text = _read_saved_text(_profiles_path(region, event_id))
    if text is None:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _compress_file(path: Path) -> None:
    """zstd-compress path to path.zst, then remove the original - but only once the archive is
    safely written, so a crash never loses the source"""
    if not path.exists():
        return
    archive = _zst_path(path)
    if archive.exists():
        path.unlink(
            missing_ok=True
        )  # a prior run wrote the archive but not the cleanup
        return
    tmp = archive.with_name(archive.name + ".tmp")
    compressor = zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
    with path.open("rb") as src, tmp.open("wb") as dst:
        compressor.copy_stream(src, dst)
        dst.flush()
        os.fsync(dst.fileno())  # the archive is fully on disk before we swap it in
    tmp.replace(archive)  # the archive now exists in full
    _fsync_dir(archive.parent)
    path.unlink()  # only now is it safe to drop the source
    _fsync_dir(path.parent)


def _compress_event_dir(event_dir: Path) -> None:
    """archive a finished event's snapshot + profile files"""
    _compress_file(event_dir / "snapshots.jsonl")
    _compress_file(event_dir / "profiles.json")


def _compress_stale_event_saves() -> None:
    """compress every past event across all regions, leaving each region's newest (current) event
    untouched. safe to re-run - it finishes any archive a crash left half done"""
    if not _EVENT_SAVES_DIR.exists():
        return
    for region_dir in _EVENT_SAVES_DIR.iterdir():
        if not region_dir.is_dir():
            continue
        event_ids = [
            int(child.name)
            for child in region_dir.iterdir()
            if child.is_dir() and child.name.isdigit()
        ]
        if not event_ids:
            continue
        current = max(event_ids)
        for event_id in event_ids:
            if event_id != current:
                _compress_event_dir(region_dir / str(event_id))


def _alias_field(values: list[str]) -> str:
    """comma-joined aliases trimmed to fit an embed field"""
    if not values:
        return "*None*"
    text = ", ".join(values)
    if len(text) + 2 > _FIELD_LIMIT:
        text = text[: _FIELD_LIMIT - 6].rsplit(", ", 1)[0] + ", …"
    return f"`{text}`"


EVENT_REGIONS = ["en", "jp", "tw", "kr"]
EVENT_TYPE_NAMES = {
    "marathon": "Marathon",
    "cheerful_carnival": "Cheerful Carnival",
    "world_bloom": "World Link",
}


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


def _tier_options(data: CurrentEventResponse) -> list[int]:
    """valid heatmap tiers in autocomplete order: the border tiers first, then the top-100 ranks
    that aren't already a border"""
    borders = _border_ranks(data)
    border_set = set(borders)
    return borders + [r for r in range(1, 101) if r not in border_set]


async def _tier_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    region = getattr(interaction.namespace, "region", None)
    if region not in EVENT_REGIONS:
        region = "en"  # border tiers are the same set across regions; just need any live event
    data = await read_current_event(region)
    options = _tier_options(data) if data and data.event_id else list(range(1, 101))
    query = current.strip().lstrip("tT").strip()
    if query:
        options = [tier for tier in options if str(tier).startswith(query)]
    return [
        app_commands.Choice(name=f"T{tier}", value=str(tier)) for tier in options[:25]
    ]


def _entry_at_tier(data: CurrentEventResponse, tier: int) -> dict | None:
    """the leaderboard/border row sitting at a given rank, or None"""
    top = (data.top_100 or {}).get("rankings", [])
    border = (data.border or {}).get("borderRankings", [])
    for entry in (*top, *border):
        if entry.get("rank") == tier:
            return entry
    return None


async def _leader_card_image(bot: SbugaBot, user_card: dict) -> bytes | None:
    """render a player's profile leader card (from their userCard) as a small card image, or None
    if we can't build it (missing card data, art, or the sekai_images package)"""
    card_id = user_card.get("cardId")
    if card_id is None:
        return None
    card = bot.pjsk.get_card(card_id)  # type: ignore[union-attr]
    if card is None or not card.attr or not card.card_rarity_type:
        return None
    trained = user_card.get("defaultImage") == "special_training"
    thumb_url = (
        card.thumbnail_url_trained
        if trained and card.thumbnail_url_trained
        else card.thumbnail_url_normal
    )
    if not thumb_url:
        return None
    try:
        from sekai_images import LeaderCardImage
        from sekai_images.generators.leader_card import CardData

        card_data = CardData(
            level=user_card.get("level"),
            mastery_rank=int(user_card.get("masterRank") or 0),
            special_training_status=user_card.get("specialTrainingStatus")
            or "not_doing",
            card_rarity_type=card.card_rarity_type,
            attr=card.attr,
            member_image=thumb_url,  # load_image fetches the url (in the worker thread)
        )
        return await asyncio.to_thread(LeaderCardImage(card_data).create)
    except Exception:
        return None


class EventsCog(commands.Cog):
    def __init__(self, bot: SbugaBot) -> None:
        self.bot = bot
        self._vlive_cache: dict[str, tuple[float, list]] = (
            {}
        )  # region -> (fetched_at, data)
        self._last_polled_event: dict[str, int] = {}  # region -> last seen event id
        self._did_startup_sweep = False
        self.poll_current_event_task.start()

    async def cog_unload(self) -> None:
        self.poll_current_event_task.cancel()

    @tasks.loop(seconds=60)
    async def poll_current_event_task(self) -> None:
        """force-fetch (cache-bypassing) each region's current event + borders into its file"""
        try:
            await asyncio.gather(
                *(self._poll_current_event(r) for r in EVENT_REGIONS),
                return_exceptions=True,
            )
            # after the first round every current event's folder exists on disk, so a sweep can
            # safely compress everything older that a previous run left uncompressed (a crash, or a
            # restart mid event-transition) without touching a live event
            if not self._did_startup_sweep:
                self._did_startup_sweep = True
                asyncio.create_task(asyncio.to_thread(_compress_stale_event_saves))
        except (
            Exception
        ) as exc:  # this loop is important - never let a stray error stop it
            self.bot.warn(f"event poll iteration failed: {exc!r}")  # type: ignore[union-attr]

    @poll_current_event_task.before_loop
    async def _before_poll(self) -> None:
        # this data matters, so start pulling it the moment the api client exists (during setup,
        # before the gateway is even connected) instead of waiting on a slow discord login
        while self.bot.sbuga is None:
            await asyncio.sleep(0.2)

    @poll_current_event_task.error
    async def _poll_error(self, exc: Exception) -> None:
        # last-resort backstop: if the loop ever dies, log it and bring it right back
        self.bot.traceback(exc)  # type: ignore[union-attr]
        await asyncio.sleep(5)
        if not self.poll_current_event_task.is_running():
            self.poll_current_event_task.start()

    async def _poll_current_event(self, region: str) -> None:
        try:
            data = await self.bot.sbuga.get_current_event(region, fresh=True)  # type: ignore[union-attr]
        except Exception:
            return  # keep the last good file for this region
        try:
            await asyncio.to_thread(_store_current_event, region, data)
        except Exception as exc:
            # surface storage failures instead of silently dropping snapshots
            self.bot.warn(f"[{region}] storing event {data.event_id} failed: {exc!r}")  # type: ignore[union-attr]

        event_id = data.event_id
        if event_id is None:
            return
        previous = self._last_polled_event.get(region)
        self._last_polled_event[region] = event_id
        # a new event started - compress the previous one's files in the background so the poll
        # loop never blocks on a long high-level zstd pass
        if previous is not None and previous != event_id:
            asyncio.create_task(self._compress_past_event(region, previous))

    async def _compress_past_event(self, region: str, event_id: int) -> None:
        try:
            await asyncio.to_thread(
                _compress_event_dir, _event_save_dir(region, event_id)
            )
        except Exception:
            pass  # the startup sweep / retro script will finish it later

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
        tier=_tier_autocomplete, region=autocompletes.pjsk_region(EVENT_REGIONS)
    )
    @app_commands.describe(
        tier="Rank 1-100 or a border tier (e.g. 100, T100, T1000).",
        region="Game server region.",
    )
    async def heatmap(
        self,
        interaction: discord.Interaction,
        tier: str,
        region: str = "default",
    ) -> None:
        await interaction.response.defer(thinking=True)
        resolved = await self._resolve_region(interaction, region)
        if resolved is None:
            return

        data = await read_current_event(resolved)
        if data is None or data.event_id is None:
            await interaction.followup.send(
                embed=embeds.error_embed("There's no active event right now.")
            )
            return

        parsed = _parse_tier(tier)
        if parsed is None or parsed not in set(_tier_options(data)):
            await interaction.followup.send(
                embed=embeds.error_embed(
                    f"`{tier}` isn't a valid tier. Pick a rank from 1-100 or a border "
                    "tier (e.g. `T1000`)."
                )
            )
            return

        event_obj = self.bot.pjsk.get_event(data.event_id)  # type: ignore[union-attr]
        event_name = event_obj.name if event_obj else "Event"
        embed = embeds.embed(
            title=f"{event_name} T{parsed} Heatmap", color=discord.Color.purple()
        )
        embed.set_footer(text=resolved.upper())

        # thumbnail: the tier player's profile leader card
        file: discord.File | None = None
        entry = _entry_at_tier(data, parsed)
        user_card = entry.get("userCard") if entry else None
        if user_card:
            image = await _leader_card_image(self.bot, user_card)
            if image:
                file = discord.File(io.BytesIO(image), filename="leader_card.png")
                embed.set_thumbnail(url="attachment://leader_card.png")

        if file is not None:
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)

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
