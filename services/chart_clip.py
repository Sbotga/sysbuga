"""Cut a short window out of a chart's SUS and render it to MP4 for chart guessing.

~10s is cut
"""

from __future__ import annotations

import asyncio
import io
import json
import random
import shutil
import tempfile
from pathlib import Path

from sonolus_converters import sus
from sonolus_converters.LevelData import next_sekai

from services import chart_preview

_TMP_BASE = Path(tempfile.gettempdir()) / "sbuga_chart_clips"

CLIP_SECONDS = 10.0
MIN_START = 3.0
MIN_COMBO = 1
MIN_NOTES = 5
WINDOW_ATTEMPTS = 8
_CLEAR_TYPE_NONE = 8  # nxsk pvClearType: no result screen
CLIP_CRF = 23  # keeps clips well under discord's 10mb (18 = 7-10mb)


class ChartClipError(RuntimeError):
    pass


def cleanup_stale() -> None:
    """remove clip scratch dirs a previous crash/kill left behind."""
    if _TMP_BASE.exists():
        for child in _TMP_BASE.iterdir():
            shutil.rmtree(child, ignore_errors=True)


class Window:
    """A chosen cut: the exported chart plus the audio window it was cut from."""

    def __init__(
        self, leveldata: bytes, starting_combo: int, start: float, end: float
    ) -> None:
        self.leveldata = leveldata
        self.starting_combo = starting_combo
        self.start = start
        self.end = end


_TICKS_PER_BEAT = 480


def cut_window(sus_text: str) -> "Window | None":
    """Pick a random ~10s window with enough content. None if none qualifies.
    cut() mutates the Score, so each attempt reloads from the SUS text."""
    base = sus.load(io.StringIO(sus_text))  # untouched, for the pre-cut tempo map
    if base.duration <= MIN_START:
        return None

    for _ in range(WINDOW_ATTEMPTS):
        score = sus.load(io.StringIO(sus_text))
        start = random.uniform(MIN_START, base.duration)
        starting_combo, (start_tick, end_tick) = score.cut(start, start + CLIP_SECONDS)
        if score.combo_count < MIN_COMBO or score.note_count < MIN_NOTES:
            continue
        # exact audio window from the ticks cut() snapped to (times off the original tempo)
        audio_start = base.time_at_beat(start_tick / _TICKS_PER_BEAT)
        audio_end = base.time_at_beat(end_tick / _TICKS_PER_BEAT)
        buffer = io.BytesIO()
        next_sekai.export(buffer, score, as_compressed=True)
        return Window(buffer.getvalue(), starting_combo, audio_start, audio_end)
    return None


# cached (pre-rendered) clips get the nicer quality; the on-the-fly fallback is smaller/faster
CACHED_HEIGHT, CACHED_FPS = 576, 30
LIVE_HEIGHT, LIVE_FPS = 360, 24

# how many pre-rendered clips to keep on disk per chart-guess type
TARGETS = {"chart": 100, "chart_append": 100}


def _settings(starting_combo: int, mirror: bool, height: int, fps: int) -> str:
    payload = {
        "exportHeight": height,
        "exportFps": fps,
        "exportPreset": 0,  # veryfast
        "exportEncThreads": 2,
        "exportAudioKbps": 128,
        "pvShowStart": False,
        "pvDrawScoreHud": False,
        "pvDrawLifeHud": False,
        "pvClearType": _CLEAR_TYPE_NONE,
        "pvPreRollDuration": 1.0,
        "pvStartingCombo": starting_combo,
        "pvMirrorScore": mirror,
        "pvWatermarkEnabled": True,
        "pvWatermarkText": "Rendered by\nSYSbuga Discord Bot",
    }
    return json.dumps(payload)


async def render_leveldata(
    window: "Window",
    *,
    mirror: bool = False,
    height: int = CACHED_HEIGHT,
    fps: int = CACHED_FPS,
    cover: bytes | None = None,
    bgm: bytes | None = None,
    timeout: float = 180.0,
) -> bytes:
    """Render an already-cut window's chart to MP4. `cover` (jacket png) and `bgm` (audio)
    are optional extras nxsk composites in. Raises ChartClipError on failure."""
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clip_", dir=_TMP_BASE) as tmp:
        tmp_path = Path(tmp)
        chart_path = tmp_path / "clip.json.gz"
        settings_path = tmp_path / "settings.json"
        output_path = tmp_path / "clip.mp4"
        chart_path.write_bytes(window.leveldata)
        settings_path.write_text(
            _settings(window.starting_combo, mirror, height, fps), encoding="utf-8"
        )
        cover_path = bgm_path = None
        if cover is not None:
            cover_path = tmp_path / "cover.png"
            cover_path.write_bytes(cover)
        if bgm is not None:
            bgm_path = tmp_path / "bgm.mp3"
            bgm_path.write_bytes(bgm)
        try:
            await chart_preview.render(
                chart_path,
                output_path,
                settings=settings_path,
                cover=cover_path,
                bgm=bgm_path,
                crf=CLIP_CRF,
                timeout=timeout,
            )
        except chart_preview.ChartPreviewError as exc:
            raise ChartClipError(str(exc)) from exc
        return output_path.read_bytes()


async def render_clip(
    sus_text: str,
    *,
    mirror: bool = False,
    height: int = CACHED_HEIGHT,
    fps: int = CACHED_FPS,
    timeout: float = 180.0,
) -> bytes | None:
    """A rendered MP4 of a random ~10s window of `sus_text`, or None if the chart has
    no usable window. Raises ChartClipError if the render itself fails."""
    # pure cpu; keep off the event loop
    window = await asyncio.get_running_loop().run_in_executor(
        None, cut_window, sus_text
    )
    if window is None:
        return None
    return await render_leveldata(
        window, mirror=mirror, height=height, fps=fps, timeout=timeout
    )


async def _clip_audio(music: bytes, start: float, duration: float) -> bytes:
    """Cut [start, start+duration] out of an mp3 so it lines up with the shifted-to-zero
    chart. nxsk re-encodes the audio anyway, so a stream copy is fine."""
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aud_", dir=_TMP_BASE) as tmp:
        src = Path(tmp) / "full.mp3"
        out = Path(tmp) / "clip.mp3"
        src.write_bytes(music)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(src),
            "-c:a",
            "copy",
            str(out),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ChartClipError(
                f"audio clip failed ({proc.returncode}): "
                f"{stderr.decode(errors='replace')[-300:]}"
            )
        return out.read_bytes()


async def render_answer_video(
    window: "Window",
    jacket: bytes,
    music: bytes,
    *,
    mirror: bool = False,
    height: int = CACHED_HEIGHT,
    fps: int = CACHED_FPS,
    timeout: float = 180.0,
) -> bytes:
    """The reveal clip: the same cut chart, but rendered by nxsk with the jacket as the
    cover and the window's clipped audio as the bgm. Pre-rendered only (audio can't leak).
    """
    clipped = await _clip_audio(music, window.start, window.end - window.start)
    return await render_leveldata(
        window,
        mirror=mirror,
        height=height,
        fps=fps,
        cover=jacket,
        bgm=clipped,
        timeout=timeout,
    )
