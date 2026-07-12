"""fetch event story dialogue for the guess-the-event-story mode (english events only)

the eventStories master maps each event to its story assetbundle, the scenario id of each episode,
and an outline (the event description). each scenario json holds TalkData, the dialogue. we pick a
random english event with a story, concatenate its episodes into one run, take a stretch of
consecutive lines (which can cross an episode boundary), and reveal more of that run per hint. the
hint that fills the snippet out to 10 lines also names the event type, the next names its bonus
attribute and unit (from eventStoryUnits), and the last shows the event description.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from services.sbuga import SbugaClient

# stage 1 is the opening snippet (4 lines); the next hints extend it to 7 then 10 lines, the
# 10-line hint (stage 3) also names the event type, stage 4 names the bonus attribute and unit,
# and stage 5 shows the event description
STAGE_LINES = {1: 4, 2: 7, 3: 10}  # cumulative lines shown by each stage
LAST_LINE_STAGE = 3
TYPE_STAGE = 3  # the 10-line hint also names the event type
FACTS_STAGE = 4  # bonus attribute and unit
DESC_STAGE = 5  # event description
MAX_STAGE = 5
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
_outlines: dict[int, str] = (
    {}
)  # event id -> outline/description (filled with the index)
_units: "dict[int, list[tuple[str, str]]] | None" = (
    None  # event id -> [(unit, relation)] from eventStoryUnits
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


async def _load_units(client: "SbugaClient") -> "dict[int, list[tuple[str, str]]]":
    """eventStoryUnits maps each event's story to its featured units and whether each is the main
    unit or a sub unit"""
    global _units
    if _units is not None:
        return _units
    async with _lock:
        if _units is not None:
            return _units
        try:
            raw = await client.get_master("eventStoryUnits", "en")
        except Exception:
            raw = []  # backend may not serve this file yet; fall back to "Mixed"
        units: dict[int, list[tuple[str, str]]] = {}
        for row in sorted(raw, key=lambda r: r.get("seq", 0)):
            unit = row.get("unit")
            if unit:
                units.setdefault(int(row["eventStoryId"]), []).append(
                    (unit, row.get("eventStoryUnitRelation", "sub"))
                )
        _units = units
    return _units


async def unit_display(client: "SbugaClient", event_id: int) -> str:
    """the featured unit(s) shown on the fourth hint, from eventStoryUnits. a single main unit is
    shown alone; multiple mains are listed and a trailing "Mixed" stands in for any sub units;
    with no main unit at all the event is just "Mixed"."""
    rows = (await _load_units(client)).get(event_id, [])
    mains = [UNIT_NAMES.get(u, u) for u, rel in rows if rel == "main"]
    has_subs = any(rel != "main" for _, rel in rows)
    if not mains:
        return "Mixed"
    if len(mains) == 1:
        return mains[0]
    joined = ", ".join(mains)
    return f"{joined} + Mixed" if has_subs else joined


async def event_outline(client: "SbugaClient", event_id: int) -> str:
    """the event's description/outline, shown on the final hint"""
    await _load_index(client)
    return _outlines.get(event_id, "")


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
            eid = int(es["eventId"])
            _outlines[eid] = (es.get("outline") or "").strip()
            ab = es.get("assetbundleName")
            sids = [
                ep["scenarioId"]
                for ep in es.get("eventStoryEpisodes", [])
                if ep.get("scenarioId")
            ]
            if ab and sids:
                idx[eid] = (ab, sids)
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
