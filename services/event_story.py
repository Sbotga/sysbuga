"""fetch event story dialogue for the guess-the-event-story mode (english events only)

the eventStories master maps each event to its story assetbundle and the scenario id of each
episode. each scenario json holds TalkData, the dialogue. we pick a random english event with a
story, concatenate its episodes into one run, take a stretch of consecutive lines (which can
cross an episode boundary), and reveal more of that run per hint. the hint that fills the snippet
out to 10 lines also names the event type, and the final hint names its bonus attribute and unit.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from services.sbuga import SbugaClient

# stage 1 is the opening snippet (4 lines); the next hints extend it to 7 then 10 lines, the
# 10-line hint (stage 3) also names the event type, and the final hint (stage 4) names the bonus
# attribute and unit
STAGE_LINES = {1: 4, 2: 7, 3: 10}  # cumulative lines shown by each stage
LAST_LINE_STAGE = 3
TYPE_STAGE = 3  # the 10-line hint also names the event type
MAX_STAGE = 4  # final hint names the bonus attribute and unit
MAX_LINES = STAGE_LINES[LAST_LINE_STAGE]

# internal event type -> display name (world_bloom keeps its in-game name)
EVENT_TYPE_NAMES = {
    "marathon": "Marathon",
    "cheerful_carnival": "Cheerful Carnival",
    "world_bloom": "World Link",
}

# internal unit id -> display name
UNIT_NAMES = {
    "light_sound": "Leo/need",
    "idol": "MORE MORE JUMP!",
    "street": "Vivid BAD SQUAD",
    "theme_park": "Wonderlands×Showtime",
    "school_refusal": "25-ji, Nightcord de.",
    "piapro": "VIRTUAL SINGER",
}

_index: "dict[int, tuple[str, list[str]]] | None" = (
    None  # event id -> (assetbundle, scenario ids)
)
_lines_cache: dict[int, list[str]] = (
    {}
)  # event id -> every episode's lines concatenated in order (lazy)
_lock = asyncio.Lock()


def type_display(event_type: "str | None") -> str:
    """the event type shown on the third hint, title-cased"""
    if not event_type:
        return "Unknown"
    return EVENT_TYPE_NAMES.get(event_type, event_type.replace("_", " ").title())


def attribute_display(attribute: "str | None") -> str:
    """the bonus attribute shown on the fourth hint, title-cased"""
    return attribute.title() if attribute else "Unknown"


def unit_display(event_unit: "str | None") -> str:
    """the unit shown on the fourth hint, from the event's `unit` field ("none"/None is a mixed
    event and shows "Mixed")"""
    return UNIT_NAMES.get(event_unit, "Mixed") if event_unit else "Mixed"


def lines_for_stage(stage: int) -> int:
    """how many dialogue lines are visible by the given stage (line reveals stop at
    LAST_LINE_STAGE, later stages add facts instead)"""
    return STAGE_LINES[min(stage, LAST_LINE_STAGE)]


async def _load_index(client: "SbugaClient") -> "dict[int, tuple[str, list[str]]]":
    global _index
    if _index is not None:
        return _index
    async with _lock:
        if _index is not None:
            return _index
        raw = await client.get_master("eventStories", "en")
        idx: dict[int, tuple[str, list[str]]] = {}
        for es in raw:
            ab = es.get("assetbundleName")
            sids = [
                ep["scenarioId"]
                for ep in es.get("eventStoryEpisodes", [])
                if ep.get("scenarioId")
            ]
            if ab and sids:
                idx[int(es["eventId"])] = (ab, sids)
        _index = idx
    return _index


def _extract_lines(raw: bytes) -> list[str]:
    """dialogue lines of one scenario, in order (a consecutive snippet is cut from this). the
    speaker is bold and the body is markdown-escaped so dialogue punctuation can't format it
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out: list[str] = []
    for talk in data.get("TalkData", []):
        body = (talk.get("Body") or "").replace("\r", " ").replace("\n", " ").strip()
        if not body:
            continue
        body = discord.utils.escape_markdown(body)
        who = (talk.get("WindowDisplayName") or "").strip()
        out.append(f"**{who}:** {body}" if who else body)
    return out


async def _fetch_scenario(client: "SbugaClient", ab: str, sid: str) -> bytes:
    try:
        return await client.get_asset(f"event_story/{ab}/scenario/{sid}.json", "en")
    except Exception:
        return b""  # skip a missing/unreadable episode


async def _event_lines(client: "SbugaClient", event_id: int) -> list[str]:
    """every episode's dialogue lines concatenated in order, so a run can carry over from one
    chapter file into the next"""
    if event_id in _lines_cache:
        return _lines_cache[event_id]
    entry = (await _load_index(client)).get(event_id)
    if not entry:
        _lines_cache[event_id] = []
        return []
    ab, sids = entry
    raws = await asyncio.gather(*(_fetch_scenario(client, ab, sid) for sid in sids))
    lines: list[str] = []
    for raw in raws:
        lines.extend(_extract_lines(raw))
    _lines_cache[event_id] = lines
    return lines


async def eligible_event_ids(client: "SbugaClient") -> set[int]:
    """english event ids that have a story to draw dialogue from"""
    return set(await _load_index(client))


async def pick_snippet(client: "SbugaClient", event_id: int, rng) -> list[str]:
    """MAX_LINES consecutive dialogue lines from the event's story (crossing chapter files if
    one runs out), or [] if the whole story has fewer than MAX_LINES lines. the start never lands
    so close to the end that a full run isn't available"""
    lines = await _event_lines(client, event_id)
    if len(lines) < MAX_LINES:
        return []
    start = rng.randint(0, len(lines) - MAX_LINES)
    return lines[start : start + MAX_LINES]
