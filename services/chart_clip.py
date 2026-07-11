"""cut a short window out of a chart's sus and render it to mp4 for chart guessing"""

from __future__ import annotations

import asyncio
import io
import json
import random
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from sonolus_converters import sus
from sonolus_converters.LevelData import next_sekai

from services import chart_preview

if TYPE_CHECKING:
    from data.models import Music

_TMP_BASE = Path(tempfile.gettempdir()) / "sbuga_chart_clips"

CLIP_SECONDS = 9.0
MIN_START = 3.0
MIN_COMBO = 1
MIN_NOTES = 5
WINDOW_ATTEMPTS = 8
_CLEAR_TYPE_NONE = 8  # nxsk pvClearType with no result screen
CLIP_CRF = 23  # keeps clips well under discord's 10mb

easter_eggs = [
    {
        "chance": 0.01,  # 1% chance
        "settings": {"pvNoteSpeed": 16.0},
        "description": "The note speed is 16 now...",
    },
    {
        "chance": 0.01,  # 1% chance
        "settings": {"pvNoteSpeed": 1.0},
        "description": "The note speed is 1 now...",
    },
    {
        "chance": 0.03,  # 3% chance
        "settings": {
            "pvWrongWayEnglish": False,
            "pvJudgeWrongWayPct": 1.0,
            "pvJudgeLatePct": 0.5,
            "pvShowJudgeTiming": True,
            "pvJudgeType": 2,
        },
        "description": "Enjoy all-great!",
    },
    {
        "chance": 0.01,  # 1% chance
        "settings": {"pvFunAllTrace": True},
        "description": "woah it's all traces",
    },
    {
        "chance": 0.01,  # 1% chance
        "settings": {"pvFunAllFlick": True},
        "description": "woah it's all flicks",
    },
    {
        "chance": 0.01,  # 1% chance
        "settings": {"pvFunAllCritical": True},
        "description": "woah it's all gold notes",
    },
    {
        "chance": 0.01,  # 1% chance
        "settings": {"pvPlaybackSpeed": 2.0},
        "description": "Why is it in 2x speed",
    },
]


class ChartClipError(RuntimeError):
    pass


def cleanup_stale() -> None:
    """remove clip scratch dirs a previous crash or kill left behind"""
    if _TMP_BASE.exists():
        for child in _TMP_BASE.iterdir():
            shutil.rmtree(child, ignore_errors=True)


class Window:
    """a chosen cut with the exported chart and the audio window it came from"""

    def __init__(
        self,
        leveldata: bytes,
        starting_combo: int,
        total_combo: int,
        start: float,
        end: float,
    ) -> None:
        self.leveldata = leveldata
        self.starting_combo = starting_combo
        self.total_combo = total_combo
        self.start = start
        self.end = end


_TICKS_PER_BEAT = 480


def cut_window(sus_text: str) -> "Window | None":
    """pick a random window with enough content or none if nothing qualifies
    cut mutates the score so each attempt reloads from the sus text"""
    base = sus.load(io.StringIO(sus_text))  # untouched for the pre-cut tempo map
    if base.duration <= MIN_START:
        return None

    # keep the whole window inside the chart so we never cut a stub at the end
    # if it can't fit MIN_START plus a full clip take the last CLIP_SECONDS or the whole chart
    latest_start = base.duration - CLIP_SECONDS
    if latest_start < MIN_START:
        start_lo = start_hi = max(0.0, latest_start)
    else:
        start_lo, start_hi = MIN_START, latest_start

    for _ in range(WINDOW_ATTEMPTS):
        score = sus.load(io.StringIO(sus_text))
        start = random.uniform(start_lo, start_hi)
        starting_combo, (start_tick, end_tick) = score.cut(start, start + CLIP_SECONDS)
        if score.combo_count < MIN_COMBO or score.note_count < MIN_NOTES:
            continue
        # exact audio window from the ticks cut snapped to off the original tempo
        audio_start = base.time_at_beat(start_tick / _TICKS_PER_BEAT)
        audio_end = base.time_at_beat(end_tick / _TICKS_PER_BEAT)
        buffer = io.BytesIO()
        next_sekai.export(buffer, score, as_compressed=True)
        return Window(
            buffer.getvalue(),
            starting_combo,
            base.combo_count,  # the full chart's combo before the cut
            audio_start,
            audio_end,
        )
    return None


# cached clips get the nicer quality and the on-the-fly fallback is smaller and faster
CACHED_HEIGHT, CACHED_FPS = 480, 30
LIVE_HEIGHT, LIVE_FPS = 360, 24

# each chart-guess mode and the difficulty it shows
DIFFICULTIES = {"chart": "master", "chart_append": "append", "chart_expert": "expert"}
# how many pre-rendered clips to keep on disk per chart-guess type
TARGETS = {"chart": 1000, "chart_append": 1000, "chart_expert": 1000}

# exponent for weighting chart selection by play level, per difficulty
# 1.0 is proportional to song count while lower flattens toward an even per-level pick so
# rarer levels get boosted, append has the longest tail so it stays closest to proportional
CHART_LEVEL_ALPHA = {"master": 0.65, "append": 0.8, "expert": 0.55}


