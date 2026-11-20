#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

[[ $# == 2 && $1 =~ ^[a-f0-9]{40}$ ]] || fail "Usage: sudo bash deploy.sh <full git SHA> <upload directory>"
[[ $EUID == 0 ]] || fail "Run the rollout as root."
release=$1
upload=$(realpath -e -- "$2")
root=/opt/signalwatch
export SIGNALWATCH_IMAGE="signalwatch:$release"
export SIGNALWATCH_STATE_DIR=/var/lib/signalwatch

[[ -f "$upload/compose.yaml" && -f "$upload/image.tar.gz" ]] || fail "Release upload is incomplete."
mountpoint -q -- "$SIGNALWATCH_STATE_DIR" || fail "Mount the persistent disk at $SIGNALWATCH_STATE_DIR first."
[[ -f "$root/staging.env" && $(stat -Lc '%u:%a' "$root/staging.env") == 0:600 ]] ||
    fail "Create the root-owned staging.env with mode 0600 in $root."
[[ -f "$root/changedetection.netrc" && $(stat -Lc '%u:%a' "$root/changedetection.netrc") =~ ^0:(400|600)$ ]] ||
    fail "Create a private root-owned changedetection.netrc in $root."

exec 9>"$root/deploy.lock"
flock -n 9 || fail "Another SignalWatch rollout is running."

release_dir="$root/releases/$release"
compose=(docker compose --project-name signalwatch --env-file "$root/staging.env")
if ! "${compose[@]}" --file "$upload/compose.yaml" config --quiet >/dev/null 2>&1; then
    fail "Compose validation failed. Check staging.env and the release Compose file on the VM."
fi
loaded=$(gzip -dc -- "$upload/image.tar.gz" | docker load)
grep -Fxq "Loaded image: $SIGNALWATCH_IMAGE" <<< "$loaded" ||
    fail "The release archive did not load $SIGNALWATCH_IMAGE."
[[ $(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$SIGNALWATCH_IMAGE") == linux/amd64 ]] ||
    fail "Expected the loaded linux/amd64 image $SIGNALWATCH_IMAGE."
docker run --rm --network none "$SIGNALWATCH_IMAGE" python -c '
import sqlite3
import signalwatch.main, signalwatch.analysis_worker

with sqlite3.connect(":memory:") as db:
    if db.execute("SELECT unixepoch(?)", ("subsec",)).fetchone()[0] is None:
        raise SystemExit("The release image SQLite does not support DBOS timestamps.")
'

install -d -m 0750 "$release_dir"
install -m 0644 "$upload/compose.yaml" "$release_dir/compose.yaml.new"
mv -f -- "$release_dir/compose.yaml.new" "$release_dir/compose.yaml"
compose+=(--file "$release_dir/compose.yaml")
"${compose[@]}" pull --policy missing changedetection

install -d -o 10001 -g 10001 -m 0700 "$SIGNALWATCH_STATE_DIR/app"
install -d -m 0700 "$SIGNALWATCH_STATE_DIR/changedetection" "$SIGNALWATCH_STATE_DIR/backups"

worker=$("${compose[@]}" ps --all --quiet worker)
restore_worker=
switch_started=false
restore_before_switch() {
    local result=$?
    if ((result != 0)) && [[ $switch_started == false && -n $restore_worker ]]; then
        docker start "$restore_worker" >/dev/null ||
            printf 'Could not restart the original worker %s.\n' "$restore_worker" >&2
    fi
}
trap restore_before_switch EXIT
if [[ -n $worker ]]; then
    worker_running=$(docker inspect --format '{{.State.Running}}' "$worker")
    if [[ $worker_running == true ]]; then
        restore_worker=$worker
        docker stop --time 90 "$worker"
        [[ $(docker inspect --format '{{.State.ExitCode}}' "$worker") == 0 ]] ||
            fail "The original worker did not stop cleanly."
    fi
fi

timeout 120s python3 - "$SIGNALWATCH_STATE_DIR" "$release" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile

state = Path(sys.argv[1])
app_db = state / "app" / "configuration.sqlite3"
worker_db = app_db.with_name("configuration.sqlite3.analysis.sqlite3")

if worker_db.exists():
    with closing(sqlite3.connect(worker_db.as_uri() + "?mode=ro", uri=True)) as db:
        active = db.execute(
            """SELECT status, count(*) FROM workflow_status
            WHERE status IN ('ENQUEUED', 'PENDING', 'DELAYED', 'SUSPENDED')
            GROUP BY status ORDER BY status"""
        ).fetchall()
    if active:
        counts = ", ".join(f"{status}={count}" for status, count in active)
        raise SystemExit(f"Unfinished DBOS jobs ({counts}). Drain them with the current worker and rerun the rollout.")

databases = [db for db in (app_db, worker_db) if db.exists()]
if databases:
    backup = Path(tempfile.mkdtemp(prefix=f"{sys.argv[2]}.", dir=state / "backups"))
    for db in databases:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup / db.name)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise SystemExit(f"SQLite backup failed its integrity check: {db.name}")
    print(f"SQLite backups saved to {backup}", flush=True)
PY

switch_started=true
if ! "${compose[@]}" up --detach --pull never --wait --wait-timeout 120; then
    "${compose[@]}" ps --all || true
    fail "Release $release did not become healthy. See the service status and inspect before rolling back."
fi

printf '%s\n' "$release" > "$root/current-sha.new"
mv -f -- "$root/current-sha.new" "$root/current-sha"
printf 'SignalWatch release %s is healthy.\n' "$release"
