import { StrictMode, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import "./maintainer.css";

type Page = {
  id: number;
  competitor_id: number;
  url: string;
  watch_id: string | null;
  state: "pending" | "creating" | "linked" | "attention";
  error: string | null;
};
type Competitor = { id: number; name: string; website: string; pages: Page[] };
type PageChange = { version: string; before: string; after: string };
type TeamDecision = { relevant: boolean; reason: string; quote: string | null };
type ChangeAnalysisResult = {
  summary: string;
  marketing: TeamDecision;
  product: TeamDecision;
  research_and_development: TeamDecision;
};
type PageChangeAnalysis = {
  status: "queued" | "completed" | "failed";
  result: ChangeAnalysisResult | null;
};
type WebhookStatus = { configured: boolean };
type SlackStatus = {
  available: boolean;
  authenticated: boolean;
  connected: boolean;
  workspace_name: string | null;
  team_channels: SlackTeamChannels;
  enabled: boolean;
  revision: string | null;
};
type SlackChannel = { id: string; name: string };
type AnalysisSettings = { quality: "economy" | "higher_quality" };
type ArticleFeedSettings = {
  source_id: number;
  source_name: string;
  url: string | null;
};
type StaleSource = {
  id: number;
  kind: "page" | "feed";
  url: string;
  source_name: string;
  last_success_at: number | null;
};
type WatchStatus = {
  available: boolean;
  paused: boolean | null;
  last_checked: number | null;
  check_count: number | null;
  error: string | null;
};
const pageStates = {
  pending: "Watch pending", creating: "Creation unconfirmed — needs attention",
  linked: "Watch connected", attention: "Needs attention",
};
const analysisTeams = [
  ["marketing", "Marketing"],
  ["product", "Product"],
  ["research_and_development", "R&D"],
] as const;
type TeamKey = (typeof analysisTeams)[number][0];
type SlackTeamChannels = Partial<Record<TeamKey, string>>;
type TeamInterests = Record<TeamKey | "business_context", string>;
type DigestEntry = {
  version: string;
  source_name: string;
  status: PageChangeAnalysis["status"] | "pending";
  summary: string | null;
  relevant_teams: TeamKey[];
  team_reasons: Partial<Record<TeamKey, string>>;
} & (
  | { kind?: never; page_id: number; page_url: string }
  | { kind: "article"; title: string; article_url: string; source_id: number; hold_reason?: "business_fit_unverified" }
);
const digestAnalysisStatus = {
  pending: "Analysis pending.",
  queued: "Analysis queued.",
  completed: "Analysis summary unavailable.",
  failed: "Analysis failed.",
} satisfies Record<DigestEntry["status"], string>;
const businessFitUnverifiedReason =
  "Held because no single change was verified to match both your business and team interests.";
const configurationPath = "/?view=configuration";
const articleFeedsPath = "/api/maintainer/article-feeds";
const slackPath = "/api/maintainer/slack";
const summarySentences = new Intl.Segmenter(undefined, { granularity: "sentence" });
const demoVideoUrl = "https://storage.googleapis.com/signalwatch-staging-007321-demo/signalwatch-demo.mp4";

function request<T>(path: string, options?: RequestInit, readHeaders?: (headers: Headers) => void): Promise<T>;
async function request(path: string, options?: RequestInit, readHeaders?: (headers: Headers) => void): Promise<unknown> {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = body && typeof body === "object" && "detail" in body ? body.detail : null;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status}).`);
  }
  if (response.status === 204) return;
  if (options?.method === "DELETE") throw new Error(`Unexpected deletion response (${response.status}).`);
  const body: unknown = await response.json();
  readHeaders?.(response.headers);
  return body;
}

function post<T>(path: string, body?: object) {
  return request<T>(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}

function message(error: unknown) {
  return error instanceof Error ? error.message : "The request could not be completed. Please try again.";
}

function ChangeTime({ version }: { version: string }) {
  const date = new Date(Number(version) * 1000);
  if (Number.isNaN(date.getTime())) return <span>Version {version}</span>;
  return (
    <time className="change-time" dateTime={date.toISOString()}>
      <span>{date.toLocaleDateString(undefined, { dateStyle: "medium" })}</span>
      <span className="muted">{date.toLocaleTimeString(undefined, { timeStyle: "short" })}</span>
    </time>
  );
}

function changeHeadline(summary: string) {
  return summarySentences.segment(summary.trim()).containing(0)?.segment.trim() ?? summary;
}

function TeamTakeaways({ result }: { result: ChangeAnalysisResult }) {
  return (
    <>
      <h2>Team takeaways</h2>
      <ul className="team-takeaways">
        {analysisTeams.map(([key, name]) => {
          const decision = result[key];
          return (
            <li key={key}>
              <strong>{name}</strong>
              <span>{decision.reason}</span>
              <span className={decision.relevant ? "team-status relevant" : "team-status"}>
                {decision.relevant ? "Relevant" : "Held"}
              </span>
            </li>
          );
        })}
      </ul>
    </>
  );
}

function RawComparison({ change, result }: { change: PageChange; result?: ChangeAnalysisResult | null }) {
  return (
    <details className="raw-comparison">
      <summary>View raw comparison</summary>
      {result && analysisTeams.map(([key, name]) => {
        const quote = result[key].quote;
        return quote && <blockquote key={key}><strong>{name}: </strong>{quote}</blockquote>;
      })}
      <dl className="snapshots">
        <div><dt>Before</dt><dd><pre>{change.before}</pre></dd></div>
        <div><dt>After</dt><dd><pre>{change.after}</pre></dd></div>
      </dl>
    </details>
  );
}

function ChangeHistory({ pageId, pageUrl, requestedVersion }: {
  pageId: number; pageUrl: string; requestedVersion: string | null;
}) {
  const [history, setHistory] = useState<{ changes: PageChange[]; historyGap: boolean } | null>(null);
  const [selectedVersion, setSelectedVersion] = useState(requestedVersion);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [analyses, setAnalyses] = useState<Record<string, PageChangeAnalysis> | null>(null);
  const [analysisError, setAnalysisError] = useState(false);
  const changes = history?.changes ?? null;
  const selected = selectedVersion === null ? changes?.[0] : changes?.find((change) => change.version === selectedVersion);
  const analysis = selected && analyses?.[selected.version];
  const result = analysis?.status === "completed" ? analysis.result : null;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");

    async function refresh() {
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
      try {
        let historyGap = false;
        const changes = await request<PageChange[]>(`/api/pages/${pageId}/changes`, { signal }, (headers) => {
          historyGap = headers.get("X-SignalWatch-History-Gap") === "true";
        });
        if (!controller.signal.aborted) {
          setHistory({ changes, historyGap });
          setSelectedVersion((version) =>
            version === requestedVersion || changes.some((change) => change.version === version) ? version : null,
          );
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(message(cause));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    void refresh();
    return () => controller.abort();
  }, [pageId, requestedVersion, loadAttempt]);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    setAnalysisError(false);
    request<Record<string, PageChangeAnalysis>>(`/api/pages/${pageId}/analysis`, { signal })
      .then((analyses) => { if (!controller.signal.aborted) setAnalyses(analyses); })
      .catch(() => { if (!controller.signal.aborted) setAnalysisError(true); });
    return () => controller.abort();
  }, [pageId, loadAttempt]);

  return (
    <div className="history-layout">
      <div className="history-sidebar panel">
        <h2>Change history</h2>
        <button type="button" disabled={loading} onClick={() => setLoadAttempt((attempt) => attempt + 1)}>
          Refresh changes
        </button>
        {error && <p role="alert">{error}</p>}
        {history?.historyGap && <p role="status">Possible history gap</p>}
        {loading && <p role="status">{changes === null ? "Loading changes…" : "Refreshing changes…"}</p>}
        {!loading && !error && !selected && (
          <p role="status">{selectedVersion !== null
            ? "This saved edit is no longer available."
            : "No changes recorded yet."}</p>
        )}
        {changes && changes.length > 0 && (
          <ul className="history-versions" aria-label="Saved changes">
            {changes.map((change, index) => (
              <li key={change.version}>
                <button type="button" aria-pressed={change.version === selected?.version}
                  onClick={() => setSelectedVersion(index === 0 ? null : change.version)}>
                  <strong className="history-version-title">
                    {changeHeadline(analyses?.[change.version]?.result?.summary ?? "Page changed")}
                  </strong>
                  <ChangeTime version={change.version} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      {selected && (
        <article className="history-detail panel" aria-label="Selected change">
          <div className="history-meta">
            <ChangeTime version={selected.version} />
            <a href={pageUrl}>Open source page</a>
          </div>
          {analysisError && (
            <p role="alert">Could not load change analysis. Use Refresh changes to try again.</p>
          )}
          {result ? (
            <>
              <h2 className="change-summary-title">Summary</h2>
              <p className="change-summary">{result.summary}</p>
            </>
          ) : !analysisError && (
            <p role="status">
              {analyses === null ? "Loading analysis…" : analysis?.status === "failed"
                ? "Analysis failed for this edit." : "Analysis pending."}
            </p>
          )}
          {result && <TeamTakeaways result={result} />}
          <RawComparison key={selected.version} change={selected} result={result} />
        </article>
      )}
    </div>
  );
}

function DeleteConfirmation({ kind, target, disabled, deleting, onConfirm }: {
  kind: "page" | "source"; target: string; disabled: boolean; deleting: boolean; onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const detailsId = useId();
  const label = kind === "page" ? "Delete page" : "Delete source";

  return (
    <>
      <button className="button-danger" type="button" disabled={disabled || deleting} aria-label={`${label} ${target}`}
        onClick={() => {
          dialog.current?.showModal();
          cancel.current?.focus();
        }}>
        {deleting ? "Deleting…" : label}
      </button>
      <dialog ref={dialog} className="delete-dialog" aria-labelledby={titleId} aria-describedby={detailsId}>
        <h2 id={titleId}>{label}?</h2>
        <div id={detailsId}>
          <p><strong>{target}</strong></p>
          <p>{kind === "page"
            ? "This permanently removes the page, its linked changedetection watch, and its history in SignalWatch."
            : "This permanently removes the source, its pages and linked changedetection watches, and its history in SignalWatch."}
          </p>
          <p>Analysis excerpts can remain in the worker database to recover unfinished jobs.</p>
        </div>
        <div className="delete-dialog-actions">
          <button type="button" ref={cancel} onClick={() => dialog.current?.close()}>Cancel</button>
          <button type="button" className="delete-confirm" onClick={() => {
            dialog.current?.close();
            onConfirm();
          }}>{label}</button>
        </div>
      </dialog>
    </>
  );
}

function PageItem({ page, onChange, onDelete, disabled }: {
  page: Page; onChange: (page: Page) => void; onDelete: (page: Page) => void; disabled: boolean;
}) {
  const [busy, setBusy] = useState<"connect" | "status" | "delete" | null>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState<WatchStatus | null>(null);

  async function run(action: "connect" | "status" | "delete") {
    setBusy(action);
    setError("");
    setStatus(null);
    try {
      if (action === "connect") {
        onChange(await post<Page>(`/api/pages/${page.id}/connect`));
      } else if (action === "delete") {
        await request<void>(`/api/pages/${page.id}`, { method: "DELETE" });
        onDelete(page);
      } else {
        setStatus(await request<WatchStatus>(`/api/pages/${page.id}/status`));
      }
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className="page-item">
      <div className="page-item-heading">
        <p className="page-url">{page.url}</p>
        <span className={`status-pill ${page.state === "linked" ? "positive" : "attention"}`}>
          {pageStates[page.state]}
        </span>
      </div>
      {page.error && <p role="alert">{page.error}</p>}
      <div className="page-actions">
        {page.state === "linked" && (
          <a className="button-link" href={`/?view=history&page=${page.id}`}
            aria-label={`Change history for ${page.url}`}>Change history</a>
        )}
        {page.state === "pending" && (
          <button disabled={busy !== null || disabled} onClick={() => void run("connect")}>
            {busy === "connect" ? "Connecting…" : "Retry connection"}
          </button>
        )}
        <button disabled={busy !== null || disabled} onClick={() => void run("status")}>
          {busy === "status" ? "Refreshing…" : "Refresh status"}
        </button>
        <DeleteConfirmation kind="page" target={page.url} disabled={busy !== null || disabled}
          deleting={busy === "delete"} onConfirm={() => void run("delete")} />
      </div>
      {error && <p role="alert">{error}</p>}
      {status && <div className="watch-status" role="status">
        <p>{status.available ? "Status available" : "Status unavailable"}</p>
        <p>{status.paused === null ? "Pause state unknown" : status.paused ? "Paused" : "Not paused"}</p>
        <p>Last fetch attempt: {status.last_checked === null
          ? "None reported" : new Date(status.last_checked * 1000).toLocaleString()}</p>
        <p>Check count: {status.check_count ?? "Unknown"}</p>
        {status.error && <p>{status.error}</p>}
      </div>}
    </li>
  );
}

function CompetitorSection({ competitor, onPage, onDelete, onRemove }: {
  competitor: Competitor; onPage: (page: Page) => void; onDelete: (page: Page) => void;
  onRemove: (competitorId: number) => void;
}) {
  const [busy, setBusy] = useState<"add" | "delete" | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  async function addPage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const url = String(new FormData(form).get("url") ?? "");
    setBusy("add");
    setError("");
    setSaved(false);
    try {
      onPage(await post<Page>(`/api/competitors/${competitor.id}/pages`, { url }));
      form.reset();
      setSaved(true);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(null);
    }
  }

  async function deleteCompetitor() {
    setBusy("delete");
    setError("");
    setSaved(false);
    try {
      await request<void>(`/api/competitors/${competitor.id}`, { method: "DELETE" });
      onRemove(competitor.id);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="competitor" aria-labelledby={`competitor-${competitor.id}`}>
      <div className="panel-heading">
        <h3 id={`competitor-${competitor.id}`}>{competitor.name}</h3>
        <DeleteConfirmation kind="source" target={competitor.name} disabled={busy !== null}
          deleting={busy === "delete"} onConfirm={() => void deleteCompetitor()} />
      </div>
      {competitor.pages.length ? (
        <ul className="page-list">{competitor.pages.map((page) => (
          <PageItem key={page.id} page={page} onChange={onPage} onDelete={onDelete} disabled={busy === "delete"} />
        ))}</ul>
      ) : <p>No pages saved.</p>}
      <form onSubmit={addPage} aria-label={`Add page for ${competitor.name}`}>
        <fieldset disabled={busy !== null}>
          <label>Page URL
            <input name="url" type="url" placeholder="https://example.com/updates" required />
          </label>
          <button type="submit">{busy === "add" ? "Saving…" : "Add page"}</button>
        </fieldset>
        {error && <p role="alert">{error}</p>}
        {saved && <p role="status">Page saved.</p>}
      </form>
    </section>
  );
}

function SiteNavigation({ active, nested }: {
  active: "digest" | "configuration" | "maintainer"; nested: boolean;
}) {
  const current = nested ? "true" : "page";
  return (
    <aside className="site-rail">
      <a className="brand" href="/">
        <span className="brand-mark" aria-hidden="true" />
        SignalWatch
      </a>
      <nav className="app-nav" aria-label="Main navigation">
        <a href="/" aria-current={active === "digest" ? current : undefined}>
          <span className="nav-icon nav-digest" aria-hidden="true" />Digest
        </a>
        <a href={configurationPath} aria-current={active === "configuration" ? current : undefined}>
          <span className="nav-icon nav-configuration" aria-hidden="true" />Configuration
        </a>
        <a href="/?view=maintainer" aria-current={active === "maintainer" ? current : undefined}>
          <span className="nav-icon nav-maintainer" aria-hidden="true" />Maintainer settings
        </a>
      </nav>
      <a className="docs-link" href="/docs">API docs</a>
    </aside>
  );
}

const feedbackPath = "/api/feedback";
const suggestionPath = `${feedbackPath}/suggestion`;
const feedbackJudgments = { useful: "Useful", noisy: "Noisy", missed: "Should have seen this" } as const;
type FeedbackRow = {
  id: string; team: TeamKey; judgment: keyof typeof feedbackJudgments; url: string; created_at: string;
  note?: string | null; page_id?: number | null; version?: string | null; occurred_on?: string | null;
};

function FeedbackForm({ entry, pending }: { entry?: DigestEntry; pending: { current: Map<string, string> } }) {
  const [team, setTeam] = useState<TeamKey | "">("");
  const [status, setStatus] = useState<"saving" | "saved" | { error: string } | null>(null);
  const shown = team !== "" && entry?.relevant_teams.includes(team);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    const target = entry?.kind === "article" ? { url: entry.article_url, source_id: entry.source_id } : entry
      ? { url: entry.page_url, page_id: entry.page_id, version: entry.version }
      : { url: fields.get("url"), occurred_on: fields.get("occurred_on") || undefined };
    const draft = { ...target, team, judgment: entry ? fields.get("judgment") : "missed", note: fields.get("note") };
    const content = JSON.stringify(draft);
    const submissions = pending.current;
    const id = submissions.get(content) ?? crypto.randomUUID();
    submissions.set(content, id);
    setStatus("saving");
    try {
      await request<void>(feedbackPath, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...draft, id }), signal: AbortSignal.timeout(8000),
      });
      if (!form.isConnected) return;
      submissions.delete(content);
      form.reset();
      setTeam("");
      setStatus("saved");
    } catch (cause) {
      setStatus({ error: cause instanceof DOMException && cause.name === "TimeoutError"
        ? "Could not confirm the save. Send again to retry." : message(cause) });
    }
  }

  return (
    <form onSubmit={submit} aria-label={entry ? "Give feedback" : "Report a missed update"}>
      <fieldset disabled={status === "saving"}>
        <label>Team
          <select value={team} required onChange={(event) => {
            setTeam(analysisTeams.find(([key]) => key === event.currentTarget.value)?.[0] ?? "");
          }}>
            <option value="">Choose a team</option>
            {analysisTeams.map(([key, name]) => <option key={key} value={key}>{name}</option>)}
          </select>
        </label>
        {entry ? (
          <label>Feedback
            <select key={team} name="judgment" defaultValue="" required disabled={!team}>
              <option value="">Choose feedback</option>
              {shown ? <><option value="useful">Useful</option><option value="noisy">Noisy</option></>
                : <option value="missed">Should have seen this</option>}
            </select>
          </label>
        ) : (
          <>
            <label>Update URL<input name="url" type="url" required /></label>
            <label>Approximate date (optional)<input name="occurred_on" type="date" /></label>
          </>
        )}
        <label>Note (optional)<input name="note" maxLength={1000} /></label>
        <button type="submit" disabled={!team}>{status === "saving" ? "Saving…" : "Send feedback"}</button>
      </fieldset>
      {status === "saved" && <p role="status">Feedback saved.</p>}
      {status && typeof status === "object" && <p role="alert">{status.error}</p>}
    </form>
  );
}

function SlackFeedback({ feedbackToken }: { feedbackToken: string }) {
  const [entry, setEntry] = useState<DigestEntry | "unavailable" | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const pendingFeedback = useRef(new Map<string, string>());
  const validToken = /^[0-9a-f]{32}$/.test(feedbackToken);

  useEffect(() => {
    if (!validToken) return;
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    request<DigestEntry>(`${feedbackPath}/items/${encodeURIComponent(feedbackToken)}`, { signal, cache: "no-store" })
      .then((item) => { if (!controller.signal.aborted) setEntry(item); })
      .catch(() => { if (!controller.signal.aborted) setEntry("unavailable"); });
    return () => controller.abort();
  }, [feedbackToken, validToken, loadAttempt]);

  return (
    <main id="main-content" tabIndex={-1} className="configuration">
      <a className="back-link" href="/">← Digest</a>
      <div className="page-heading"><h1>Give feedback</h1></div>
      {!validToken || entry === "unavailable" ? (
        <div className="panel view-state">
          <p role="alert">This feedback link is unavailable.</p>
          {validToken && <button type="button" onClick={() => {
            setEntry(null);
            setLoadAttempt((attempt) => attempt + 1);
          }}>Retry loading</button>}
        </div>
      ) : entry === null ? (
        <div className="panel view-state"><p role="status">Loading update…</p></div>
      ) : (
        <article className="panel digest-entry">
          <header>
            <div className="digest-entry-meta">
              <span>{entry.source_name}</span>
              <ChangeTime version={entry.version} />
            </div>
            <h2>{entry.kind === "article" ? entry.title : changeHeadline(entry.summary ?? "") || "Page changed"}</h2>
            {entry.kind === "article" && entry.summary && <p>{entry.summary}</p>}
          </header>
          <div className="digest-entry-links">
            {entry.kind === "article" ? <a href={entry.article_url}>Open article</a> :
              <a href={entry.page_url}>Open source page</a>}
          </div>
          <FeedbackForm entry={entry} pending={pendingFeedback} />
        </article>
      )}
    </main>
  );
}

function Digest({ held, walkthroughOpen }: { held: boolean; walkthroughOpen: boolean }) {
  const [entries, setEntries] = useState<DigestEntry[] | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [error, setError] = useState(false);
  const [feedbackEnabled, setFeedbackEnabled] = useState(false);
  const [feedbackTarget, setFeedbackTarget] = useState<DigestEntry | "missing" | null>(null);
  const pendingFeedback = useRef(new Map<string, string>());

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    setError(false);
    request<DigestEntry[]>(held ? "/api/digest?view=held" : "/api/digest", { signal }, (headers) => {
      if (!controller.signal.aborted) setFeedbackEnabled(headers.get("X-Feedback-Enabled") === "true");
    })
      .then((entries) => { if (!controller.signal.aborted) setEntries(entries); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [held, loadAttempt]);

  return (
    <main id="main-content" tabIndex={-1} className="configuration">
      {held && <a className="back-link" href="/?view=maintainer">← Maintainer settings</a>}
      <div className="page-heading">
        <div>
          <h1>{held ? "Held items" : "Digest"}</h1>
          <p className="muted">{held
            ? "Review analysis status and updates held from the digest."
            : "Updates that match your team interests."}</p>
        </div>
        {feedbackEnabled && !held && (
          <button type="button" aria-expanded={feedbackTarget === "missing"}
            onClick={() => setFeedbackTarget(feedbackTarget === "missing" ? null : "missing")}>
            Report a missed update
          </button>
        )}
      </div>
      {!held && (
        <details className="panel demo-video" open={walkthroughOpen}>
          <summary>Watch the walkthrough</summary>
          <video src={demoVideoUrl} controls preload="none" playsInline aria-label="SignalWatch walkthrough video" />
          <a href={demoVideoUrl}>Open video directly</a>
        </details>
      )}
      {feedbackEnabled && feedbackTarget === "missing" && (
        <div className="panel feedback-report"><FeedbackForm pending={pendingFeedback} /></div>
      )}
      {error ? (
        <div className="panel view-state">
          <p role="alert">{held ? "Could not load held items." : "Could not load the digest."}</p>
          <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>
        </div>
      ) : entries === null ? (
        <div className="panel view-state"><p role="status">{held ? "Loading held items…" : "Loading digest…"}</p></div>
      ) : entries.length === 0 ? (
        <div className="panel view-state">
          <p role="status">{held ? "No held items yet." : "No items match your team interests yet."}</p>
        </div>
      ) : (
        <ol className="digest-feed">
          {entries.map((entry) => {
            const isArticle = entry.kind === "article";
            const holdReason = isArticle && entry.hold_reason === "business_fit_unverified"
              ? businessFitUnverifiedReason : null;
            const sourceUrl = isArticle ? entry.article_url : entry.page_url;
            const destination = new URL(sourceUrl);
            const sourceLocation = isArticle || destination.pathname === "/"
              ? destination.host : `${destination.host}${destination.pathname}`;
            const completed = entry.status === "completed";
            const summary = completed ? entry.summary?.trim() : null;
            const takeaway = summary ? changeHeadline(summary) : null;
            const title = isArticle ? entry.title : takeaway ?? "Page changed";
            const showAllReasons = held && isArticle;
            const teams = completed ? analysisTeams.filter(([key]) => showAllReasons
              ? entry.team_reasons[key] !== undefined : entry.relevant_teams.includes(key)) : [];
            const historyPath = isArticle ? null :
              `/?view=history&page=${entry.page_id}&version=${encodeURIComponent(entry.version)}${held ? "&from=held" : ""}`;
            return (
              <li key={isArticle ? `article:${entry.source_id}:${sourceUrl}` : `edit:${entry.page_id}/${entry.version}`}>
                <article className="panel digest-entry">
                  <header>
                    <div className="digest-entry-meta">
                      <span>{isArticle ? `Via ${entry.source_name}` : entry.source_name}</span>
                      <ChangeTime version={entry.version} />
                    </div>
                    <h2>{title}</h2>
                    {isArticle && takeaway && <p>{takeaway}</p>}
                    {!takeaway && !holdReason && (
                      <p className={entry.status === "failed" ? "error-text" : "muted"}>
                        {digestAnalysisStatus[entry.status]}
                      </p>
                    )}
                  </header>
                  {holdReason ? (
                    <p className="muted">{holdReason}</p>
                  ) : completed && (teams.length === 0 ? <p className="muted">Skipped. No team matched.</p> : (
                    <div className="digest-relevance">
                      <h3>{showAllReasons ? "Team reasons" : "Why it matters"}</h3>
                      <ul className="digest-team-reasons">
                        {teams.map(([key, name]) => (
                          <li key={key}>
                            <strong className="digest-team-label">{name}</strong>
                            <span>{entry.team_reasons[key]?.trim() || "Reason unavailable."}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                  <footer className="digest-entry-links">
                    {historyPath && (
                      <a href={historyPath} aria-label={`View change history for ${sourceLocation}`}>View change history</a>
                    )}
                    <a href={sourceUrl}>
                      {isArticle ? `Open article on ${sourceLocation}` : `Open source page · ${sourceLocation}`}
                    </a>
                    {feedbackEnabled && (
                      <button type="button" aria-expanded={feedbackTarget === entry} aria-label={`Give feedback on ${title}`}
                        onClick={() => setFeedbackTarget(feedbackTarget === entry ? null : entry)}>Give feedback</button>
                    )}
                  </footer>
                  {feedbackEnabled && feedbackTarget === entry && <FeedbackForm entry={entry} pending={pendingFeedback} />}
                </article>
              </li>
            );
          })}
        </ol>
      )}
    </main>
  );
}

function PageHistory({ pageId, requestedVersion, fromHeld }: {
  pageId: string; requestedVersion: string | null; fromHeld: boolean;
}) {
  const [sources, setSources] = useState<Competitor[] | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [error, setError] = useState("");
  const source = sources?.find((item) => item.pages.some((page) => String(page.id) === pageId));
  const page = source?.pages.find((item) => String(item.id) === pageId);
  const linked = source && page?.state === "linked";

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    setError("");
    request<Competitor[]>("/api/competitors", { signal })
      .then((sources) => { if (!controller.signal.aborted) setSources(sources); })
      .catch(() => { if (!controller.signal.aborted) setError("Could not load the watched page."); });
    return () => controller.abort();
  }, [loadAttempt]);

  return (
    <main id="main-content" tabIndex={-1} className="configuration history-page">
      {fromHeld ? <a className="back-link" href="/?view=held">← Held items</a> : requestedVersion !== null ? (
        <a className="back-link" href="/">← Digest</a>
      ) : (
        <a className="back-link" href={source ? `${configurationPath}#competitor-${source.id}` : configurationPath}>
          ← Configuration
        </a>
      )}
      <div className="page-heading">
        <div>
          <h1>{linked ? source.name : "Change history"}</h1>
          {linked && <p className="page-url muted">{page.url}</p>}
        </div>
      </div>
      {error ? (
        <div className="panel view-state">
          <p role="alert">{error}</p>
          <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>
        </div>
      ) : sources === null ? <div className="panel view-state"><p role="status">Loading watched page…</p></div> : linked ? (
        <ChangeHistory pageId={page.id} pageUrl={page.url} requestedVersion={requestedVersion} />
      ) : <div className="panel view-state"><p role="status">This watched page is no longer available.</p></div>}
    </main>
  );
}

