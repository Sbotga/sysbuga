"""Render Sonolus charts to MP4 via the nxsk-chart-preview binary.

Run once in `--export-cli` server mode (GL context + assets load once): it prints READY,
then renders one request per stdin line and replies "OK <path>" / "ERR <msg>". Needs an
OpenGL context, so on a headless host it's wrapped in `xvfb-run`.
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

_SERVER_START_TIMEOUT = 90.0  # one-time gl + asset load

# one server; renders serialize through it (parallel software-gl renders just fight for cores)
_lock = asyncio.Lock()
_process: "asyncio.subprocess.Process | None" = None


def available() -> bool:
    """Whether the renderer binary is present. Callers can fall back when it isn't."""
    return _EXECUTABLE.is_file()


class ChartPreviewError(RuntimeError):
    def __init__(self, returncode: int, message: str) -> None:
        super().__init__(f"nxsk-chart-preview: {message}")
        self.returncode = returncode
        self.message = message


def _headless_argv(argv: list[str]) -> list[str]:
    """Wrap in Xvfb when there is no display, so the GL context can be created headless."""
    if os.name == "nt" or os.environ.get("DISPLAY"):
        return argv
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run is None:
        raise ChartPreviewError(
            -1, "no DISPLAY and xvfb-run is not installed (apt install xvfb)"
        )
    return [xvfb_run, "-a", *argv]


async def _kill_tree(process: "asyncio.subprocess.Process") -> None:
    """kill the whole process group: on linux the child is `xvfb-run`, which forks xvfb +
    the renderer, so killing just it orphans them. on windows the child is the renderer.
    """
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # already gone
    try:
        await process.wait()
    except ProcessLookupError:
        pass


async def _stop_locked() -> None:
    """Tear down the current server. Caller must hold `_lock`."""
    global _process
    if _process is not None:
        await _kill_tree(_process)
        _process = None


async def _ensure_server() -> "asyncio.subprocess.Process":
    """The live server, (re)starting it if it isn't running. Caller must hold `_lock`."""
    global _process
    if _process is not None and _process.returncode is None:
        return _process
    if not _EXECUTABLE.is_file():
        raise ChartPreviewError(-1, f"renderer not found at {_EXECUTABLE}")

    _process = await asyncio.create_subprocess_exec(
        *_headless_argv([str(_EXECUTABLE), "--export-cli"]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,  # per-frame progress noise
        start_new_session=os.name != "nt",
    )
    assert _process.stdout is not None
    try:
        while True:
            raw = await asyncio.wait_for(
                _process.stdout.readline(), _SERVER_START_TIMEOUT
            )
            if not raw:
                raise ChartPreviewError(-1, "renderer exited before READY")
            if raw.decode("utf-8", "replace").strip() == "READY":
                break
    except (asyncio.TimeoutError, ChartPreviewError):
        await _stop_locked()
        raise ChartPreviewError(-1, "renderer failed to start") from None
    return _process


def _quote(token: str) -> str:
    # nxsk strips quotes as pure grouping, so quoting everything is safe and handles spaces
    return '"' + token + '"'


async def render(
    chart: Path,
    output: Path,
    *,
    settings: Path | None = None,
    cover: Path | None = None,
    bgm: Path | None = None,
    crf: int = 18,
    default_settings: bool = True,
    timeout: float | None = 180.0,
) -> Path:
    """Export `chart` (.json.gz) to `output` as MP4. `cover`/`bgm` are optional files nxsk
    classifies by extension (a .png cover, an audio bgm). `crf` is the per-render libx264
    quality (0-51, lower = better/larger); `default_settings` starts from built-in defaults
    so a render doesn't inherit a previous request's state."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tokens = [str(chart)]
    if settings is not None:
        tokens.append(str(settings))
    if cover is not None:
        tokens.append(str(cover))
    if bgm is not None:
        tokens.append(str(bgm))
    if default_settings:
        tokens.append("--default-settings")
    tokens += ["--crf", str(crf), "--export", str(output)]
    request = (" ".join(_quote(t) for t in tokens) + "\n").encode("utf-8")

    async with _lock:
        process = await _ensure_server()
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(request)
            await process.stdin.drain()
            while True:
                raw = await asyncio.wait_for(process.stdout.readline(), timeout)
                if not raw:
                    await _stop_locked()  # server died mid-request
                    raise ChartPreviewError(-1, "renderer closed unexpectedly")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("OK "):
                    return output
                if line.startswith("ERR "):
                    raise ChartPreviewError(1, line[4:])  # in sync; don't restart
        except (
            asyncio.TimeoutError,
            asyncio.CancelledError,
            BrokenPipeError,
            ConnectionResetError,
        ) as exc:
            # mid-render or broken pipe: restart, or the next request reads this one's late reply
            await _stop_locked()
            if isinstance(exc, asyncio.TimeoutError):
                raise ChartPreviewError(
                    -1, f"render timed out after {timeout}s"
                ) from None
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ChartPreviewError(-1, "renderer pipe broke") from None


async def start() -> None:
    """Warm the server up front so the first render doesn't pay the GL + asset load.
    Best-effort: a failure here just means the first real render (re)tries, then falls back.
    """
    if not available():
        return
    try:
        async with _lock:
            await _ensure_server()
    except Exception:
        pass


async def stop() -> None:
    """Shut the server down (graceful `quit`, then kill). Safe to call when none is running."""
    async with _lock:
        global _process
        if _process is None:
            return
        if _process.returncode is None and _process.stdin is not None:
            try:
                _process.stdin.write(b"quit\n")
                await _process.stdin.drain()
                await asyncio.wait_for(_process.wait(), 5.0)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                await _kill_tree(_process)
        _process = None
