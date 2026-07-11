"""pre-render chart-guess clips to disk so a round can grab one instantly

a background worker keeps up to chart_clip.TARGETS[type] clips per chart type on disk and
renders at the cached higher quality whenever a pool is below target and no live render is
waiting. a round pops one instantly, or renders on the fly (smaller and faster) if the pool
is empty.

each entry is a folder with chart.mp4 answer.mp4 and meta.json, and only counts once meta's
"complete" flag is written and the folder is atomically renamed into place. multi-process
safe: store() publishes with an atomic rename, pop() claims one the same way, and
cleanup_invalid() drops anything a crash left half-generated.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from services import chart_clip, chart_preview

if TYPE_CHECKING:
    from data.pjsk import PJSKData

CACHE_ROOT = Path("cache/chart_clips")
TYPES = tuple(chart_clip.TARGETS)  # chart, chart_append, chart_expert
_POLL_SECONDS = 10.0
# how many different chart clips the filler renders at once
# each still renders its chart and answer one after another so it holds one nxsk session
# needs a spare session in chart_preview.MAX_SESSIONS for on-the-fly rounds
FILL_CONCURRENCY = 2

_task: "asyncio.Task | None" = None
_pjsk: "PJSKData | None" = None
_running = False
# on-the-fly renders in flight
# the filler pauses while any are waiting so a live round doesn't queue behind fill work
_live_pending = 0
# this-process generation stats since the filler runs in the bot
# clip_seconds sums each render's own duration
# wall_seconds sums the batch wall time which is lower per clip thanks to concurrency
_stats = {"generated": 0, "clip_seconds": 0.0, "wall_seconds": 0.0}


def stats() -> dict[str, Any]:
    n = _stats["generated"]
    return {
        "generated": n,
        "avg_one_clip": (
            (_stats["clip_seconds"] / n) if n else 0.0
        ),  # one render's duration
        "avg_per_clip": (_stats["wall_seconds"] / n) if n else 0.0,  # concurrency-aware
        "pools": {t: (count(t), chart_clip.TARGETS[t]) for t in TYPES},
    }


def _dir(gtype: str) -> Path:
    return CACHE_ROOT / gtype


# each entry is a folder with chart.mp4 answer.mp4 and meta.json
# the "_" prefix hides an entry still being written (_tmp_) or mid-pop (_claim_) from count and pop
def _entries(d: Path) -> "list[Path]":
    return [e for e in d.iterdir() if e.is_dir() and not e.name.startswith("_")]


def _valid(entry: Path) -> "dict[str, Any] | None":
    """the entry's meta if it's fully generated with both clips present and json flagged
    complete, else none. meta.json is written last so its flag means done"""
    try:
        meta = json.loads((entry / "meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not meta.get("complete"):
        return None
    if not (entry / "chart.mp4").exists() or not (entry / "answer.mp4").exists():
        return None
    return meta


def cleanup_invalid() -> None:
    """delete entries a crash or shutdown left half-generated plus any _tmp_ or _claim_ leftovers
    run once at startup in the filler process before the worker or any pop"""
    for gtype in TYPES:
        d = _dir(gtype)
        if not d.exists():
            continue
        for entry in d.iterdir():
            if not entry.is_dir():
                entry.unlink(missing_ok=True)
            elif entry.name.startswith("_") or _valid(entry) is None:
                shutil.rmtree(entry, ignore_errors=True)


def count(gtype: str) -> int:
    d = _dir(gtype)
    return len(_entries(d)) if d.exists() else 0


def pop(gtype: str) -> "tuple[bytes, bytes | None, dict[str, Any]] | None":
    """atomically claim one cached entry, returns (chart mp4, answer mp4, meta)"""
    d = _dir(gtype)
    if not d.exists():
        return None
    for entry in sorted(_entries(d)):
        claim = d / f"_claim_{entry.name}"
        try:
            os.rename(entry, claim)  # atomic and whoever wins the rename owns the entry
        except OSError:
            continue  # another process took it
        meta = _valid(claim)
        try:
            if meta is None:
                raise OSError
            chart = (claim / "chart.mp4").read_bytes()
            answer = (claim / "answer.mp4").read_bytes()
        except OSError:
            shutil.rmtree(claim, ignore_errors=True)
            continue
        shutil.rmtree(claim, ignore_errors=True)
        return chart, answer, meta
    return None


def _store(
    gtype: str, chart_mp4: bytes, answer_mp4: bytes, meta: dict[str, Any]
) -> None:
    d = _dir(gtype)
    d.mkdir(parents=True, exist_ok=True)
    uid = secrets.token_hex(8)
    tmp = d / f"_tmp_{uid}"
    tmp.mkdir()
    (tmp / "chart.mp4").write_bytes(chart_mp4)
    (tmp / "answer.mp4").write_bytes(answer_mp4)
    # meta.json written last with the flag that signals the whole entry is done
    (tmp / "meta.json").write_text(json.dumps({**meta, "complete": True}), "utf-8")
    os.rename(tmp, d / uid)  # atomic publish


class live_priority:
    """mark an on-the-fly render in progress so the filler yields to it"""

    def __enter__(self) -> "live_priority":
        global _live_pending
        _live_pending += 1
        return self

    def __exit__(self, *_: Any) -> None:
        global _live_pending
        _live_pending -= 1


# the reveal audio best available in this order
_VOCAL_PRIORITY = ("instrumental", "sekai", "virtual_singer")


def _pick_bgm_url(music: Any) -> str | None:
    # trimmed silence-removed audio so it lines up with the chart's time coordinate
    def url(v: Any) -> str | None:
        return v.bgm_nosil_url or v.bgm_url

    by_type: dict[str, str] = {}
    for v in music.vocals:
        u = url(v)
        if u and v.vocal_type not in by_type:
            by_type[v.vocal_type] = u
    for vtype in _VOCAL_PRIORITY:
        if vtype in by_type:
            return by_type[vtype]
    return next((u for v in music.vocals if (u := url(v))), None)


def _png_jacket_url(url: str) -> str:
    # nxsk's image loader can't decode webp so use the .png variant beside it on r2
    return url.rsplit(".", 1)[0] + ".png"


async def _fetch_bytes(url: str) -> bytes | None:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read() if resp.status == 200 else None


async def _render_one(gtype: str) -> bool:
    assert _pjsk is not None
    want = chart_clip.DIFFICULTIES[gtype]
    music = chart_clip.weighted_chart_music(_pjsk.musics(), want)
    if music is None:
        return False
    region = next(
        (r for r in _pjsk.regions_for_music(music.id) if r in ("en", "jp")), "en"
    )
    sus_bytes = await _fetch_bytes(_pjsk.chart_source_url(music.id, want, region))
    if not sus_bytes:
        return False
    window = await asyncio.get_running_loop().run_in_executor(
        None, chart_clip.cut_window, sus_bytes.decode("utf-8", "replace")
    )
    if window is None:
        return False

    # an entry needs both the chart clip and the reveal video
    # skip the song if the audio or jacket isn't available since the reveal is mandatory
    bgm_url = _pick_bgm_url(music)
    if not bgm_url or not music.jacket_url:
        return False
    bgm = await _fetch_bytes(bgm_url)
    jacket = await _fetch_bytes(_png_jacket_url(music.jacket_url))
    if not bgm or not jacket:
        return False

    # roll easter eggs once so the guess clip and its reveal share the same overrides
    egg_settings, egg_descriptions = chart_clip.roll_easter_eggs()

    # render the two clips one after another so each filler slot only holds one nxsk session
    try:
        chart_mp4 = await chart_clip.render_leveldata(
            window,
            height=chart_clip.CACHED_HEIGHT,
            fps=chart_clip.CACHED_FPS,
            extra_settings=egg_settings,
        )
        answer_mp4 = await chart_clip.render_answer_video(
            window,
            jacket,
            bgm,
            height=chart_clip.CACHED_HEIGHT,
            fps=chart_clip.CACHED_FPS,
            extra_settings=egg_settings,
        )
    except chart_clip.ChartClipError:
        return False

    _store(
        gtype,
        chart_mp4,
        answer_mp4,
        {"music_id": music.id, "diff": want, "eggs": egg_descriptions},
    )
    return True


async def _timed_render(gtype: str) -> tuple[bool, float]:
    started = time.perf_counter()
    try:
        ok = await _render_one(gtype)
    except Exception:
        ok = False
    return ok, time.perf_counter() - started


def _fill_targets(n: int) -> list[str]:
    """up to n pools to render this batch biased to the least-rendered so the pools grow
    together, the same pool can repeat when only it is below target"""
    counts = {t: count(t) for t in TYPES if count(t) < chart_clip.TARGETS[t]}
    targets: list[str] = []
    for _ in range(n):
        if not counts:
            break
        fewest = min(counts.values())
        pool = random.choice([t for t in counts if counts[t] == fewest])
        targets.append(pool)
        counts[pool] += 1  # pretend it's rendered so the next pick balances
    return targets


async def _worker() -> None:
    while _running:
        if not chart_preview.available() or _live_pending:
            await asyncio.sleep(_POLL_SECONDS)
            continue
        targets = _fill_targets(FILL_CONCURRENCY)
        if not targets:
            await asyncio.sleep(_POLL_SECONDS)  # all pools full
            continue
        # render FILL_CONCURRENCY different clips at once each with its own nxsk session
        batch_start = time.perf_counter()
        results = await asyncio.gather(*[_timed_render(t) for t in targets])
        batch_wall = time.perf_counter() - batch_start
        made = 0
        for ok, dur in results:
            if ok:
                made += 1
                _stats["generated"] += 1
                _stats["clip_seconds"] += dur  # one render's own duration
        if made:
            _stats["wall_seconds"] += batch_wall  # wall time shared across the batch
        else:
            await asyncio.sleep(
                _POLL_SECONDS
            )  # missing sus or renderer down so back off


def _empty_pools() -> int:
    return sum(1 for t in TYPES if count(t) == 0)


def start(pjsk: "PJSKData") -> None:
    """begin filling the cache in the background, only one process should do this"""
    global _pjsk, _running, _task
    _pjsk = pjsk
    # keep a session warm per empty pool so an on-the-fly render for a drained type is ready
    chart_preview.set_warm_source(_empty_pools)
    if _task and not _task.done():
        return
    _running = True
    _task = asyncio.create_task(_worker())


async def stop() -> None:
    global _running, _task
    _running = False
    chart_preview.set_warm_source(None)
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