function MaintainerSettings({ slackCallback }: { slackCallback: string | null }) {
  const [status, setStatus] = useState<WebhookStatus | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    request<WebhookStatus>("/api/maintainer/webhooks", { signal: controller.signal })
      .then((status) => { if (!controller.signal.aborted) setStatus(status); })
      .catch(() => { if (!controller.signal.aborted) setError("Could not load webhook status."); });
    return () => controller.abort();
  }, [loadAttempt]);

  return (
    <main id="main-content" tabIndex={-1} className="configuration maintainer-page">
      <header className="page-heading">
        <div>
          <h1>Maintainer settings</h1>
          <p className="muted">Manage integrations and analysis for your watched sources.</p>
        </div>
        <div className="page-heading-actions">
          <a className="button-link button-primary" href="/?view=held">Review held items</a>
        </div>
      </header>
      <div className="maintainer-grid">
        <SourceMonitoring />
        <section className="panel" aria-labelledby="webhooks-title">
          <h2 id="webhooks-title">changedetection webhooks</h2>
          {status ? (
            <>
              <p role="status">
                <span className={status.configured ? "status-pill positive" : "status-pill attention"}>
                  {status.configured ? "Webhook settings configured" : "Setup required"}
                </span>
              </p>
              <p className="muted">{status.configured
                ? "SignalWatch checked the format of the webhook URL and shared secret set on the server."
                : "Set the SignalWatch webhook URL and shared secret on the server."}</p>
            </>
          ) : !error && <p role="status">Loading webhook status…</p>}
          {error && <p role="alert">{error}</p>}
          {error && <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>}
        </section>
        <AnalysisQualitySettings />
        <SlackSettings callback={slackCallback} />
        <ArticleDiscoverySettings />
        <FeedbackReview />
      </div>
    </main>
  );
}

