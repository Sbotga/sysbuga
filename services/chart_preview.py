"""Render a Sonolus chart to MP4 with the bundled nxsk-chart-preview binary.

The renderer is an executable, not an importable library, so this drives it as a subprocess.
It always creates an OpenGL context -- even for `--export` -- so on a headless Linux host the
call is wrapped in `xvfb-run`. See the README's Ubuntu setup for the apt packages it needs.
"""

import asyncio
import os
import shutil
from pathlib import Path

_LIBRARIES = Path(__file__).resolve().parent.parent / "libraries"
_EXECUTABLE = _LIBRARIES / (
    "nxsk-chart-preview.exe" if os.name == "nt" else "nxsk-chart-preview"
)

# Progress goes to stderr as "export: 1234/5678 frames (21.7%)"; keep the tail for error reports.
_STDERR_TAIL = 2000


class ChartPreviewError(RuntimeError):
    """The renderer exited non-zero. `stderr` holds the tail of its output."""

    def __init__(self, returncode: int, stderr: str) -> None:
        super().__init__(f"nxsk-chart-preview exited {returncode}: {stderr}")
        self.returncode = returncode
        self.stderr = stderr


def _headless_argv(argv: list[str]) -> list[str]:
    """Wrap in Xvfb when there is no display. `-a` picks a free display number, so concurrent
    renders don't collide -- though each one saturates a CPU core, so don't outrun `nproc`.
    """
    if os.name == "nt" or os.environ.get("DISPLAY"):
        return argv
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run is None:
        raise ChartPreviewError(
            -1, "no DISPLAY and xvfb-run is not installed (apt install xvfb)"
        )
    return [xvfb_run, "-a", *argv]


async def render(
    chart: Path,
    output: Path,
    *,
    bgm: Path | None = None,
    cover: Path | None = None,
    settings: Path | None = None,
    timeout: float | None = 900.0,
) -> Path:
    """Export `chart` (a Sonolus .json.gz level) to `output` as an MP4.

    `settings` is the same override JSON the loader scripts write: top-level keys are locked for
    the run, and an optional "prefill" sub-object holds keys that stay editable. Use it to inject
    the start-screen title/artist/difficulty, the warning text, or the spoiler watermark.

    The whole chart is rendered -- there is no trim range -- so expect roughly a third of the
    song's duration in wall time on a CPU-only host, and considerably less on a GPU.
    """
    if not _EXECUTABLE.is_file():
        raise ChartPreviewError(-1, f"renderer not found at {_EXECUTABLE}")

    argv = [str(_EXECUTABLE), str(chart)]
    argv += [str(path) for path in (bgm, cover, settings) if path is not None]
    # --default-settings keeps the run reproducible and stops the renderer writing a settings.json
    # next to itself (i.e. into libraries/), which a shared install must not accumulate.
    argv += ["--default-settings", "--export", str(output)]

    output.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        *_headless_argv(argv),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ChartPreviewError(-1, f"timed out after {timeout}s") from None

    if process.returncode != 0:
        tail = stderr.decode(errors="replace")[-_STDERR_TAIL:]
        raise ChartPreviewError(process.returncode or -1, tail)
    return output
