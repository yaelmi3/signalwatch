# SignalWatch design

SignalWatch watches competitor sites for new articles and quiet page edits. It tells Marketing, Product, and R&D what changed and why it matters to them. Business users add competitors with watched pages and optional RSS feeds and set team interests in the web app. They can edit a feed on its source card. Maintainers have a separate screen for feeds, Slack, analysis, and source health.

## Why we chose these tools

**[changedetection.io](https://github.com/dgtlmoon/changedetection.io)** already handles scheduled checks, keeps page history, and lets us read saved copies through an API. We reused it to avoid writing and maintaining a page monitoring engine. It checks each configured page on a schedule, saves changed versions, and notifies SignalWatch when it finds a change.
We started with HTTP fetching to keep the pilot lightweight, and monitoring JavaScript-rendered pages would require adding a browser service and configuring changedetection.io to use its Playwright fetch backend.

**SQLite** fits one host with one background worker and modest write traffic. It saves state on a persistent disk and needs no database server. The Python app stores settings, results, and feedback in one SQLite file. DBOS, the library that runs background jobs, saves progress in a second file so work can resume after a restart. We considered PostgreSQL with PgQueuer and Temporal. Both would add a service to operate.

## How a competitor update reaches a team

1. **Collect.** The worker checks configured RSS 2.0 feeds every 30 minutes. The first successful check records the articles already there so they do not trigger alerts. Later checks pick up new article links, and Trafilatura extracts the text from the publisher's page. For page edits, SignalWatch reads the saved before-and-after versions from changedetection.io. It skips page versions and article links it has already saved for that watch or feed.
2. **Decide.** The worker asks the model to check the saved evidence against each team's interests. For articles, it checks proposed matches against supporting quotes. It saves each team's decision and the reason. For example, a post about AsterShield's CI integration matched R&D and was held for Marketing and Product.
3. **Deliver.** The Digest shows matching updates with their source links and reasons. Slack gets one message per matching channel. Maintainers can inspect items that were held and see why each team passed.

## How quality improves

Readers can flag a poor alert or report a missed update. For feedback on a saved article, a maintainer can compare a suggested change to a team's interests against up to three saved articles. Applying it changes future decisions. In one R&D test, the proposed change produced the expected decisions on two labeled articles. Measuring alert quality across all three teams is still open.

## What happens when something fails

- **A source or model call fails.** Feed checks try again on the next poll. The worker retries temporary analysis errors and records failures when it runs out of retries. A separate cloud monitor emails the maintainer if the app is unreachable or source checks go stale. In staging, feeds go stale after two hours and page checks after four hours. Maintainer settings shows which source needs attention.
- **Slack fails or times out.** SignalWatch records one send attempt per item and channel, including failed or uncertain outcomes. The maintainer checks the channel before resending, since Slack may have accepted the message even if its reply was lost.
- **A service or the host goes down.** Docker restarts exited services. On the same disk and release, DBOS can resume saved jobs. If the page detector kept running, SignalWatch catches up from the versions it still holds and flags a gap when its last saved version is missing. RSS can catch up on entries still in the feed. A full host outage pauses collection. Changes the detector never saw and articles that have already dropped off the feed can be missed.

## How we deploy and recover

Docker Compose runs the app, worker, and changedetection.io in separate containers on the same VM. The detector uses a pinned image and keeps watch history on the persistent disk. It checks websites through an outbound proxy and sends change notifications directly to the app over Docker's internal network. Compose waits for the detector to pass its health check before starting the app. [Compose configuration](../../compose.yaml)

GitHub Actions builds the exact revision pushed to `main` and deploys it to a single Google Cloud VM. The deploy stops the worker, checks for unfinished jobs, backs up both databases, and waits for health checks. If jobs are unfinished, the operator finishes them with the old release first. If a rollout fails after switching versions, the operator can use the last healthy revision and restore matching data when needed. A full restore requires both databases and the detector history. Restoring a backup discards changes made after it. [Deployment and recovery runbook](../deploy-staging.md)

## Where the design stops

More sources mean more fetches, more topics give the model more to evaluate, and more channels mean more sends. The worker runs up to two analysis jobs at a time. Queue delay, source freshness, and model cost would guide when to add capacity or move to PostgreSQL for multiple hosts. Article discovery is limited to configured RSS feeds. For a business handoff, the remaining work is private access with separate roles, off-host backups with a tested restore, and an owner for credentials and alerts.

Automatic retention and cleanup are future work. Long-running deployments need retention policies for detector snapshots, application history, article evidence, completed DBOS workflows, and backups. Cleanup must preserve change-detection checkpoints, article deduplication, unfinished workflows, and evidence needed to evaluate analysis quality.