function SourceMonitoring() {
  const [sources, setSources] = useState<StaleSource[] | "error" | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    request<StaleSource[]>("/api/maintainer/monitoring", { signal, cache: "no-store" })
      .then((sources) => { if (!controller.signal.aborted) setSources(sources); })
      .catch(() => { if (!controller.signal.aborted) setSources("error"); });
    return () => controller.abort();
  }, [loadAttempt]);

  return (
    <section className="panel monitoring-settings" aria-labelledby="monitoring-title">
      <header className="panel-heading">
        <h2 id="monitoring-title">Source monitoring</h2>
        <button type="button" disabled={sources === null} onClick={() => {
          setSources(null);
          setLoadAttempt((attempt) => attempt + 1);
        }}>Refresh status</button>
      </header>
      {sources === null ? <p role="status">Loading monitoring status…</p> :
        sources === "error" ? <p role="alert">Could not load monitoring status.</p> :
        sources.length === 0 ? <p role="status">No stale sources.</p> : (
          <ul className="page-list" aria-label="Stale sources">
            {sources.map((source) => (
              <li className="page-item" key={`${source.kind}:${source.id}`}>
                <p><strong>{source.source_name}</strong> · {source.kind === "page" ? "Watched page" : "Article feed"}</p>
                <p><a href={source.url}>{source.url}</a></p>
                <p className="muted">Last successful check: {source.last_success_at === null
                  ? "None yet" : new Date(source.last_success_at * 1000).toLocaleString()}</p>
              </li>
            ))}
          </ul>
        )}
    </section>
  );
}

