"""fetch event story dialogue for the guess-the-event-story mode (english events only)

the eventStories master maps each event to its story assetbundle and the scenario id of each
episode. each scenario json holds TalkData, the dialogue. we pick a random english event with a
story, take a run of consecutive dialogue lines from one of its episodes, and reveal more of that
run per hint (the final hint also names the event's unit, since event types are mixed).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.sbuga import SbugaClient

# stage -> how many consecutive lines are shown; stage 4 keeps 7 and adds the event unit
STAGE_LINES = {1: 3, 2: 5, 3: 7, 4: 7}
MAX_STAGE = 4
MAX_LINES = STAGE_LINES[MAX_STAGE]
MIN_LINES = STAGE_LINES[1]

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
_scenarios_cache: dict[int, list[list[str]]] = (
    {}
)  # event id -> lines per episode (lazy)
_lock = asyncio.Lock()


def unit_display(event_unit: "str | None") -> str:
    """the unit shown on the final hint, straight from the event's `unit` field. "none"/None is
    a mixed event, so it shows "Mixed"."""
    return UNIT_NAMES.get(event_unit, "Mixed") if event_unit else "Mixed"


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
    """dialogue lines of one scenario, in order (a consecutive snippet is cut from this)"""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out: list[str] = []
    for talk in data.get("TalkData", []):
        body = (talk.get("Body") or "").replace("\r", " ").replace("\n", " ").strip()
        if not body:
            continue
        who = (talk.get("WindowDisplayName") or "").strip()
        out.append(f"**{who}:** {body}" if who else body)
    return out


async def _event_scenarios(client: "SbugaClient", event_id: int) -> list[list[str]]:
    """each episode's dialogue lines, kept separate so a snippet stays within one scene"""
    if event_id in _scenarios_cache:
        return _scenarios_cache[event_id]
    entry = (await _load_index(client)).get(event_id)
    if not entry:
        return []
    ab, sids = entry
    scenarios: list[list[str]] = []
    for sid in sids:
        try:
            raw = await client.get_asset(f"event_story/{ab}/scenario/{sid}.json", "en")
        except Exception:
            continue  # skip a missing/unreadable episode
        lines = _extract_lines(raw)
        if lines:
            scenarios.append(lines)
    _scenarios_cache[event_id] = scenarios
    return scenarios


async def eligible_event_ids(client: "SbugaClient") -> set[int]:
    """english event ids that have a story to draw dialogue from"""
    return set(await _load_index(client))


async def pick_snippet(client: "SbugaClient", event_id: int, rng) -> list[str]:
    """up to MAX_LINES consecutive dialogue lines from one of the event's episodes, or [] if no
    episode has at least MIN_LINES lines"""
    scenarios = [
        s for s in await _event_scenarios(client, event_id) if len(s) >= MIN_LINES
    ]
    if not scenarios:
        return []
    # prefer an episode long enough for the whole run, else take the best we have
    long_enough = [s for s in scenarios if len(s) >= MAX_LINES] or scenarios
    scenario = rng.choice(long_enough)
    if len(scenario) <= MAX_LINES:
        return list(scenario)
    start = rng.randint(0, len(scenario) - MAX_LINES)
    return scenario[start : start + MAX_LINES]
