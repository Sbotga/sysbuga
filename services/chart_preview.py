"""Render a Sonolus chart to MP4 with the bundled nxsk-chart-preview binary.

The renderer is an executable, not an importable library, so this drives it as a subprocess.
It always creates an OpenGL context -- even for `--export` -- so on a headless Linux host the
call is wrapped in `xvfb-run`. See the README's Ubuntu setup for the apt packages it needs.
"""

import asyncio
import os
import shutil
import signal
from pathlib import Path

_LIBRARIES = Path(__file__).resolve().parent.parent / "libraries"
_EXECUTABLE = _LIBRARIES / (
    "nxsk-chart-preview.exe" if os.name == "nt" else "nxsk-chart-preview"
)

# Progress goes to stderr as "export: 1234/5678 frames (21.7%)"; keep the tail for error reports.
_STDERR_TAIL = 2000

# Each render pins a CPU core for minutes. The subprocess never blocks the event loop,
# but letting N of them run at once starves every other thread on the box -- including
# the one answering Discord's heartbeat -- so the bot looks hung. Leave a core free.
MAX_CONCURRENT_RENDERS = max(1, (os.cpu_count() or 2) - 1)
_slots = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)


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


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """On Linux the child is `xvfb-run`, a shell script that forks Xvfb and the renderer.
    Killing it alone orphans both, and the renderer keeps a core pinned until the host
    reboots. The spawn puts them in their own process group so the group can be killed.
    On Windows there is no wrapper, so the child *is* the renderer."""
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # already gone
    await process.wait()


async def render(
    chart: Path,
    output: Path,
    *,
    bgm: Path | None = None,
    cover: Path | None = None,
    settings: Path | None = None,
    default_settings: bool = True,
    timeout: float | None = 900.0,
) -> Path:
    """Export `chart` (a Sonolus .json.gz level) to `output` as an MP4.

    `settings` is the same override JSON the loader scripts write, and it can drive essentially
    every knob the GUI exposes -- note speed, resolution/fps/encoder, stage and effect options,
    the start screen, the warning text, the watermark. Top-level keys are locked for the run; an
    optional "prefill" sub-object holds keys that stay editable.

    `default_settings` passes --default-settings, which starts from the built-in defaults instead
    of whatever settings.json happens to sit beside the binary, and stops the renderer writing one
    back into libraries/. Combined with a settings file that means: clean baseline, then exactly
    the keys you asked for -- reproducible regardless of host state. Turn it off only if you
    deliberately want the saved settings as the baseline.

    The whole chart is rendered -- there is no trim range -- so expect roughly a third of the
    song's duration in wall time on a CPU-only host, and considerably less on a GPU.
    """
    if not _EXECUTABLE.is_file():
        raise ChartPreviewError(-1, f"renderer not found at {_EXECUTABLE}")

    argv = [str(_EXECUTABLE), str(chart)]
    argv += [str(path) for path in (bgm, cover, settings) if path is not None]
    if default_settings:
        argv.append("--default-settings")
    argv += ["--export", str(output)]

    output.parent.mkdir(parents=True, exist_ok=True)
    async with _slots:
        process = await asyncio.create_subprocess_exec(
            *_headless_argv(argv),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            # own process group, so a timeout can take the whole tree down with it
            start_new_session=os.name != "nt",
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            await _kill_tree(process)
            raise ChartPreviewError(-1, f"timed out after {timeout}s") from None
        except asyncio.CancelledError:
            # the caller went away (command timed out, cog unloaded); don't leak a render
            await _kill_tree(process)
            raise

    if process.returncode != 0:
        tail = stderr.decode(errors="replace")[-_STDERR_TAIL:]
        raise ChartPreviewError(process.returncode or -1, tail)
    return output