type RuleSuggestion = {
  id: string; team: TeamKey; status: "queued" | "ready" | "failed" | "applied";
  current_interest: string; proposed_interest: string | null; error: string | null;
  cases: {
    title: string; url: string; expected: boolean | null; current: TeamKey[] | null; proposed: TeamKey[] | null;
  }[];
};

function ArticleSuggestion() {
  const [team, setTeam] = useState<TeamKey>(analysisTeams[0][0]);
  const [checked, setChecked] = useState<{ team: TeamKey; suggestion: RuleSuggestion | null; error?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const suggestion = checked?.team === team ? checked.suggestion : undefined;
  const readError = checked?.team === team ? checked.error : undefined;
  const teamName = analysisTeams.find(([key]) => key === team)?.[1];

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function check() {
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
      try {
        const result = await request<RuleSuggestion | null>(`${suggestionPath}?team=${team}`, { signal, cache: "no-store" });
        if (controller.signal.aborted) return;
        setChecked({ team, suggestion: result });
        if (result?.status === "queued") timer = window.setTimeout(check, 2000);
      } catch (cause) {
        if (!controller.signal.aborted) setChecked({ team, suggestion: null, error: message(cause) });
      }
    }
    void check();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [team, busy, attempt]);

  async function act(endpoint: string, method: "POST" | "DELETE", body?: object) {
    if (busy) return;
    setBusy(true);
    setActionError("");
    try {
      const result = await request<RuleSuggestion | undefined>(endpoint, {
        method, signal: AbortSignal.timeout(8000),
        headers: body ? { "Content-Type": "application/json" } : undefined, body: JSON.stringify(body),
      });
      setChecked({ team, suggestion: result ?? null });
    } catch (cause) {
      setActionError(message(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h3>Article rule suggestions</h3>
      <fieldset disabled={busy}>
        <label>Team
          <select value={team} onChange={(event) => {
            const selected = analysisTeams.find(([key]) => key === event.currentTarget.value);
            if (selected) { setTeam(selected[0]); setChecked(null); setActionError(""); }
          }}>
            {analysisTeams.map(([key, name]) => <option key={key} value={key}>{name}</option>)}
          </select>
        </label>
        {!readError && (suggestion === null || suggestion?.status === "failed" || suggestion?.status === "applied") && (
          <button type="button" className="button-primary" onClick={() => void act(suggestionPath, "POST", { team })}>
            {suggestion?.status === "failed" ? "Retry suggestion" : "Suggest change"}
          </button>
        )}
      </fieldset>
      {busy ? <p role="status">Working…</p> : suggestion === undefined && <p role="status">Checking for a suggestion…</p>}
      {suggestion?.status === "queued" && <p role="status">Preparing a suggestion and checking the article examples…</p>}
      {readError && <>
        <p role="alert">Could not check the suggestion. {readError}</p>
        <button type="button" onClick={() => { setChecked(null); setAttempt((value) => value + 1); }}>Retry loading</button>
      </>}
      {actionError && <p role="alert">{actionError}</p>}
      {suggestion?.status === "failed" && <p role="alert">{suggestion.error || "Could not prepare a suggestion."}</p>}
      {suggestion?.status === "applied" && <p role="status">Suggestion applied to team interests.</p>}
      {(suggestion?.status === "ready" || suggestion?.status === "applied") && <>
        <dl className="suggestion-wording">
          <dt>{suggestion.status === "applied" ? "Before" : "Current wording"}</dt>
          <dd>{suggestion.current_interest || "No note set."}</dd>
          <dt>{suggestion.status === "applied" ? "After" : "Suggested wording"}</dt>
          <dd>{suggestion.proposed_interest}</dd>
        </dl>
        <table aria-label="Article decisions before and after the suggested change">
          <thead><tr><th scope="col">Article</th><th scope="col">Before</th><th scope="col">After</th></tr></thead>
          <tbody>{suggestion.cases.map((example) => (
            <tr key={example.url}>
              <th scope="row">
                <a href={example.url}>{example.title}</a>
                {example.expected !== null && <div>Expected for {teamName} · {example.expected ? "Show" : "Hold"}</div>}
              </th>
              {[example.current, example.proposed].map((teams, index) => (
                <td key={index}>{teams === null ? "Unavailable" : analysisTeams.map(([key, name]) => (
                  <div key={key}>{name} · {teams.includes(key) ? "Show" : "Hold"}</div>
                ))}</td>
              ))}
            </tr>
          ))}</tbody>
        </table>
        {suggestion.status === "ready" && <div className="button-row suggestion-actions">
          <button type="button" className="button-primary" disabled={busy}
            onClick={() => void act(`${suggestionPath}/${encodeURIComponent(suggestion.id)}/apply`, "POST")}>Apply</button>
          <button type="button" disabled={busy}
            onClick={() => void act(`${suggestionPath}/${encodeURIComponent(suggestion.id)}`, "DELETE")}>Dismiss</button>
        </div>}
      </>}
    </>
  );
}

function FeedbackReview() {
  const [rows, setRows] = useState<FeedbackRow[] | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    fetch(feedbackPath, { signal, cache: "no-store" }).then(async (response) => {
      if (response.status === 404) return;
      if (!response.ok) throw new Error();
      const feedback: FeedbackRow[] = await response.json();
      if (!controller.signal.aborted) setRows(feedback);
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, []);

  if (rows === null && !error) return null;

  return (
    <section className="panel feedback-review" aria-labelledby="feedback-review-title">
      <h2 id="feedback-review-title">Feedback</h2>
      {error && <p role="alert">Could not load feedback. Refresh to try again.</p>}
      {rows && <>
        <ArticleSuggestion />
        <h3>Latest feedback</h3>
        {rows.length === 0 ? <p>No feedback yet.</p> : <ul>{rows.map((row) => (
          <li key={row.id}>
            <strong>{analysisTeams.find(([key]) => key === row.team)?.[1]} · {feedbackJudgments[row.judgment]}</strong>
            {" · "}<a href={row.url}>{row.url}</a>
            {row.page_id && row.version && <> · <a
              href={`/?view=history&page=${row.page_id}&version=${encodeURIComponent(row.version)}`}>History</a></>}
            {" · "}<time dateTime={row.created_at}>{new Date(row.created_at).toLocaleDateString()}</time>
            {row.occurred_on && <p>Occurred around {row.occurred_on}</p>}
            {row.note && <p>{row.note}</p>}
          </li>
        ))}</ul>}
      </>}
    </section>
  );
}

async function readSlackStatus(signal?: AbortSignal) {
  const timeout = AbortSignal.timeout(8000);
  try {
    return await request<SlackStatus>(slackPath, {
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout, cache: "no-store",
    });
  } catch (cause) {
    return { error: `Could not load Slack settings. ${message(cause)}` };
  }
}

function SlackSettings({ callback }: { callback: string | null }) {
  const [status, setStatus] = useState<SlackStatus | { error: string } | null>(null);
  const [statusAttempt, setStatusAttempt] = useState(0);
  const [channels, setChannels] = useState<SlackChannel[] | { error: string } | null>(null);
  const [channelAttempt, setChannelAttempt] = useState(0);
  const [draft, setDraft] = useState<{ channels: SlackTeamChannels; revision: string } | null>(null);
  const [setupRequested, setSetupRequested] = useState(false);
  const [action, setAction] = useState("");
  const [feedback, setFeedback] = useState<{ role: "status" | "alert"; text: string } | null>(null);

  const settings = status && "available" in status ? status : null;
  const connected = settings?.available && settings.connected;
  const awaitingPassphrase = settings?.available && setupRequested && !settings.authenticated;
  const revision = settings?.revision ?? null;
  const savedChannels = settings?.team_channels ?? {};
  const draftChannels = draft?.revision === revision ? draft.channels : null;
  const teamChannels = draftChannels ?? savedChannels;
  const channelsChanged = analysisTeams.some(([team]) => teamChannels[team] !== savedChannels[team]);
  const availableChannels = Array.isArray(channels) ? channels : [];

  useEffect(() => {
    if (callback !== "connected" && callback !== "error") return;
    const location = new URL(window.location.href);
    location.searchParams.delete("slack");
    window.history.replaceState(window.history.state, "", location);
  }, [callback]);

  useEffect(() => {
    const controller = new AbortController();
    readSlackStatus(controller.signal).then((next) => {
      if (!controller.signal.aborted) setStatus(next);
    });
    return () => controller.abort();
  }, [statusAttempt]);

  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    request<SlackChannel[]>(`${slackPath}/channels`, { signal: controller.signal, cache: "no-store" })
      .then((result) => { if (!controller.signal.aborted) setChannels(result); })
      .catch(async (cause) => {
        if (controller.signal.aborted) return;
        const next = await readSlackStatus(controller.signal);
        if (controller.signal.aborted) return;
        const error = `Could not load Slack channels. ${message(cause)}`;
        setStatus(next);
        setChannels({ error });
        if ("connected" in next && next.available && !next.connected) {
          setFeedback({ role: "alert", text: error });
        }
      });
    return () => controller.abort();
  }, [connected, channelAttempt]);

  async function runAction(progress: string, perform: () => Promise<void>) {
    if (action) return;
    setAction(progress);
    setFeedback(null);
    try {
      await perform();
    } catch (cause) {
      setFeedback({ role: "alert", text: message(cause) });
      if (settings?.connected || settings?.authenticated) {
        const next = await readSlackStatus();
        const connectionChanged = "error" in next || !next.available || !next.connected ||
          next.workspace_name !== settings.workspace_name;
        if (connectionChanged) {
          setChannels(null);
          if ("connected" in next && next.connected) setChannelAttempt((attempt) => attempt + 1);
        }
        if (connectionChanged || next.revision !== revision) setDraft(null);
        setStatus(next);
      }
    } finally {
      setAction("");
    }
  }

  function authorizeSetup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const password = new FormData(event.currentTarget).get("password");
    event.currentTarget.reset();
    if (typeof password !== "string" || !password) return;
    void runAction("Opening Slack…", async () => {
      await post<void>(`${slackPath}/session`, { password });
      await openSlack();
    });
  }

  function connect() {
    setSetupRequested(true);
    setFeedback(null);
    if (settings?.authenticated) void runAction("Opening Slack…", openSlack);
  }

  async function openSlack() {
    const { url } = await post<{ url: string }>(`${slackPath}/connect`, {});
    const destination = URL.parse(url);
    if (!destination || destination.origin !== "https://slack.com" ||
        destination.pathname !== "/oauth/v2/authorize" || destination.username || destination.password) {
      throw new Error("Slack authorization returned an invalid link.");
    }
    window.location.assign(destination.href);
  }

  function selectChannel(team: TeamKey, channel: string) {
    if (revision === null) return;
    setDraft((current) => {
      const next = { ...(current?.revision === revision ? current.channels : savedChannels) };
      if (channel) next[team] = channel;
      else delete next[team];
      return { channels: next, revision };
    });
  }

  function saveChannels(destinations: SlackTeamChannels, enabled: boolean, notice: string) {
    if (revision === null) return;
    void runAction("Saving Slack settings…", async () => {
      const next = await request<SlackStatus>(slackPath, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ team_channels: destinations, enabled, revision }),
      });
      setStatus(next);
      setDraft(channelsChanged && destinations !== draftChannels && next.revision !== null
        ? { channels: teamChannels, revision: next.revision } : null);
      setFeedback({ role: "status", text: notice });
    });
  }

  function sendTest(team: TeamKey, teamName: string) {
    void runAction("Sending test message…", async () => {
      await post<void>(`${slackPath}/test`, { team });
      setFeedback({ role: "status", text: `Test message sent to the ${teamName} channel.` });
    });
  }

  return (
    <section className="panel slack-settings" aria-labelledby="slack-settings-title">
      <header className="panel-heading">
        <div>
          <h2 id="slack-settings-title">Slack delivery</h2>
          {connected && <p className="muted">Workspace <strong>{settings.workspace_name ?? "Slack"}</strong></p>}
        </div>
        {settings?.available && !awaitingPassphrase && (
          <button type="button" disabled={Boolean(action)} onClick={connect}>
            {connected ? "Reconnect Slack" : "Connect Slack"}
          </button>
        )}
      </header>
      {!action && !feedback && callback === "error" &&
        <p role="alert">Slack authorization did not complete. Try connecting again.</p>}
      {!action && !feedback && callback === "connected" && settings?.available && (
        settings.connected ? <p role="status">Slack workspace connected.</p> :
          <p role="alert">Could not verify the Slack connection. Try connecting again.</p>
      )}
      {status === null && <p role="status">Loading Slack settings…</p>}
      {status && "error" in status && (
        <>
          <p role="alert">{status.error}</p>
          <button type="button" onClick={() => { setStatus(null); setStatusAttempt((attempt) => attempt + 1); }}>
            Retry loading Slack settings
          </button>
        </>
      )}
      {settings && !settings.available && <p>Slack delivery is unavailable.</p>}
      {awaitingPassphrase && (
        <form className="slack-passphrase" onSubmit={authorizeSetup} autoComplete="off">
          <fieldset disabled={Boolean(action)}>
            <label>Maintainer passphrase<input name="password" type="password" required /></label>
            <div className="button-row">
              <button type="submit" className="button-primary">Continue to Slack</button>
              <button type="button" onClick={() => { setSetupRequested(false); setFeedback(null); }}>Cancel</button>
            </div>
          </fieldset>
        </form>
      )}
      {connected && (
        <>
          <div className="panel-heading slack-channels-heading">
            <h3 id="slack-channels-title">Team channels</h3>
            {channels !== null && (
              <button type="button" disabled={Boolean(action)} onClick={() => {
                setChannels(null);
                setChannelAttempt((attempt) => attempt + 1);
              }}>Reload channels</button>
            )}
          </div>
          {channels === null && <p role="status">Loading Slack channels…</p>}
          {channels && "error" in channels && <p role="alert">{channels.error}</p>}
          {Array.isArray(channels) && channels.length === 0 && (
            <p>No selectable channels. Invite the SignalWatch bot to a channel, then reload channels.</p>
          )}
          <form onSubmit={(event) => {
            event.preventDefault();
            if (channelsChanged) saveChannels(teamChannels, false, "Channels saved. Delivery is disabled.");
          }}>
            <fieldset className="slack-team-channels" aria-labelledby="slack-channels-title"
              disabled={Boolean(action) || channels === null || revision === null}>
              {analysisTeams.map(([team, name]) => {
                const savedChannel = savedChannels[team];
                const channelId = teamChannels[team];
                const savedUnavailable = savedChannel && !availableChannels.some((channel) => channel.id === savedChannel);
                const draftUnavailable = channelId && channelId !== savedChannel &&
                  !availableChannels.some((channel) => channel.id === channelId);
                return (
                  <div className="slack-team-channel" key={team}>
                    <label>
                      <span>{name} channel</span>
                      <select value={channelId ?? ""} onChange={(event) => selectChannel(team, event.target.value)}>
                        <option value="">No channel</option>
                        {savedUnavailable && <option value={savedChannel}>Saved channel ({savedChannel})</option>}
                        {draftUnavailable && <option value={channelId} disabled>Unavailable channel ({channelId})</option>}
                        {availableChannels.map((channel) => <option key={channel.id} value={channel.id}>#{channel.name}</option>)}
                      </select>
                    </label>
                    {savedChannel && (
                      <button type="button" disabled={channelId !== savedChannel} aria-label={`Send ${name} test message`}
                        onClick={() => sendTest(team, name)}>Send test message</button>
                    )}
                  </div>
                );
              })}
              <button type="submit" className="button-primary" disabled={!channelsChanged}>Save channels</button>
            </fieldset>
          </form>
          <div className="slack-delivery-footer">
            <p role="status">
              <span className={settings.enabled ? "status-pill positive" : "status-pill"}>
                Delivery is {settings.enabled ? "enabled" : "disabled"}
              </span>
            </p>
            {Object.keys(savedChannels).length > 0 && (
              <fieldset aria-label="Slack delivery controls" disabled={Boolean(action) || channels === null || revision === null}>
                <button type="button" disabled={channelsChanged && !settings.enabled} onClick={() =>
                  saveChannels(savedChannels, !settings.enabled, "Delivery settings saved.")
                }>{settings.enabled ? "Disable delivery" : "Enable delivery"}</button>
              </fieldset>
            )}
          </div>
        </>
      )}
      {action && <p role="status">{action}</p>}
      {feedback && <p role={feedback.role}>{feedback.text}</p>}
    </section>
  );
}

function AnalysisQualitySettings() {
  const settingsPath = "/api/maintainer/analysis";
  const [settings, setSettings] = useState<AnalysisSettings | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    setError("");
    request<AnalysisSettings>(settingsPath, { signal })
      .then((settings) => { if (!controller.signal.aborted) setSettings(settings); })
      .catch(() => { if (!controller.signal.aborted) setError("Could not load analysis quality."); });
    return () => controller.abort();
  }, [loadAttempt]);

  async function saveQuality(quality: string) {
    setSaving(true);
    setError("");
    try {
      setSettings(await request<AnalysisSettings>(settingsPath, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ quality }),
        signal: AbortSignal.timeout(8000),
      }));
    } catch {
      setSettings(null);
      setError("Could not confirm the save. Reload analysis quality to check the current setting.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel analysis-settings" aria-labelledby="analysis-settings-title">
      <h2 id="analysis-settings-title">Saved page analysis</h2>
      {settings ? (
        <label className="analysis-quality">
          Analysis quality
          <select value={settings.quality} disabled={saving} aria-describedby="analysis-quality-help"
            onChange={(event) => void saveQuality(event.target.value)}>
            <option value="economy">Economy</option>
            <option value="higher_quality">Higher quality</option>
          </select>
        </label>
      ) : !error && <p role="status">Loading analysis quality…</p>}
      <p className="muted" id="analysis-quality-help">Applies to upcoming page analyses.</p>
      {saving && <p role="status">Saving analysis quality…</p>}
      {error && <p role="alert">{error}</p>}
      {!settings && error && <button onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>}
    </section>
  );
}