def weighted_chart_music(musics: "Iterable[Music]", difficulty: str) -> "Music | None":
    """pick a song that has this difficulty, weighted by play level so rarer levels get boosted
    without flattening to an even per-level pick
    buckets songs by level, weights each level by count ** alpha, then picks a level and a
    uniform song in it. none if no song has the difficulty"""
    buckets: dict[int, list["Music"]] = {}
    for music in musics:
        diff = next((d for d in music.difficulties if d.difficulty == difficulty), None)
        if diff is not None:
            buckets.setdefault(diff.play_level, []).append(music)
    if not buckets:
        return None
    alpha = CHART_LEVEL_ALPHA.get(difficulty, 1.0)
    levels = list(buckets)
    weights = [len(buckets[level]) ** alpha for level in levels]
    level = random.choices(levels, weights=weights, k=1)[0]
    return random.choice(buckets[level])


def roll_easter_eggs() -> tuple[dict, list[str]]:
    """roll every egg independently and return the merged overrides plus descriptions for the
    ones that hit where multiple can stack
    an egg is skipped when a setting it wants was already claimed by an earlier egg so they
    never fight over the same one"""
    settings: dict = {}
    descriptions: list[str] = []
    for egg in easter_eggs:
        if any(key in settings for key in egg["settings"]):
            continue  # an earlier egg already claimed one of these settings
        if random.random() < egg["chance"]:
            settings.update(egg["settings"])
            # prefix the roll odds like (2%) or (0.5%)
            descriptions.append(f"({egg['chance'] * 100:g}%) {egg['description']}")
    return settings, descriptions


def _settings(
    starting_combo: int,
    total_combo: int,
    height: int,
    fps: int,
    talent: int = 250000,
    extra: dict | None = None,
) -> str:
    payload = {
        "exportHeight": height,
        "exportFps": fps,
        "exportPreset": 0,  # veryfast
        "exportEncThreads": 1,
        "pvNoteSpeed": 10.6,
        "pvShowStart": False,
        "pvClearType": _CLEAR_TYPE_NONE,
        "pvPreRollDuration": 1.0,
        "pvStartingCombo": starting_combo,
        "pvStartingScore": round(
            4.42
            * talent
            * (starting_combo / total_combo)
            * min(1 + 0.00005 * starting_combo, 1.1)
        ),
        "pvWatermarkEnabled": True,
        "pvWatermarkText": "Rendered by\nSYSbuga Discord Bot",
        "scoreTalent": talent,
        "pvAutoIndicator": True,
    }
    if extra:  # easter-egg overrides
        payload.update(extra)
    return json.dumps(payload)


async def render_leveldata(
    window: "Window",
    *,
    height: int = CACHED_HEIGHT,
    fps: int = CACHED_FPS,
    cover: bytes | None = None,
    bgm: bytes | None = None,
    extra_settings: dict | None = None,
    timeout: float = 180.0,
) -> bytes:
    """render an already-cut window's chart to mp4
    cover is an optional jacket png and bgm optional audio that nxsk composites in
    extra_settings overrides render settings for easter eggs
    raises ChartClipError on failure"""
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clip_", dir=_TMP_BASE) as tmp:
        tmp_path = Path(tmp)
        chart_path = tmp_path / "clip.json.gz"
        settings_path = tmp_path / "settings.json"
        output_path = tmp_path / "clip.mp4"
        chart_path.write_bytes(window.leveldata)
        settings_path.write_text(
            _settings(
                window.starting_combo,
                window.total_combo,
                height,
                fps,
                extra=extra_settings,
            ),
            encoding="utf-8",
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
    height: int = CACHED_HEIGHT,
    fps: int = CACHED_FPS,
    timeout: float = 180.0,
) -> "tuple[bytes, list[str]] | None":
    """rendered mp4 and egg descriptions for a random window of sus_text
    or none if the chart has no usable window
    raises ChartClipError if the render fails"""
    # pure cpu keep off the event loop
    window = await asyncio.get_running_loop().run_in_executor(
        None, cut_window, sus_text
    )
    if window is None:
        return None
    egg_settings, egg_descriptions = roll_easter_eggs()
    clip = await render_leveldata(
        window, height=height, fps=fps, extra_settings=egg_settings, timeout=timeout
    )
    return clip, egg_descriptions


async def _clip_audio(music: bytes, start: float, duration: float) -> bytes:
    """cut [start, start+duration] out of an mp3 so it lines up with the shifted-to-zero chart
    nxsk re-encodes the audio anyway so a stream copy is fine"""
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
    height: int = CACHED_HEIGHT,
    fps: int = CACHED_FPS,
    timeout: float = 180.0,
) -> bytes:
    """the reveal clip the same cut chart rendered by nxsk with the jacket as the cover and
    the window's clipped audio as the bgm
    pre-rendered only since audio can't leak
    no easter-egg overrides so the reveal always shows the clean chart"""
    clipped = await _clip_audio(music, window.start, window.end - window.start)
    return await render_leveldata(
        window,
        height=height,
        fps=fps,
        cover=jacket,
        bgm=clipped,
        timeout=timeout,
    )
