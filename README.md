# SignalWatch

Competitor content monitoring with audience-specific insights.

[Draft technical design](docs/technical_design_records/0001_signalwatch.md)

[Staging deployment](docs/deploy-staging.md)

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

### Team feedback and replay

On a private, access-controlled installation, set `SIGNALWATCH_FEEDBACK_ORIGIN` to the exact browser origin
(for example, `http://127.0.0.1:8001`). Use HTTPS or localhost. Set it in the app environment, or in the Compose
environment before starting the app. Only enable this on an installation whose private ingress already restricts
access. Leave it unset on the public demo.

Use **Give feedback** on a saved update and pick the team. Use **Report a missed update** if the update is absent
from both lists. **Maintainer settings** shows recent reports and a recorded comparison. Confirm the team's
expected decision before turning a report into a replay example.

Run `uv run python -m signalwatch.replay < examples.json` with a configured `OPENAI_API_KEY`. Each article needs the
saved publisher text consumed by the scorer, up to 12,000 characters. The command compares one team's two notes
on that text and prints the expected decision, both decisions, and both reasons. Treat text recovered later as a
reconstruction. The command runs outside the worker and makes no writes to the app database or Slack.

```json
{
  "model": "gpt-5.4-mini",
  "team": "research_and_development",
  "interests": {
    "business_context": "We build AI security tools.",
    "research_and_development": "API or integration changes."
  },
  "proposed_interest": "API changes that add or strengthen AI security protections.",
  "articles": [
    {"title": "Solver update", "publisher_text": "The release adds an API for matrix optimization.", "expected": "hold"}
  ]
}
```

### Article discovery

Open **Maintainer settings → Article discovery** and give a source a public RSS 2.0 feed URL. The worker checks the feed
at startup and every five minutes. The first successful poll records the baseline, even when no articles are eligible.
Later discoveries are analyzed from the original publisher page. Relevant articles link directly to the publisher,
and Held shows why each team passed on an analyzed article.

RSS discovery runs alongside watched-page analysis. Saved edits to any watched page go through the same team relevance
check. Clearing the feed URL stops discovery and preserves the source's article history.

### Optional Slack delivery

Invite the Slack bot to a sandbox channel. For OAuth setup, add the `chat:write`, `channels:read`, and `groups:read`
bot scopes. Register the exact HTTPS URL ending in `/api/maintainer/slack/callback` in Slack. This integration uses
workspace bot tokens with token rotation off. Exclude the callback query string from proxy and hosting access logs.

Set these server variables on the app and worker:

| Variable | Value |
| --- | --- |
| `SIGNALWATCH_SLACK_CLIENT_ID` | Slack app client ID |
| `SIGNALWATCH_SLACK_CLIENT_SECRET` | Slack app client secret |
| `SIGNALWATCH_SLACK_REDIRECT_URL` | Exact registered HTTPS callback URL |
| `SIGNALWATCH_SLACK_MAINTAINER_PASSWORD` | Strong random passphrase for Slack setup |
| `SIGNALWATCH_SLACK_ENCRYPTION_KEY` | Private Fernet key, generated with `cryptography.fernet.Fernet.generate_key()` |

In **Maintainer settings → Slack delivery**, use the setup passphrase to connect a workspace. Once connected, anyone
with access to SignalWatch can select public Slack channels for Marketing, Product, and R&D, send test messages,
and enable delivery. Teams can share a channel, and each item posts once per matching channel with the reasons
for its assigned teams. Leave a team unassigned to skip its Slack notifications. The worker picks up saved settings
while running. Keep the encryption key separate from SQLite backups. Changing the key requires reconnecting Slack.

For environment-only delivery, leave the five OAuth variables unset and set `SIGNALWATCH_SLACK_BOT_TOKEN` and
`SIGNALWATCH_SLACK_CHANNEL_ID` on the worker with the bot's `chat:write` scope and channel membership. This uses the
same channel for all teams. Leave all Slack variables unset to disable delivery. Only newly completed analyses that
qualify for Digest create a delivery.
Pending posts keep their original channel and include teams still assigned to it when delivery is enabled.

The worker records one send attempt per item and channel. Rejections, rate limits, and uncertain outcomes are logged and
recorded in `slack_deliveries`. Review the recorded error and check the channel before resending manually.
A timeout or interruption can leave the result uncertain even if Slack accepted the message.

### Worker upgrades and retention

Before changing DBOS or workflow code, stop the worker and check its system database for unfinished jobs. Replace the
example path with the absolute path to your app database with `.analysis.sqlite3` appended.

```sh
uv run dbos workflow list \
  --sys-db-url 'sqlite:///file:/absolute/path/configuration.sqlite3.analysis.sqlite3?uri=true' \
  --status ENQUEUED --status PENDING --status DELAYED --status SUSPENDED
```

If the list contains jobs, use the previous release to finish or recover them, then stop the worker and check again.
Back up both SQLite databases before starting the new release. The app can keep saving page changes while the worker
is stopped.

Deleting a page or source removes its history from the app and its watches from changedetection. Saved excerpts can
remain in DBOS checkpoints. Manage retention for the worker database and its backups separately from app deletion.

## Checks

After building the frontend:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