function ArticleDiscoverySettings() {
  const [feeds, setFeeds] = useState<ArticleFeedSettings[] | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    setError(false);
    request<ArticleFeedSettings[]>(articleFeedsPath, { signal })
      .then((feeds) => { if (!controller.signal.aborted) setFeeds(feeds); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [loadAttempt]);

  return (
    <section className="panel article-discovery-settings" aria-labelledby="article-discovery-title">
      <h2 id="article-discovery-title">Article discovery</h2>
      {error ? (
        <>
          <p role="alert">Could not load article feeds.</p>
          <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading feeds</button>
        </>
      ) : feeds === null ? <p role="status">Loading article feeds…</p> : feeds.length === 0 ? (
        <p>No sources saved.</p>
      ) : (
        <>
          <p className="muted" id="article-feed-help">Leave the RSS URL blank to turn off discovery.</p>
          {feeds.map((feed) => <ArticleFeedForm key={feed.source_id} feed={feed} />)}
        </>
      )}
    </section>
  );
}

function ArticleFeedForm({ feed }: { feed: ArticleFeedSettings }) {
  const [feedUrl, setFeedUrl] = useState(feed.url ?? "");
  const [saveState, setSaveState] = useState<"saving" | "saved" | null>(null);
  const [error, setError] = useState("");

  async function saveFeed(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saveState === "saving") return;
    setSaveState("saving");
    setError("");
    try {
      const saved = await request<ArticleFeedSettings>(`${articleFeedsPath}/${feed.source_id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: feedUrl.trim() || null }),
        signal: AbortSignal.timeout(8000),
      });
      setFeedUrl(saved.url ?? "");
      setSaveState("saved");
    } catch (cause) {
      setError(`Could not save feed settings. ${message(cause)}`);
      setSaveState(null);
    }
  }

  return (
    <form className="article-feed-form" onSubmit={saveFeed} onChange={() => {
      setSaveState(null);
      setError("");
    }}>
      <fieldset disabled={saveState === "saving"}>
        <legend>{feed.source_name}</legend>
        <label>RSS 2.0 URL
          <input type="url" name="url" value={feedUrl} aria-describedby="article-feed-help"
            onChange={(event) => setFeedUrl(event.target.value)} />
        </label>
        <button type="submit" className="button-primary">{saveState === "saving" ? "Saving…" : "Save feed"}</button>
      </fieldset>
      {saveState === "saved" && <p role="status">Feed settings saved.</p>}
      {error && <p role="alert">{error}</p>}
    </form>
  );
}

function TeamInterestsSettings() {
  const settingsPath = "/api/team-interests";
  const [interests, setInterests] = useState<TeamInterests | null>(null);
  const savedInterests = useRef<TeamInterests | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [saveState, setSaveState] = useState<"saving" | "saved" | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    setError("");
    request<TeamInterests>(settingsPath, { signal })
      .then((loaded) => {
        if (controller.signal.aborted) return;
        savedInterests.current = loaded;
        setInterests(loaded);
      })
      .catch(() => { if (!controller.signal.aborted) setError("Could not load team interests."); });
    return () => controller.abort();
  }, [loadAttempt]);

  function updateInterest(key: keyof TeamInterests, value: string) {
    setInterests((current) => current && { ...current, [key]: value });
    setSaveState(null);
    setError("");
  }

  async function saveInterests(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = savedInterests.current;
    if (interests === null || saved === null || saveState === "saving") return;
    const changes: Partial<TeamInterests> = {};
    for (const key of ["business_context", ...analysisTeams.map(([key]) => key)] as const) {
      if (interests[key].trim() !== saved[key]) changes[key] = interests[key];
    }
    setSaveState("saving");
    setError("");
    try {
      const updated = await request<TeamInterests>(settingsPath, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
        signal: AbortSignal.timeout(8000),
      });
      savedInterests.current = updated;
      setInterests(updated);
      setSaveState("saved");
    } catch (cause) {
      setError(`Could not save team interests. ${message(cause)}`);
      setSaveState(null);
    }
  }

  return (
    <section className="panel" aria-labelledby="team-interests-title">
      <h2 id="team-interests-title">Team interests</h2>
      <p className="muted" id="team-interests-help">
        Describe what each team wants to hear about. These interests apply across all watched pages and articles.
        SignalWatch checks each team's interests separately.
      </p>
      {interests ? (
        <form onSubmit={saveInterests} aria-describedby="team-interests-help">
          <fieldset className="team-interests-fields" disabled={saveState === "saving"}>
            <label>What matters to us (optional)
              <textarea name="business_context" rows={3} maxLength={2000} aria-describedby="business-context-help"
                value={interests.business_context} onChange={(event) => updateInterest("business_context", event.target.value)} />
            </label>
            <small className="muted" id="business-context-help">
              Describe what your business does or is focused on. Articles need a clear match here to appear in Digest.
            </small>
            {analysisTeams.map(([key, name]) => (
              <label key={key}>{name}
                <textarea name={key} rows={3} maxLength={1000} value={interests[key]}
                  onChange={(event) => updateInterest(key, event.target.value)} />
              </label>
            ))}
            <button className="button-primary" type="submit">{saveState === "saving" ? "Saving…" : "Save team interests"}</button>
          </fieldset>
          {saveState === "saved" && <p role="status">Team interests saved.</p>}
        </form>
      ) : !error && <p role="status">Loading team interests…</p>}
      {error && <p role="alert">{error}</p>}
      {!interests && error && <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>
        Retry loading team interests
      </button>}
    </section>
  );
}

function Configuration() {
  const [competitors, setCompetitors] = useState<Competitor[]>([]);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError("");
    request<Competitor[]>("/api/competitors", { signal: controller.signal })
      .then(setCompetitors)
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setLoadError(message(cause));
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [loadAttempt]);

  async function addCompetitor(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    setSaving(true);
    setSaveError("");
    setSaved(false);
    try {
      const competitor = await post<Competitor>("/api/competitors", {
        name: String(fields.get("name") ?? "").trim(), website: String(fields.get("website") ?? ""),
      });
      setCompetitors((current) => [...current, competitor]);
      form.reset();
      setSaved(true);
    } catch (cause) {
      setSaveError(message(cause));
    } finally {
      setSaving(false);
    }
  }

  function updatePage(page: Page) {
    setCompetitors((current) => current.map((competitor) => {
      if (competitor.id !== page.competitor_id) return competitor;
      const pages = competitor.pages.some((item) => item.id === page.id)
        ? competitor.pages.map((item) => item.id === page.id ? page : item)
        : [...competitor.pages, page];
      return { ...competitor, pages };
    }));
  }

  function deletePage(page: Page) {
    setCompetitors((current) => current.map((competitor) => competitor.id === page.competitor_id
      ? { ...competitor, pages: competitor.pages.filter((item) => item.id !== page.id) }
      : competitor));
  }

  return (
    <main id="main-content" tabIndex={-1} className="configuration">
      <div className="page-heading">
        <div>
          <h1>Configuration</h1>
          <p className="muted">Manage watched pages and what each team wants to hear about.</p>
        </div>
      </div>
      <div className="configuration-grid">
        <div className="source-panels">
          <section className="panel" aria-labelledby="add-competitor-title">
            <h2 id="add-competitor-title">Add source</h2>
            <form onSubmit={addCompetitor}>
              <fieldset disabled={saving || loading || Boolean(loadError)}>
                <label>Source name<input name="name" required /></label>
                <label>First page to watch
                  <input name="website" type="url" placeholder="https://example.com" required />
                </label>
                <button className="button-primary" type="submit">{saving ? "Saving…" : "Add source"}</button>
              </fieldset>
              {saveError && <p role="alert">{saveError}</p>}
              {saved && <p role="status">Source and first page saved.</p>}
            </form>
          </section>
          <section className="panel" aria-labelledby="competitors-title">
            <h2 id="competitors-title">Sources</h2>
            {loading ? <p role="status">Loading sources…</p> : loadError ? (
              <div>
                <p role="alert">{loadError}</p>
                <button onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>
              </div>
            ) : competitors.length === 0 ? <p className="muted">No sources saved.</p> : competitors.map((competitor) => (
              <CompetitorSection key={competitor.id} competitor={competitor}
                onPage={updatePage} onDelete={deletePage}
                onRemove={(id) => setCompetitors((current) => current.filter((item) => item.id !== id))} />
            ))}
          </section>
        </div>
        <TeamInterestsSettings />
      </div>
    </main>
  );
}

const root = document.getElementById("root");

if (!root) {
  throw new Error("The page is missing its root element.");
}

const search = new URLSearchParams(window.location.search);
const feedbackToken = search.get("feedback");
const view = feedbackToken === null ? search.get("view") : "feedback";
const requestedVersion = search.get("version");
const held = view === "held";
const fromHeld = view === "history" && search.get("from") === "held";
const active = view === "maintainer" || held || fromHeld ? "maintainer" :
  view === "configuration" || (view === "history" && requestedVersion === null) ? "configuration" : "digest";
createRoot(root).render(
  <StrictMode>
    <a className="skip-link" href="#main-content">Skip to content</a>
    <div className="app-layout">
      <SiteNavigation active={active} nested={feedbackToken !== null || view === "history" || held} />
      {feedbackToken !== null ? <SlackFeedback feedbackToken={feedbackToken} /> :
        view === "maintainer" ? <MaintainerSettings slackCallback={search.get("slack")} /> : view === "history" ? (
        <PageHistory pageId={search.get("page") ?? ""} requestedVersion={requestedVersion} fromHeld={fromHeld} />
      ) : view === "configuration" ? <Configuration /> : (
        <Digest held={held} walkthroughOpen={view === "walkthrough"} />
      )}
    </div>
  </StrictMode>,
);
