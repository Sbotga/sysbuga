"""Run blocking operations safely off the event loop.

Two executors, picked by the kind of work:
- `executor` (threads): I/O and GIL-releasing native work (numpy/PIL, disk writes, rapidfuzz,
  pydantic) where a thread already lets the loop breathe.
- the process pool (`to_process_with_timeout`): pure-Python CPU work that would otherwise hold
  the GIL and stall the loop. Spawned (not forked) so forking this multithreaded process can't
  deadlock, and so it behaves identically on Windows and Linux.
"""

import asyncio
import multiprocessing
import signal
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial
from typing import Any, Callable

executor = ThreadPoolExecutor(max_workers=64)

_process_pool: "ProcessPoolExecutor | None" = None
_process_lock = threading.Lock()


def _worker_init() -> None:
    """Ask the kernel to SIGKILL this worker when its parent dies, so a bot crash / SIGKILL
    (where the pool is never shut down cleanly) can't leave orphaned worker processes.
    Linux-only; a no-op elsewhere."""
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            PR_SET_PDEATHSIG = 1
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(
                PR_SET_PDEATHSIG, signal.SIGKILL
            )
        except Exception:
            pass


def _new_process_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_worker_init,
    )


def _get_process_pool() -> ProcessPoolExecutor:
    """Lazily create the process pool on first use, so importing this module doesn't spawn
    workers (and so the workers only start once real CPU work is submitted).

    A `ProcessPoolExecutor` is permanently unusable once any worker dies unexpectedly, which would
    otherwise take down *every* CPU command until the bot restarts. So if the current pool is
    broken, we throw it away and build a fresh one - a single worker death self-heals.
    """
    global _process_pool
    with _process_lock:
        pool = _process_pool
        if pool is None or getattr(pool, "_broken", False):
            if pool is not None:
                print(
                    "[unblock] process pool broke (a worker died); recreating it",
                    file=sys.stderr,
                )
                pool.shutdown(wait=False, cancel_futures=True)
            pool = _new_process_pool()
            _process_pool = pool
        return pool


def shutdown() -> None:
    """Tear the process pool down (best-effort). atexit also handles this on a clean exit."""
    global _process_pool
    if _process_pool is not None:
        _process_pool.shutdown(wait=False, cancel_futures=True)
        _process_pool = None


def to_thread(func: Callable, *args, **kwargs) -> None:
    threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()


async def to_process_with_timeout(
    func: Callable, *args: Any, timeout: int = 20, **kwargs: Any
) -> Any:
    """Run `func(*args, **kwargs)` in a worker process, off the event loop entirely. `func`
    must be importable (module-level) and its args/return picklable. The timeout cancels the
    await, not the worker — a wedged call ties up a pool slot until it returns."""
    loop = asyncio.get_running_loop()
    for attempt in range(2):
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    _get_process_pool(), partial(func, *args, **kwargs)
                ),
                timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Function {func.__name__} timed out after {timeout} seconds"
            )
        except BrokenProcessPool:
            # a worker died mid-call - _get_process_pool rebuilds the pool, so retry once
            if attempt:
                raise
