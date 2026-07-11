"""render sonolus charts to mp4 via the nxsk-chart-preview binary

each nxsk process runs in --export-cli server mode where the gl context and assets load once,
it prints READY then renders one request per stdin line and replies "OK <path>" or "ERR <msg>".
a bounded pool of these sessions lets renders run in parallel, the cache filler renders a chart
clip and its answer clip at once and on-the-fly rounds get their own session.

sessions are memory-heavy since each is a full gl context and assets so the pool keeps only
what's useful: a maintenance loop holds warm_source() idle sessions ready (the caller sets this
to the number of empty cache pools so a session stays warm exactly when on-the-fly renders are
likely) and trims the rest once they've sat idle past a grace window. MAX_SESSIONS caps total
live sessions. needs an opengl context so on a headless host each is wrapped in xvfb-run.
"""

import asyncio
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Callable

_LIBRARIES = Path(__file__).resolve().parent.parent / "libraries"
_EXECUTABLE = _LIBRARIES / (
    "nxsk-chart-preview.exe" if os.name == "nt" else "nxsk-chart-preview"
)

_SERVER_START_TIMEOUT = 90.0  # one-time gl and asset load per session

# max total live sessions active plus idle
# each is a full gl context and assets so this is the memory ceiling, lower it on a tight box
# sized for chart_cache.FILL_CONCURRENCY filler renders plus one spare for on-the-fly rounds
MAX_SESSIONS = 3
# retire a session after this many renders and spawn a fresh one so leaked gl memory or cpu
# creep from a long-lived process doesn't accumulate
_RENDERS_PER_SESSION = 7
# a just-released session stays warm this long before it can be trimmed
_IDLE_GRACE = 20.0
_MAINTAIN_INTERVAL = 3.0

# a permit is held for a session's whole life so this bounds total sessions
_slots = asyncio.Semaphore(MAX_SESSIONS)
_idle: "list[tuple[Session, float]]" = []  # session and idle_since monotonic
_pool_lock = asyncio.Lock()
_warm_source: "Callable[[], int] | None" = None  # how many idle sessions to keep ready
_maintain_task: "asyncio.Task | None" = None


class ChartPreviewError(RuntimeError):
    def __init__(self, returncode: int, message: str) -> None:
        super().__init__(f"nxsk-chart-preview: {message}")
        self.returncode = returncode
        self.message = message


class Session:
    def __init__(self, process: "asyncio.subprocess.Process") -> None:
        self.process = process
        self.renders = (
            0  # requests served then the session is retired once it hits the cap
        )

    @property
    def alive(self) -> bool:
        return self.process.returncode is None


def available() -> bool:
    """whether the renderer binary is present so callers can fall back when it isn't"""
    return _EXECUTABLE.is_file()


def set_warm_source(fn: "Callable[[], int] | None") -> None:
    """register a function returning how many idle sessions to keep warm clamped to MAX_SESSIONS
    the maintenance loop reads it each tick"""
    global _warm_source
    _warm_source = fn


def _warm_target() -> int:
    if _warm_source is None:
        return 0
    try:
        return max(0, min(_warm_source(), MAX_SESSIONS))
    except Exception:
        return 0


def _headless_argv(argv: list[str]) -> list[str]:
    """wrap in xvfb when there is no display so the gl context can be created headless"""
    if os.name == "nt" or os.environ.get("DISPLAY"):
        return argv
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run is None:
        raise ChartPreviewError(
            -1, "no DISPLAY and xvfb-run is not installed (apt install xvfb)"
        )
    return [xvfb_run, "-a", *argv]


# --- orphan cleanup ---------------------------------------------------------------
# sessions start in their own group so os.killpg can take down xvfb and the renderer together
# the flip side is that on a crash or sigkill stop() never runs and those detached groups keep
# running each holding a gl context and assets
# we record every spawned pid to a pidfile and on the next startup kill any still one of ours
# the pidfile is keyed per pm2 instance or entry script so a restart reaps its own leftovers
# while a concurrently-running sibling like the activity workers is never touched


