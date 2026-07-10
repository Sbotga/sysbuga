"""Pre-render chart-guess clips to disk so a round can grab one instantly.

A background worker keeps up to TARGET_PER_TYPE clips per chart type on disk, rendering at
the cached (higher) quality whenever the pool is below target and no live render is waiting.
A round pops one instantly; if the pool is empty it renders on the fly (smaller/faster).
Clips are stored non-mirrored, so a user with mirror on always renders live.

Each entry is a folder (chart.mp4 + answer.mp4 + meta.json); it only counts once meta.json's
"complete" flag is written and the folder is atomically renamed into place. Multi-process
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
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from services import chart_clip, chart_preview

if TYPE_CHECKING:
    from data.pjsk import PJSKData

CACHE_ROOT = Path("cache/chart_clips")
TARGET_PER_TYPE = 100
TYPES = ("chart", "chart_append")
_POLL_SECONDS = 10.0

_task: "asyncio.Task | None" = None
_pjsk: "PJSKData | None" = None
_running = False
# on-the-fly renders in flight; the filler pauses while any are waiting so a live round
# doesn't queue behind cache-fill work
_live_pending = 0


def _dir(gtype: str) -> Path:
    return CACHE_ROOT / gtype


# each entry is a folder holding chart.mp4 + answer.mp4 + meta.json; the "_" prefix hides
# an entry still being written (_tmp_) or mid-pop (_claim_) from count()/pop()
def _entries(d: Path) -> "list[Path]":
    return [e for e in d.iterdir() if e.is_dir() and not e.name.startswith("_")]


def _valid(entry: Path) -> "dict[str, Any] | None":
    """The entry's meta if it's fully generated (both clips present, json flagged
    complete), else None. meta.json is written last, so its flag means done."""
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
    """Delete entries a crash/shutdown left half-generated (and any _tmp_/_claim_ leftovers).
    Run once at startup in the filler process, before the worker or any pop."""
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
    """Atomically claim one cached entry. Returns (chart mp4, answer mp4, meta)."""
    d = _dir(gtype)
    if not d.exists():
        return None
    for entry in sorted(_entries(d)):
        claim = d / f"_claim_{entry.name}"
        try:
            os.rename(entry, claim)  # atomic — whoever wins the rename owns the entry
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
    # meta.json written last, with the flag that signals the whole entry is done
    (tmp / "meta.json").write_text(json.dumps({**meta, "complete": True}), "utf-8")
    os.rename(tmp, d / uid)  # atomic publish


class live_priority:
    """Mark an on-the-fly render in progress so the filler yields to it."""

    def __enter__(self) -> "live_priority":
        global _live_pending
        _live_pending += 1
        return self

    def __exit__(self, *_: Any) -> None:
        global _live_pending
        _live_pending -= 1


# the reveal audio, best available in this order
_VOCAL_PRIORITY = ("instrumental", "sekai", "virtual_singer")


def _pick_bgm_url(music: Any) -> str | None:
    # trimmed (silence-removed) audio, so it lines up with the chart's time coordinate
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
    # nxsk's image loader can't decode webp; the .png variant sits beside it on R2
    return url.rsplit(".", 1)[0] + ".png"


async def _fetch_bytes(url: str) -> bytes | None:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read() if resp.status == 200 else None


async def _render_one(gtype: str) -> bool:
    assert _pjsk is not None
    has_append = gtype == "chart_append"
    want = "append" if has_append else "master"
    musics = [
        m for m in _pjsk.musics() if any(d.difficulty == want for d in m.difficulties)
    ]
    if not musics:
        return False
    music = random.choice(musics)
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
    chart_mp4 = await chart_clip.render_leveldata(
        window, mirror=False, height=chart_clip.CACHED_HEIGHT, fps=chart_clip.CACHED_FPS
    )

    # a cache entry must have the reveal video too; skip the song if we can't build it
    bgm_url = _pick_bgm_url(music)
    if not bgm_url or not music.jacket_url:
        return False
    bgm = await _fetch_bytes(bgm_url)
    jacket = await _fetch_bytes(_png_jacket_url(music.jacket_url))
    if not bgm or not jacket:
        return False
    try:
        answer_mp4 = await chart_clip.render_answer_video(
            window,
            jacket,
            bgm,
            height=chart_clip.CACHED_HEIGHT,
            fps=chart_clip.CACHED_FPS,
        )
    except chart_clip.ChartClipError:
        return False

    _store(gtype, chart_mp4, answer_mp4, {"music_id": music.id, "diff": want})
    return True


async def _worker() -> None:
    while _running:
        if not chart_preview.available() or _live_pending:
            await asyncio.sleep(_POLL_SECONDS)
            continue
        target = next((t for t in TYPES if count(t) < TARGET_PER_TYPE), None)
        if target is None:
            await asyncio.sleep(_POLL_SECONDS)  # all pools full
            continue
        try:
            ok = await _render_one(target)
        except Exception:
            ok = False
        if not ok:
            await asyncio.sleep(_POLL_SECONDS)  # missing SUS / renderer down: back off


def start(pjsk: "PJSKData") -> None:
    """Begin filling the cache in the background. Only one process should do this."""
    global _pjsk, _running, _task
    _pjsk = pjsk
    if _task and not _task.done():
        return
    _running = True
    _task = asyncio.create_task(_worker())


async def stop() -> None:
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
