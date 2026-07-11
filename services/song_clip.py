"""Cut short audio snippets out of a song for the 'guess the music' mode.

A round fixes one window start; each hint reveals a longer clip from it - 1s, then 3s, 5s,
and finally the whole 7s. Cutting a NOSIL (silence-trimmed) vocal so the window lands on
actual audio. The full track is kept for the reveal.
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
FULL_SECONDS = 7.0  # the longest (final) reveal clip
# stage -> seconds of the window revealed; stage 4 is the whole window
STAGE_SECONDS = {1: 1.0, 2: 3.0, 3: 5.0, 4: FULL_SECONDS}
MAX_STAGE = 4


class SongClipError(RuntimeError):
    pass


def pick_nosil_url(music: Any) -> str | None:
    """A random vocal's silence-trimmed audio - no vocal-type priority."""
    urls = [v.bgm_nosil_url for v in music.vocals if v.bgm_nosil_url]
    return random.choice(urls) if urls else None


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
    """The first STAGE_SECONDS[stage] seconds of the window that begins at `start`."""
    seconds = STAGE_SECONDS.get(stage, FULL_SECONDS)
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
