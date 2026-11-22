import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from threading import Event

import structlog
from fastapi import FastAPI

from signalwatch import changedetection, storage
from signalwatch.changes import save_new_changes

_INTERVAL_ENV = "SIGNALWATCH_CATCHUP_INTERVAL_SECONDS"
_DEFAULT_INTERVAL_SECONDS = 60
catchup_logger = structlog.get_logger()


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
        catchup_logger.warning("Could not list watches for page change catch-up.", error_type=type(error).__name__)
        return

    observed: dict[int, changedetection.WatchStatus] = {}
    for watch in watches:
        if stop.is_set():
            return
        try:
            await asyncio.to_thread(save_new_changes, watch.page_id, watch.watch_id, stop=stop)
            if stop.is_set():
                return
            observed[watch.page_id] = await asyncio.to_thread(changedetection.read_watch, watch.watch_id)
        except (changedetection.EngineError, sqlite3.OperationalError, OSError) as error:
            catchup_logger.warning(
                "Could not catch up page changes.", page_id=watch.page_id, error_type=type(error).__name__
            )

    if not watches or stop.is_set():
        return
    try:
        system = await asyncio.to_thread(changedetection.read_system_info)
    except changedetection.EngineError as error:
        catchup_logger.warning("Could not read watch system information.", error_type=type(error).__name__)
        return

    running = {item.uuid for item in system.checking_now}
    for watch in watches:
        if stop.is_set():
            return
        status = observed.get(watch.page_id)
        try:
            if system.all_paused:
                await asyncio.to_thread(storage.record_page_check, watch, None, None, True)
            elif status is not None:
                await asyncio.to_thread(_record_watch_health, watch, status, watch.watch_id in running)
        except (changedetection.EngineError, sqlite3.OperationalError, OSError) as error:
            catchup_logger.warning(
                "Could not record page monitoring status.", page_id=watch.page_id, error_type=type(error).__name__
            )


def _record_watch_health(watch: storage.LinkedWatch, status: changedetection.WatchStatus, running: bool) -> None:
    completed_count = None
    success_at = None
    # The start time can belong to a newer fetch than the completed count.
    # Require unchanged status on both sides of the idle observation.
    if (
        not running
        and not status.paused
        and status.check_count is not None
        and status.check_count != watch.check_count
        and changedetection.read_watch(watch.watch_id) == status
    ):
        completed_count = status.check_count
        if completed_count > 0 and not status.error:
            success_at = status.last_checked
    storage.record_page_check(watch, completed_count, success_at, bool(status.paused))