def _pidfile() -> Path:
    inst = (
        os.environ.get("pm_id")
        or os.environ.get("NODE_APP_INSTANCE")
        or Path(sys.argv[0]).stem
        or "sbuga"
    )
    return Path("cache") / f"nxsk_sessions_{inst}.pid"


def _record_session(pid: int) -> None:
    # "<owner pid> <session pid>" the owner lets a sweep tell a leftover with owner dead from
    # a sibling worker's live session with owner alive even when they share this pidfile
    try:
        pf = _pidfile()
        pf.parent.mkdir(parents=True, exist_ok=True)
        with open(pf, "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {pid}\n")
    except OSError:
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal
    except OSError:
        return False


def _is_renderer(pid: int) -> bool:
    """guard against killing an unrelated process that reused a recorded pid"""
    if os.name == "nt":
        return True  # dev only and the taskkill below is best-effort
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"nxsk-chart-preview" in f.read()
    except OSError:
        return False


def _hard_kill(pid: int) -> None:
    try:
        if os.name == "nt":
            import subprocess

            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def cleanup_orphans() -> None:
    """kill nxsk sessions leaked by a previous run where stop() never ran on a crash or sigkill
    only sessions whose owner process is gone are reaped so a concurrently-running sibling's
    live sessions are left alone
    run once at startup before spawning anything"""
    pf = _pidfile()
    try:
        lines = pf.read_text("utf-8").splitlines()
    except OSError:
        return
    keep: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            owner, session = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if _alive(owner):
            keep.append(line)  # a live instance still owns this session
        elif _is_renderer(session):
            _hard_kill(session)  # owner is gone so reap the leftover
    try:
        if keep:
            pf.write_text("\n".join(keep) + "\n", "utf-8")
        else:
            pf.unlink(missing_ok=True)
    except OSError:
        pass


async def _kill(session: "Session") -> None:
    """kill the whole process group
    on linux the child is xvfb-run which forks xvfb and the renderer so killing just it orphans them
    on windows the child is the renderer"""
    process = session.process
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


