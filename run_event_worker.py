"""Standalone minutely event-data worker (a separate process from the bot, like run_activity).

Every minute it force-fetches each region's current event (top 100 + borders) and writes it via
services.event_store, which the bot's /event commands read from. Running it apart from the bot
means bot restarts and maintenance don't interrupt data collection, and it starts fast (no discord
or cog imports). Both processes must run; they share state only through the files under data/cache
and event_saves.

    python run_event_worker.py
"""

import asyncio
import logging
import signal
import time

from helpers.config_loader import get_config, set_config_path
from services import event_store
from services.sbuga import SbugaClient

POLL_SECONDS = 60
FETCH_RETRIES = 3  # a missed minute leaves a real gap in the heatmap, so retry hard
RETRY_DELAY = 3.0
FETCH_TIMEOUT = (
    45.0  # a live leaderboard fetch is slow; give it room but still retry if it hangs
)

logger = logging.getLogger("event-worker")


class EventWorker:
    def __init__(self, client: SbugaClient) -> None:
        self.client = client
        self._last_event: dict[str, int] = {}  # region -> last seen event id
        self._did_startup_sweep = False

    async def _fetch_current(self, region: str):
        """fetch this region's current event, retrying on failure. each failure is a warning
        (a missed minute is a permanent hole in the data). None if every attempt failed.
        """
        for attempt in range(1, FETCH_RETRIES + 1):
            try:
                return await asyncio.wait_for(
                    self.client.get_current_event(region, fresh=True), FETCH_TIMEOUT
                )
            except Exception as exc:
                logger.warning(
                    "[%s] fetch failed (attempt %d/%d): %r",
                    region,
                    attempt,
                    FETCH_RETRIES,
                    exc,
                )
                if attempt < FETCH_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)
        return None

    async def _poll_region(self, region: str) -> None:
        data = await self._fetch_current(region)
        if data is None:
            return  # every retry failed - keep the last good file for this region
        try:
            await asyncio.to_thread(event_store.store_current_event, region, data)
        except Exception as exc:
            logger.warning("[%s] store failed: %r", region, exc)

        event_id = data.event_id
        if event_id is None:
            return
        previous = self._last_event.get(region)
        self._last_event[region] = event_id
        # a new event started - archive the previous one's files in the background so the poll
        # never blocks on a long high-level zstd pass
        if previous is not None and previous != event_id:
            asyncio.create_task(self._compress_past(region, previous))

    async def _compress_past(self, region: str, event_id: int) -> None:
        try:
            await asyncio.to_thread(
                event_store.compress_event_dir,
                event_store.event_save_dir(region, event_id),
            )
        except Exception:
            pass  # the startup sweep / retro script will finish it later

    async def run(self) -> None:
        while True:
            try:
                await asyncio.gather(
                    *(self._poll_region(r) for r in event_store.EVENT_REGIONS),
                    return_exceptions=True,
                )
                # after the first round every current event's folder exists on disk, so a sweep can
                # safely archive everything older that a previous run left uncompressed (a crash, or
                # a restart mid event-transition) without touching a live event
                if not self._did_startup_sweep:
                    self._did_startup_sweep = True
                    asyncio.create_task(
                        asyncio.to_thread(event_store.compress_stale_event_saves)
                    )
            except Exception as exc:  # never let a stray error stop the loop
                logger.warning("iteration failed: %r", exc)
            # align every poll to the top of the wall-clock minute (:00), not to when the
            # service happened to start, so snapshots land on consistent minute boundaries
            await asyncio.sleep(POLL_SECONDS - (time.time() % POLL_SECONDS))


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [event-worker] %(message)s",
    )
    set_config_path("config.yml")
    scfg = get_config()["sbuga"]
    client = SbugaClient(
        scfg["api_url"],
        image_type=scfg["image_type"],
        bot_token=scfg.get("bot_token", ""),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # windows has no signal handlers on the loop

    worker = asyncio.create_task(EventWorker(client).run())
    print("[event-worker] started", flush=True)
    await stop.wait()

    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    await client.close()
    print("[event-worker] stopped", flush=True)


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
