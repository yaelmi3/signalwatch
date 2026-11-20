import { StrictMode, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

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
  channel_id: string | null;
  enabled: boolean;
};
type SlackChannel = { id: string; name: string };
type AnalysisSettings = { quality: "economy" | "higher_quality" };
type ArticleFeedSettings = {
  source_id: number;
  source_name: string;
  url: string | null;
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
      <div className="history-sidebar">
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
        <article className="history-detail" aria-label="Selected change">
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
      <button type="button" disabled={disabled || deleting} aria-label={`${label} ${target}`}
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
      <p className="page-url">{page.url}</p>
      <p className="muted">{pageStates[page.state]}</p>
      {page.error && <p role="alert">{page.error}</p>}
      <div className="page-actions">
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
      {page.state === "linked" && (
        <a href={`/?view=history&page=${page.id}`} aria-label={`Change history for ${page.url}`}>Change history</a>
      )}
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
      <h2 id={`competitor-${competitor.id}`}>{competitor.name}</h2>
      <DeleteConfirmation kind="source" target={competitor.name} disabled={busy !== null}
        deleting={busy === "delete"} onConfirm={() => void deleteCompetitor()} />
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

function SiteHeader() {
  return (
    <header className="site-header">
      <div className="brand">SignalWatch</div>
      <nav aria-label="Resources">
        <a href="/docs">API docs</a>
      </nav>
    </header>
  );
}

function Digest({ held }: { held: boolean }) {
  const [entries, setEntries] = useState<DigestEntry[] | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    setError(false);
    request<DigestEntry[]>(held ? "/api/digest?view=held" : "/api/digest", { signal })
      .then((entries) => { if (!controller.signal.aborted) setEntries(entries); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [held, loadAttempt]);

  return (
    <main className="configuration">
      {held && <a href="/?view=maintainer">← Maintainer settings</a>}
      <h1>{held ? "Held items" : "Digest"}</h1>
      {error ? (
        <>
          <p role="alert">{held ? "Could not load held items." : "Could not load the digest."}</p>
          <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>
        </>
      ) : entries === null ? (
        <p role="status">{held ? "Loading held items…" : "Loading digest…"}</p>
      ) : entries.length === 0 ? (
        <p role="status">{held ? "No held items yet." : "No items match your team interests yet."}</p>
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
            const showAllReasons = held && isArticle;
            const teams = completed ? analysisTeams.filter(([key]) => showAllReasons
              ? entry.team_reasons[key] !== undefined : entry.relevant_teams.includes(key)) : [];
            const historyPath = isArticle ? null :
              `/?view=history&page=${entry.page_id}&version=${encodeURIComponent(entry.version)}${held ? "&from=held" : ""}`;
            return (
              <li key={isArticle ? `article:${entry.source_id}:${sourceUrl}` : `edit:${entry.page_id}/${entry.version}`}>
                <article className="digest-entry">
                  <header>
                    <div className="digest-entry-meta">
                      <span>{isArticle ? `Via ${entry.source_name}` : entry.source_name}</span>
                      <ChangeTime version={entry.version} />
                    </div>
                    <h2>{isArticle ? entry.title : takeaway ?? "Page changed"}</h2>
                    {isArticle && takeaway && <p>{takeaway}</p>}
                    {!takeaway && !holdReason && (
                      <p className={entry.status === "failed" ? "digest-failed" : "muted"}>
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
                  </footer>
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
    <main className="configuration history-page">
      {fromHeld ? <a href="/?view=held">← Held items</a> : requestedVersion !== null ? <a href="/">← Digest</a> : (
        <a href={source ? `${configurationPath}#competitor-${source.id}` : configurationPath}>← Configuration</a>
      )}
      <div className="history-heading">
        <h1>{linked ? source.name : "Change history"}</h1>
        {linked && <p className="page-url muted">{page.url}</p>}
      </div>
      {error ? (
        <>
          <p role="alert">{error}</p>
          <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>
        </>
      ) : sources === null ? <p role="status">Loading watched page…</p> : linked ? (
        <ChangeHistory pageId={page.id} pageUrl={page.url} requestedVersion={requestedVersion} />
      ) : <p role="status">This watched page is no longer available.</p>}
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
    <main className="configuration">
      <a href={configurationPath}>← Configuration</a>
      <h1>Maintainer settings</h1>
      <p><a href="/?view=held">Review held items</a></p>
      <section aria-labelledby="webhooks-title">
        <h2 id="webhooks-title">changedetection webhooks</h2>
        {status ? (
          <>
            <p role="status"><strong>{status.configured ? "Webhook settings configured" : "Setup required"}</strong></p>
            <p className="muted">{status.configured
              ? "SignalWatch checked the format of the webhook URL and shared secret set on the server."
              : "Set the SignalWatch webhook URL and shared secret on the server."}</p>
          </>
        ) : !error && <p role="status">Loading webhook status…</p>}
        {error && <p role="alert">{error}</p>}
        {error && <button onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>}
      </section>
      <SlackSettings callback={slackCallback} />
      <AnalysisQualitySettings />
      <ArticleDiscoverySettings />
    </main>
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
  const [selectedChannel, setSelectedChannel] = useState<string | null>(null);
  const [action, setAction] = useState("");
  const [feedback, setFeedback] = useState<{ role: "status" | "alert"; text: string } | null>(null);

  const settings = status && "available" in status ? status : null;
  const connected = settings?.available && settings.authenticated && settings.connected;
  const savedChannel = connected ? settings.channel_id : null;
  const savedChannelName = Array.isArray(channels) ? channels.find((channel) => channel.id === savedChannel)?.name : null;
  const channelId = selectedChannel ?? savedChannel ?? "";
  const channelChanged = channelId !== (savedChannel ?? "");

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
        if ("authenticated" in next && next.available && !next.authenticated) {
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
      if (settings?.authenticated) {
        const next = await readSlackStatus();
        if ("error" in next || !next.available || !next.authenticated || !next.connected ||
            next.workspace_name !== settings.workspace_name) {
          setChannels(null);
          setSelectedChannel(null);
          if ("connected" in next && next.connected) setChannelAttempt((attempt) => attempt + 1);
        }
        setStatus(next);
      }
    } finally {
      setAction("");
    }
  }

  function unlock(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const password = new FormData(event.currentTarget).get("password");
    event.currentTarget.reset();
    if (typeof password !== "string" || !password) return;
    void runAction("Unlocking Slack settings…", async () => {
      await post<void>(`${slackPath}/session`, { password });
      const next = await readSlackStatus();
      setChannels(null);
      setSelectedChannel(null);
      setStatus(next);
      if ("authenticated" in next && next.available && !next.authenticated) {
        throw new Error("Could not start a maintainer session. Try again.");
      }
    });
  }

  function connect() {
    void runAction("Opening Slack…", async () => {
      const { url } = await post<{ url: string }>(`${slackPath}/connect`, {});
      const destination = URL.parse(url);
      if (!destination || destination.origin !== "https://slack.com" ||
          destination.pathname !== "/oauth/v2/authorize" || destination.username || destination.password) {
        throw new Error("Slack authorization returned an invalid link.");
      }
      window.location.assign(destination.href);
    });
  }

  function saveDestination(destinationId: string, enabled: boolean, notice: string) {
    void runAction("Saving Slack settings…", async () => {
      const next = await request<SlackStatus>(slackPath, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel_id: destinationId, enabled }),
      });
      setStatus(next);
      if (destinationId === channelId) setSelectedChannel(null);
      setFeedback({ role: "status", text: notice });
    });
  }

  function sendTest() {
    void runAction("Sending test message…", async () => {
      await post<void>(`${slackPath}/test`, {});
      setFeedback({ role: "status", text: "Test message sent to the saved Slack channel." });
    });
  }

  return (
    <section className="slack-settings" aria-labelledby="slack-settings-title">
      <h2 id="slack-settings-title">Slack delivery</h2>
      {!action && !feedback && callback === "error" &&
        <p role="alert">Slack authorization did not complete. Try connecting again.</p>}
      {!action && !feedback && callback === "connected" && settings?.available && (
        !settings.authenticated ? <p role="status">Unlock Slack settings to verify the connection.</p> :
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
      {settings?.available && !settings.authenticated && (
        <form onSubmit={unlock} autoComplete="off">
          <fieldset disabled={Boolean(action)}>
            <label>Maintainer passphrase<input name="password" type="password" required /></label>
            <button type="submit">Unlock Slack settings</button>
          </fieldset>
        </form>
      )}
      {settings?.available && settings.authenticated && !settings.connected && (
        <button type="button" disabled={Boolean(action)} onClick={connect}>Connect Slack</button>
      )}
      {connected && (
        <>
          <p>Workspace <strong>{settings.workspace_name ?? "Slack"}</strong></p>
          <button type="button" disabled={Boolean(action)} onClick={connect}>Reconnect Slack</button>
          {channels === null && <p role="status">Loading Slack channels…</p>}
          {channels && "error" in channels && <p role="alert">{channels.error}</p>}
          {Array.isArray(channels) && channels.length === 0 && (
            <p>No selectable channels. Invite the SignalWatch bot to a channel, then reload channels.</p>
          )}
          {channels && ("error" in channels || channels.length === 0) && (
            <button type="button" disabled={Boolean(action)} onClick={() => {
              setChannels(null);
              setChannelAttempt((attempt) => attempt + 1);
            }}>Reload channels</button>
          )}
          {Array.isArray(channels) && channels.length > 0 && (
            <form onSubmit={(event) => {
              event.preventDefault();
              if (channelId && channelChanged) saveDestination(channelId, false, "Channel saved. Delivery is disabled.");
            }}>
              <fieldset disabled={Boolean(action)}>
                <label>Slack channel
                  <select value={channelId} onChange={(event) => setSelectedChannel(event.target.value)}>
                    <option value="" disabled>Select a channel</option>
                    {savedChannel && !savedChannelName &&
                      <option value={savedChannel}>Saved channel ({savedChannel})</option>}
                    {channels.map((channel) => <option key={channel.id} value={channel.id}>#{channel.name}</option>)}
                  </select>
                </label>
                <button type="submit" disabled={!channelChanged}>Save channel</button>
              </fieldset>
            </form>
          )}
          {savedChannel && (!Array.isArray(channels) || channels.length === 0 || channelChanged) && (
            <p>Saved channel <strong>{savedChannelName ? `#${savedChannelName}` : savedChannel}</strong></p>
          )}
          <p>Delivery is {settings.enabled ? "enabled" : "disabled"}.</p>
          {savedChannel && (
            <fieldset aria-label="Slack delivery controls" disabled={Boolean(action) || channels === null}>
              <button type="button" onClick={sendTest}>Send test message</button>
              <button type="button" disabled={channelChanged && !settings.enabled} onClick={() =>
                saveDestination(savedChannel, !settings.enabled, "Delivery settings saved.")
              }>{settings.enabled ? "Disable delivery" : "Enable delivery"}</button>
            </fieldset>
          )}
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
    <section aria-labelledby="analysis-settings-title">
      <h2 id="analysis-settings-title">Saved page analysis</h2>
      {settings ? (
        <label>
          Analysis quality
          <select value={settings.quality} disabled={saving} onChange={(event) => void saveQuality(event.target.value)}>
            <option value="economy">Economy</option>
            <option value="higher_quality">Higher quality</option>
          </select>
        </label>
      ) : !error && <p role="status">Loading analysis quality…</p>}
      <p className="muted">Applies to upcoming page analyses.</p>
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
    <section aria-labelledby="article-discovery-title">
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
        <button type="submit">{saveState === "saving" ? "Saving…" : "Save feed"}</button>
      </fieldset>
      {saveState === "saved" && <p role="status">Feed settings saved.</p>}
      {error && <p role="alert">{error}</p>}
    </form>
  );
}

function TeamInterestsSettings() {
  const settingsPath = "/api/team-interests";
  const [interests, setInterests] = useState<TeamInterests | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [saveState, setSaveState] = useState<"saving" | "saved" | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
    setError("");
    request<TeamInterests>(settingsPath, { signal })
      .then((interests) => { if (!controller.signal.aborted) setInterests(interests); })
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
    if (interests === null || saveState === "saving") return;
    setSaveState("saving");
    setError("");
    try {
      setInterests(await request<TeamInterests>(settingsPath, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(interests),
        signal: AbortSignal.timeout(8000),
      }));
      setSaveState("saved");
    } catch (cause) {
      setError(`Could not save team interests. ${message(cause)}`);
      setSaveState(null);
    }
  }

  return (
    <section aria-labelledby="team-interests-title">
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
            <button type="submit">{saveState === "saving" ? "Saving…" : "Save team interests"}</button>
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
    <main className="configuration">
      <h1>Configuration</h1>
      <p className="muted">Add sources and choose which pages to monitor.</p>
      <a href="/?view=maintainer">Maintainer settings</a>
      <TeamInterestsSettings />
      <section aria-labelledby="add-competitor-title">
        <h2 id="add-competitor-title">Add source</h2>
        <form onSubmit={addCompetitor}>
          <fieldset disabled={saving || loading || Boolean(loadError)}>
            <label>Source name<input name="name" required /></label>
            <label>First page to watch
              <input name="website" type="url" placeholder="https://example.com" required />
            </label>
            <button type="submit">{saving ? "Saving…" : "Add source"}</button>
          </fieldset>
          {saveError && <p role="alert">{saveError}</p>}
          {saved && <p role="status">Source and first page saved.</p>}
        </form>
      </section>
      <section aria-labelledby="competitors-title">
        <h2 id="competitors-title">Sources</h2>
        {loading ? <p role="status">Loading sources…</p> : loadError ? (
          <div>
            <p role="alert">{loadError}</p>
            <button onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>
          </div>
        ) : competitors.length === 0 ? <p>No sources saved.</p> : competitors.map((competitor) => (
          <CompetitorSection key={competitor.id} competitor={competitor}
            onPage={updatePage} onDelete={deletePage}
            onRemove={(id) => setCompetitors((current) => current.filter((item) => item.id !== id))} />
        ))}
      </section>
    </main>
  );
}

const root = document.getElementById("root");

if (!root) {
  throw new Error("The page is missing its root element.");
}

const search = new URLSearchParams(window.location.search);
const view = search.get("view");
const requestedVersion = search.get("version");
const held = view === "held";
const fromHeld = view === "history" && search.get("from") === "held";
const configurationActive = view === "configuration" || view === "maintainer" || held ||
  (view === "history" && (requestedVersion === null || fromHeld));
const current = view === "history" || view === "maintainer" || held ? "true" : "page";
createRoot(root).render(
  <StrictMode>
    <SiteHeader />
    <div className="app-layout">
      <nav className="app-nav" aria-label="Main navigation">
        <a href="/" aria-current={configurationActive ? undefined : current}>Digest</a>
        <a href={configurationPath} aria-current={configurationActive ? current : undefined}>Configuration</a>
      </nav>
      {view === "maintainer" ? <MaintainerSettings slackCallback={search.get("slack")} /> : view === "history" ? (
        <PageHistory pageId={search.get("page") ?? ""} requestedVersion={requestedVersion} fromHeld={fromHeld} />
      ) : view === "configuration" ? <Configuration /> : <Digest held={held} />}
    </div>
  </StrictMode>,
);
