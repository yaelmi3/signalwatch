import asyncio
import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from threading import Event

from fastapi import FastAPI

from signalwatch import changedetection, storage
from signalwatch.changes import save_new_changes

_INTERVAL_ENV = "SIGNALWATCH_CATCHUP_INTERVAL_SECONDS"
_DEFAULT_INTERVAL_SECONDS = 60
_LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def catchup_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    interval = _catchup_interval()
    if interval == 0:
        yield
        return

    stop = Event()
    shutdown = asyncio.Event()
    task = asyncio.create_task(_run_catchup(interval=interval, shutdown=shutdown, stop=stop))
    try:
        yield
    finally:
        stop.set()
        shutdown.set()
        await task


def _catchup_interval() -> int:
    error = f"{_INTERVAL_ENV} must be a non-negative integer."
    try:
        interval = int(os.getenv(_INTERVAL_ENV, str(_DEFAULT_INTERVAL_SECONDS)))
    except ValueError:
        raise ValueError(error) from None
    if interval < 0:
        raise ValueError(error)
    return interval


async def _run_catchup(*, interval: int, shutdown: asyncio.Event, stop: Event) -> None:
    while not stop.is_set():
        await _catch_up_once(stop)
        with suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval)


async def _catch_up_once(stop: Event) -> None:
    try:
        watches = await asyncio.to_thread(storage.list_linked_watches)
    except (sqlite3.OperationalError, OSError) as error:
        _LOGGER.warning("Could not list watches for page change catch-up.", extra={"error_type": type(error).__name__})
        return

    for watch in watches:
        if stop.is_set():
            return
        try:
            await asyncio.to_thread(save_new_changes, watch.page_id, watch.watch_id, stop=stop)
        except (changedetection.EngineError, sqlite3.OperationalError, OSError) as error:
            _LOGGER.warning(
                "Could not catch up page changes.", extra={"page_id": watch.page_id, "error_type": type(error).__name__}
            )
