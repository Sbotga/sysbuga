"""cut short audio snippets out of a song for the guess the music mode

a round fixes one window start and each hint reveals a longer clip from it, 1s then 3s then 5s,
and the last hint keeps the 5s clip but reveals the cover type. cutting a nosil (silence-trimmed)
vocal so the window lands on actual audio. the full track is kept for the reveal.
"""

from __future__ import annotations

import asyncio
import random
import tempfile
from pathlib import Path
from typing import Any

_TMP_BASE = Path(tempfile.gettempdir()) / "sbuga_song_clips"

CLIP_START_MIN = 5.0  # cut at least this far from the song's start
CLIP_END_MARGIN = 20.0  # ...and the cut point at least this far from its end
FULL_SECONDS = 5.0  # the longest clip length (the final hint reveals the cover type, not more audio)
# stage -> seconds of the window revealed; stage 4 keeps stage 3's length and adds the cover type
STAGE_SECONDS = {1: 1.0, 2: 3.0, 3: 5.0, 4: 5.0}
MAX_STAGE = 4

# japanese vocal captions normalized to english (see sbuga-sonolus-server music api)
CAPTION_JP_TO_EN: dict[str, str] = {
    "バーチャル・シンガーver.": "VIRTUAL SINGER ver.",
    "セカイver.": "SEKAI ver.",
    "ワンダーランズ×ショウタイム ver.": "Wonderlands×Showtime ver.",
    "25時、ナイトコードで。ver.": "Nightcord at 25:00 ver.",
    "アナザーボーカルver.": "Cover ver.",
    "Inst.ver.": "Instrumental ver.",
    "エイプリルフールver.": "April Fool's ver.",
    "コネクトライブver.": "Connect Live ver.",
    "コネクトライブ(DAY1夜)ver.": "Connect Live (DAY1 Night) ver.",
    "コネクトライブ(DAY1昼)ver.": "Connect Live (DAY1 Day) ver.",
    "コネクトライブ(DAY2夜)ver.": "Connect Live (DAY2 Night) ver.",
    "コネクトライブ(DAY2昼)ver.": "Connect Live (DAY2 Day) ver.",
    "あんさんぶるスターズ！！コラボver.": "Ensemble Stars!! Crossover ver.",
    "「劇場版プロジェクトセカイ」ver.": "COLORFUL STAGE! The Movie ver.",
}


def normalize_caption(caption: str) -> str:
    return CAPTION_JP_TO_EN.get(caption, caption)


class SongClipError(RuntimeError):
    pass


def pick_nosil(music: Any) -> "tuple[str, str] | None":
    """a random vocal's silence-trimmed audio url and its cover type caption in english, or
    none if the song has no nosil vocal"""
    vocals = [v for v in music.vocals if v.bgm_nosil_url]
    if not vocals:
        return None
    vocal = random.choice(vocals)
    return vocal.bgm_nosil_url, normalize_caption(vocal.caption)


def clip_filename(stage: int) -> str:
    n = int(STAGE_SECONDS.get(stage, FULL_SECONDS))
    return f"song ({n} second{'' if n == 1 else 's'}).mp3"


async def _ffprobe_duration(path: Path) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return None


async def choose_window(audio: bytes) -> float | None:
    """Pick a start offset for the clip window: at least CLIP_START_MIN from the start and
    CLIP_END_MARGIN from the end. None if the song is too short to place one."""
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe_", dir=_TMP_BASE) as tmp:
        src = Path(tmp) / "full.mp3"
        src.write_bytes(audio)
        duration = await _ffprobe_duration(src)
    if duration is None:
        return None
    latest = duration - CLIP_END_MARGIN
    if latest < CLIP_START_MIN:
        return None  # too short to place a window
    return random.uniform(CLIP_START_MIN, latest)


async def stage_clip(audio: bytes, start: float, stage: int) -> bytes:
    """the first STAGE_SECONDS[stage] seconds of the window that begins at start"""
    # cut a touch short of the advertised length so discord's rounded-up duration display
    # matches (a 1.0s mp3 shows as 2s otherwise)
    seconds = max(0.1, STAGE_SECONDS.get(stage, FULL_SECONDS))
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stage_", dir=_TMP_BASE) as tmp:
        src = Path(tmp) / "full.mp3"
        out = Path(tmp) / "stage.mp3"
        src.write_bytes(audio)
        await _ffmpeg_cut(src, out, start=start, length=seconds)
        return out.read_bytes()


async def _ffmpeg_cut(src: Path, out: Path, *, start: float, length: float) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{length:.3f}",
        "-i",
        str(src),
        "-c:a",
        "libmp3lame",
        "-q:a",
        "5",
        str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise SongClipError(
            f"ffmpeg cut failed ({proc.returncode}): "
            f"{err.decode(errors='replace')[-300:]}"
        )
