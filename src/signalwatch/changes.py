from signalwatch import changedetection, storage


def save_new_changes(page_id: int, watch_id: str) -> None:
    history = changedetection.read_history(watch_id)
    checkpoint = storage.latest_page_change(page_id)
    if checkpoint is None:
        if len(history) < 2:
            return
        baseline = history[0]
        before = changedetection.read_snapshot(watch_id, baseline)
    else:
        baseline = int(checkpoint.version)
        before = checkpoint.after

    for version in history:
        if version <= baseline:
            continue
        after = changedetection.read_snapshot(watch_id, version)
        change = storage.PageChange(version=str(version), before=before, after=after)
        storage.save_page_change(page_id=page_id, watch_id=watch_id, change=change)
        before = after
