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


def _cut_window(sus_text: str) -> tuple[bytes, int] | None:
    """Pick a random ~10s window with enough content and return
    (gzipped Next-SEKAI LevelData, starting combo). None if no window qualifies.

    cut() mutates the Score, so each attempt reloads from the SUS text."""
    duration = sus.load(io.StringIO(sus_text)).duration
    if duration <= MIN_START:
        return None

    for _ in range(WINDOW_ATTEMPTS):
        score = sus.load(io.StringIO(sus_text))
        start = random.uniform(MIN_START, duration)
        starting_combo = score.cut(start, start + CLIP_SECONDS)
        if score.combo_count < MIN_COMBO or score.note_count < MIN_NOTES:
            continue
        buffer = io.BytesIO()
        next_sekai.export(buffer, score, as_compressed=True)
        return buffer.getvalue(), starting_combo
    return None


def _settings(starting_combo: int, mirror: bool) -> str:
    payload = {
        "exportHeight": 720,
        "exportFps": 24,
        "exportPreset": 0,  # veryfast
        "pvShowStart": False,
        "pvDrawScoreHud": False,
        "pvDrawLifeHud": False,
        "pvClearType": _CLEAR_TYPE_NONE,
        "pvPreRollDuration": 3.0,
        "pvStartingCombo": starting_combo,
        "pvMirrorScore": mirror,
    }
    return json.dumps(payload)


async def render_clip(
    sus_text: str, *, mirror: bool = False, timeout: float = 180.0
) -> bytes | None:
    """A rendered MP4 of a random ~10s window of `sus_text`, or None if the chart has
    no usable window. Raises ChartClipError if the render itself fails."""
    # pure cpu; keep off the event loop
    window = await asyncio.get_running_loop().run_in_executor(
        None, _cut_window, sus_text
    )
    if window is None:
        return None
    leveldata, starting_combo = window

    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clip_", dir=_TMP_BASE) as tmp:
        tmp_path = Path(tmp)
        chart_path = tmp_path / "clip.json.gz"
        settings_path = tmp_path / "settings.json"
        output_path = tmp_path / "clip.mp4"
        chart_path.write_bytes(leveldata)
        settings_path.write_text(_settings(starting_combo, mirror), encoding="utf-8")

        try:
            await chart_preview.render(
                chart_path,
                output_path,
                settings=settings_path,
                crf=CLIP_CRF,
                timeout=timeout,
            )
        except chart_preview.ChartPreviewError as exc:
            raise ChartClipError(str(exc)) from exc

        return output_path.read_bytes()
