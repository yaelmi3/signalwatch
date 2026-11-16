# changedetection.io dependency checks

Date: **2026-11-16**. Result: **13/13 behavior groups passed**: nine core/API/webhook groups and four browser groups. This checks dependencies, not the proposed application.

## Setup

Docker Desktop 4.43.2 / Engine 28.3.2 ran Linux arm64 images with ordinary container privileges and isolated local fixtures, networks, and data volumes.

- Engine: `ghcr.io/dgtlmoon/changedetection.io:0.70.3`, build `d466b8bb0ad8c64ceec232809f5ef7683b1fce49`.
- Engine digest: `sha256:47b1e9cc3eff820e6022b655e3fcc948625f60c1770ae8b9c30aa3bf104dc255`.
- Browser: `dgtlmoon/sockpuppetbrowser:0.0.5`, Google Chrome 157.0.8103.50.
- Browser digest: `sha256:571ca4f44d4c423f0448ee360df38da3293f977d59a63618cdcef883599f0632`.

Private-address access was enabled only for local fixtures. Core startup's default sample watches were removed before controlled checks; browser setup omitted them. Notifications went only to the local receiver.

## Observed behavior

- The first stored copy was API-readable without an ordinary alert. An unchanged fetch stayed quiet. An edit saved a new version and sent a webhook.
- A failed webhook left its version readable. There was one failed send for revision C; the cumulative count of two included the earlier successful revision B. No retry arrived during the 15-second recovery window. The [packaged send loop](https://github.com/dgtlmoon/changedetection.io/blob/d466b8bb0ad8c64ceec232809f5ef7683b1fce49/changedetectionio/flask_app.py#L1664-L1733) logs failures without requeueing. The runtime result does not prove behavior beyond that window.
- Explicit timestamp reads kept old snapshots stable and returned the expected before/after diff.
- A title/paused-only PUT preserved filters, headers, fetch backend, and schedule. Arbitrary nested-field merging was not tested.
- A graceful engine restart with the same volume preserved watches, history, paused state, and tested settings.
- A source HTTP 500 produced a visible error without a false content version. Recovery cleared the error.
- Pausing stopped scheduled checks during the eight-second observation, but manual rechecks still ran. Deletion removed watch and history API access.
- With identical raw HTML, a JavaScript-only content change produced a browser version/diff while HTTP stayed unchanged. Browser outage was visible; HTTP fetching continued. Browser fetching recovered without restarting the engine.

## Integration cautions and limits

Webhook `timestamp_from`/`timestamp_to` rendered `None`; read history for exact versions. `last_checked` records attempt start, not success. Use completed `check_count` with error state and an application-owned last-success record. The first browser recovery assertion polled the wrong field. Correcting the harness resolved it; the initial attempt was preserved, with no dependency bug found.

No real competitor coverage, Trafilatura integration, application/LLM relevance, real Slack/email delivery, load, host-crash durability, or long-term reliability was tested. These results do not select a deployment release or establish production readiness.

Owned containers, networks, and volumes were removed. Cached images and unrelated resources were retained.
