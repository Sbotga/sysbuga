"""Run blocking operations safely off the event loop."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

executor = ThreadPoolExecutor(max_workers=64)


def to_thread(func: Callable, *args, **kwargs) -> None:
    threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()


async def to_process_with_timeout(
    func: Callable, *args: Any, timeout: int = 20, **kwargs: Any
) -> Any:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, lambda: func(*args, **kwargs)),
            timeout,
        )
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"Function {func.__name__} timed out after {timeout} seconds"
        )
