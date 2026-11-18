from bisect import bisect_right
from threading import Event, Lock
from weakref import WeakValueDictionary

from signalwatch import changedetection, storage

_WATCH_LOCKS: WeakValueDictionary[str, Lock] = WeakValueDictionary()
_WATCH_LOCKS_GUARD = Lock()
_SQLITE_INTEGER_LIMIT = 2**63


def save_new_changes(page_id: int, watch_id: str, *, stop: Event | None = None) -> None:
    with _WATCH_LOCKS_GUARD:
        watch_lock = _WATCH_LOCKS.setdefault(watch_id, Lock())

    with watch_lock:
        if stop is not None and stop.is_set():
            return
        history = changedetection.read_history(watch_id)
        if history and (history[0] < -_SQLITE_INTEGER_LIMIT or history[-1] >= _SQLITE_INTEGER_LIMIT):
            raise changedetection.EngineError("The engine returned a watch version outside the supported range.")

        checkpoint = storage.latest_page_snapshot(page_id)
        before = None
        if checkpoint is not None:
            history = history[bisect_right(history, checkpoint.version) :]
            before = checkpoint.after

        for version in history:
            if stop is not None and stop.is_set():
                return
            after = changedetection.read_snapshot(watch_id, version)
            if not storage.save_page_version(
                page_id=page_id, watch_id=watch_id, version=version, before=before, after=after
            ):
                return
            before = after