async def _spawn() -> "Session":
    """start a session and wait for READY where the caller must already hold a slot permit"""
    if not _EXECUTABLE.is_file():
        raise ChartPreviewError(-1, f"renderer not found at {_EXECUTABLE}")
    process = await asyncio.create_subprocess_exec(
        *_headless_argv([str(_EXECUTABLE), "--export-cli"]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,  # per-frame progress noise
        start_new_session=os.name != "nt",
    )
    session = Session(process)
    _record_session(process.pid)  # so a crash's leftover can be reaped next startup
    assert process.stdout is not None
    try:
        while True:
            raw = await asyncio.wait_for(
                process.stdout.readline(), _SERVER_START_TIMEOUT
            )
            if not raw:
                raise ChartPreviewError(-1, "renderer exited before READY")
            if raw.decode("utf-8", "replace").strip() == "READY":
                break
    except (asyncio.TimeoutError, ChartPreviewError):
        await _kill(session)
        raise ChartPreviewError(-1, "renderer failed to start") from None
    return session


async def _acquire() -> "Session":
    """a ready session for one render reused from idle or freshly spawned bounded by MAX_SESSIONS
    a reused session keeps its permit and a new one takes a fresh permit"""
    async with _pool_lock:
        if _idle:
            return _idle.pop()[0]
    await _slots.acquire()
    try:
        async with _pool_lock:
            if _idle:  # one was released while we waited for a permit
                _slots.release()
                return _idle.pop()[0]
        return await _spawn()
    except BaseException:
        _slots.release()
        raise


async def _release(session: "Session", healthy: bool) -> None:
    # retire rather than re-idle a desynced session or one that's hit its render cap
    if healthy and session.alive and session.renders < _RENDERS_PER_SESSION:
        async with _pool_lock:
            _idle.append((session, time.monotonic()))  # keeps its permit while idle
    else:
        await _kill(session)
        _slots.release()


async def _warm_one() -> None:
    """spawn a session straight into the idle pool and only call when a permit is free"""
    await _slots.acquire()
    try:
        session = await _spawn()
    except BaseException:
        _slots.release()
        raise
    async with _pool_lock:
        _idle.append((session, time.monotonic()))


async def _maintain() -> None:
    while True:
        target = _warm_target()
        now = time.monotonic()
        to_kill: list[Session] = []
        async with _pool_lock:
            # keep the most-recently-idle up to target plus anything still within the grace
            # window like a filler session cycling between renders and trim the rest
            _idle.sort(key=lambda item: item[1], reverse=True)
            kept: list[tuple[Session, float]] = []
            for session, since in _idle:
                if len(kept) < target or (now - since) < _IDLE_GRACE:
                    kept.append((session, since))
                else:
                    to_kill.append(session)
            _idle[:] = kept  # in-place so _idle stays the module global
            deficit = target - len(_idle)
        for session in to_kill:
            await _kill(session)
            _slots.release()
        # top up toward the target one per tick only if a permit is free without blocking
        if deficit > 0 and not _slots.locked():
            try:
                await _warm_one()
            except Exception:
                pass
        await asyncio.sleep(_MAINTAIN_INTERVAL)


def _quote(token: str) -> str:
    # nxsk strips quotes as pure grouping so quoting everything is safe and handles spaces
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
    """export chart (.json.gz) to output as mp4
    cover and bgm are optional files nxsk classifies by extension, a png cover and an audio bgm
    crf is the per-render libx264 quality from 0-51 where lower is better and larger
    default_settings starts from built-in defaults so a render doesn't inherit prior state
    """
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

    session = await _acquire()
    process = session.process
    assert process.stdin is not None and process.stdout is not None
    healthy = False
    try:
        process.stdin.write(request)
        await process.stdin.drain()
        while True:
            raw = await asyncio.wait_for(process.stdout.readline(), timeout)
            if not raw:
                raise ChartPreviewError(-1, "renderer closed unexpectedly")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("OK "):
                healthy = True  # in sync so keep the session
                session.renders += 1
                return output
            if line.startswith("ERR "):
                healthy = True  # clean reply still in sync
                session.renders += 1
                raise ChartPreviewError(1, line[4:])
    except (
        asyncio.TimeoutError,
        asyncio.CancelledError,
        BrokenPipeError,
        ConnectionResetError,
    ) as exc:
        # mid-render or broken pipe means the session is desynced so it's dropped with healthy
        # left False, otherwise the next request on it would read this one's late reply
        if isinstance(exc, asyncio.TimeoutError):
            raise ChartPreviewError(-1, f"render timed out after {timeout}s") from None
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ChartPreviewError(-1, "renderer pipe broke") from None
    finally:
        await _release(session, healthy)


async def start() -> None:
    """start the maintenance loop which warms sessions per the warm_source
    best-effort so if the binary is absent it does nothing and renders fall back"""
    global _maintain_task
    if not available():
        return
    if _maintain_task is None or _maintain_task.done():
        _maintain_task = asyncio.create_task(_maintain())


async def stop() -> None:
    """stop maintenance and shut every idle session down
    in-flight renders drop their own sessions when cancelled so this only sweeps the idle pool
    """
    global _maintain_task
    if _maintain_task is not None:
        _maintain_task.cancel()
        try:
            await _maintain_task
        except asyncio.CancelledError:
            pass
        _maintain_task = None
    async with _pool_lock:
        sessions = [s for s, _ in _idle]
        _idle.clear()
    for session in sessions:
        process = session.process
        if session.alive and process.stdin is not None:
            try:
                process.stdin.write(b"quit\n")
                await process.stdin.drain()
                await asyncio.wait_for(process.wait(), 5.0)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                await _kill(session)
        _slots.release()  # idle sessions hold a permit for life so free it now
