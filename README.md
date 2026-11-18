# SignalWatch

Competitor content monitoring with audience-specific insights.

[Draft technical design](docs/technical_design_records/0001_signalwatch.md)

The starter serves a React greeting page. FastAPI serves the compiled Vite frontend and API docs from one origin.

## Local setup

Use uv, Python 3.13, Node.js 24, and pnpm 11. Run from the repository root:

```sh
uv sync
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
uv run uvicorn signalwatch.main:app --reload
```

Open [the greeting page](http://127.0.0.1:8000/) or [the API docs](http://127.0.0.1:8000/docs).
Rebuild the frontend after frontend changes. The frontend build must exist before starting FastAPI or running tests.
Run one SignalWatch process so the webhook receiver and catch-up timer share the same per-watch locks.

## Connect changedetection

Set these variables on SignalWatch before adding a page.

| Variable | Value |
| --- | --- |
| `CHANGEDETECTION_URL` | Base URL of the changedetection instance |
| `CHANGEDETECTION_API_KEY` | Key from changedetection’s API settings |
| `SIGNALWATCH_WEBHOOK_URL` | Full callback URL reachable from changedetection, ending in `/api/webhooks/changedetection` |
| `CHANGEDETECTION_WEBHOOK_SECRET` | A URL-safe random token shared with changedetection |

Use a hostname dedicated to the callback. Use HTTPS outside a trusted local network. Give the changedetection process a
private, read-only `netrc` file and set its `NETRC` variable to that file's path. The entry uses the callback hostname
without a port and the same token as SignalWatch.

```text
machine <callback-host>
  login signalwatch
  password <shared-token>
```

SignalWatch configures new watches automatically. The credential stays in the private file on the engine. If a page is
pending, finish setup and click **Retry connection**. Maintainer settings checks the format of SignalWatch's webhook
settings.

For existing watches configured by an earlier version, rotate the old token and replace only the SignalWatch notification
URL in changedetection with the credential-free callback. Use `jsons://` for HTTPS or `json://` for local HTTP. Keep the
notification body `{{watch_uuid}}`. The old token may be in engine logs and backups.

## Checks

After building the frontend:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
