"""Shared event-data storage. The standalone minutely worker (run_event_worker.py) writes here;
the bot's event commands read from here. Deliberately free of discord / heavy deps so the worker
process starts fast and is unaffected by bot maintenance.

Every minute the worker force-refreshes each region's current-event data (top 100 + borders). The
latest full snapshot lives in the cache for the leaderboard; meaningful snapshots are also saved
indefinitely under event_saves/{region}/{event_id}/ for predictions, next to a rolling copy of
every user's profile (kept because the live user data is later deleted). While the event runs we
snapshot every minute; once it has ended we keep polling but only snapshot on a real change (a user
added/removed or their event points moving - cosmetic profile edits don't count). A finished
event's files are zstd-compressed once the next event starts, and kept forever.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import orjson
import zstandard

from services.models import CurrentEventResponse

EVENT_REGIONS = ["en", "jp", "tw", "kr"]

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


def event_save_dir(region: str, event_id: int) -> Path:
    return _EVENT_SAVES_DIR / region / str(event_id)


def _snapshots_path(region: str, event_id: int) -> Path:
    return event_save_dir(region, event_id) / "snapshots.jsonl"


def _profiles_path(region: str, event_id: int) -> Path:
    return event_save_dir(region, event_id) / "profiles.json"


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
    """the freshest current-event data the worker wrote for a region, or None if we have none yet.
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


def iter_snapshots(region: str, event_id: int) -> Iterator[dict]:
    """yield each saved snapshot dict one at a time, streaming the live file or the .zst archive so
    a full event's history (can be 160MB+) never sits in memory at once. each is
    {"ranking": RankingSnapshot, "border": BorderSnapshot}. blocking - run off the event loop
    """
    path = _snapshots_path(region, event_id)
    if path.exists():
        with path.open("rb") as f:
            for line in f:
                if line.strip():
                    try:
                        yield orjson.loads(line)
                    except orjson.JSONDecodeError:
                        pass  # a partial trailing line still being appended
        return
    archive = _zst_path(path)
    if archive.exists():
        with archive.open("rb") as fh:
            reader = io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(fh), encoding="utf-8"
            )
            for line in reader:
                if line.strip():
                    try:
                        yield orjson.loads(line)
                    except orjson.JSONDecodeError:
                        pass


async def read_event_history(region: str, event_id: int) -> list[dict]:
    """every saved snapshot for an event as a list, oldest first (see iter_snapshots for the
    streaming form the heatmap uses). reads the live file, or the archive once the event is past
    """
    return await asyncio.to_thread(lambda: list(iter_snapshots(region, event_id)))


async def read_event_profiles(region: str, event_id: int) -> dict[str, dict]:
    """every profile ever seen during an event (userId -> profile), preserved after the live data
    is deleted. reads the live file or its compressed archive"""
    raw = await asyncio.to_thread(_read_saved_text, _profiles_path(region, event_id))
    if raw is None:
        return {}
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
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


def _load_profiles(region: str, event_id: int) -> dict[str, dict]:
    text = _read_saved_text(_profiles_path(region, event_id))
    if text is None:
        return {}
    try:
        return orjson.loads(text)
    except orjson.JSONDecodeError:
        return {}


def store_current_event(region: str, data: CurrentEventResponse) -> None:
    """persist one poll (blocking I/O, run off the event loop). always refresh the leaderboard's
    latest snapshot; then, for the live event, update the profile copy and append a snapshot when
    it's worth keeping - every minute while the event runs, only on a real change once it has ended
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


def compress_event_dir(event_dir: Path) -> None:
    """archive a finished event's snapshot + profile files"""
    _compress_file(event_dir / "snapshots.jsonl")
    _compress_file(event_dir / "profiles.json")


def compress_stale_event_saves() -> None:
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
                compress_event_dir(region_dir / str(event_id))
