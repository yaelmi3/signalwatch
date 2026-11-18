# SignalWatch

Competitor content monitoring with audience-specific insights.

[Draft technical design](docs/technical_design_records/0001_signalwatch.md)

FastAPI serves the compiled React frontend and API docs from one origin.

## Local setup

Use uv, Python 3.13, Node.js 24, and pnpm 11. Run from the repository root:

```sh
uv sync
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
uv run uvicorn signalwatch.main:app --reload
```

Open [SignalWatch](http://127.0.0.1:8000/) or [the API docs](http://127.0.0.1:8000/docs).
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

## Analyze saved changes

Set `OPENAI_API_KEY` and both model variables in the worker's environment. Both models must support structured outputs.
Use `SIGNALWATCH_ANALYSIS_MODEL` for **Economy** and `SIGNALWATCH_ANALYSIS_HIGHER_QUALITY_MODEL` for **Higher quality**.
Run one worker alongside the app. If you override `SIGNALWATCH_DB_PATH`, use the same value in both processes.

```sh
uv run python -m signalwatch.analysis_worker
```

The worker uses DBOS with SQLite and runs up to two jobs at once. It picks up saved edits at startup and while the app
is running. It sends page text to OpenAI to explain the edit and judge whether it matches the interests saved for
Marketing, Product, and R&D. In **Configuration**, set each team's interests and add optional shared context in
**What matters to us**. A blank team field holds that team's updates.
Open **Maintainer settings** under **Configuration** to choose **Analysis quality**. Economy is the default.
Your choice applies to upcoming page analyses.
Open **Digest** for updates that match at least one team. Each match includes a short reason and a source link.
Select an edit to open its saved details. **Maintainer settings** links to **Held items** for skipped updates and
analysis status.

For a source configured with the Hacker News homepage, the worker checks its RSS feed at startup and every five
minutes. The first feed establishes which URLs are already seen. The worker evaluates new articles from the original
publisher page once per URL. Relevant articles appear in **Digest** with a direct **Open article** link.

Back up the app database and the DBOS database together to preserve both the results and unfinished jobs.

## Checks

After building the frontend:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
