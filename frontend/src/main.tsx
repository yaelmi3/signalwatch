import { StrictMode, useEffect, useState, type FormEvent } from "react";
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

function request<T>(path: string, options?: RequestInit): Promise<T>;
async function request(path: string, options?: RequestInit): Promise<unknown> {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = body && typeof body === "object" && "detail" in body ? body.detail : null;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status}).`);
  }
  if (response.status === 204) return;
  if (options?.method === "DELETE") throw new Error(`Unexpected deletion response (${response.status}).`);
  return response.json();
}

function post<T>(path: string, body?: object) {
  return request<T>(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}

function message(error: unknown) {
  return error instanceof Error ? error.message : "The request could not be completed. Please try again.";
}

function PageItem({ page, onChange, onDelete }: {
  page: Page; onChange: (page: Page) => void; onDelete: (page: Page) => void;
}) {
  const [busy, setBusy] = useState<"connect" | "status" | "delete" | null>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState<WatchStatus | null>(null);

  async function run(action: "connect" | "status" | "delete") {
    if (action === "delete" && !window.confirm(
      `Delete page ${page.url} and its changedetection watch and history? This cannot be undone.`,
    )) return;
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
          <button disabled={busy !== null} onClick={() => void run("connect")}>
            {busy === "connect" ? "Connecting…" : "Retry connection"}
          </button>
        )}
        <button disabled={busy !== null} onClick={() => void run("status")}>
          {busy === "status" ? "Refreshing…" : "Refresh status"}
        </button>
        <button disabled={busy !== null} onClick={() => void run("delete")}
          aria-label={`Delete page ${page.url}`}>
          {busy === "delete" ? "Deleting…" : "Delete page"}
        </button>
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

function CompetitorSection({ competitor, onPage, onDelete }: {
  competitor: Competitor; onPage: (page: Page) => void; onDelete: (page: Page) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  async function addPage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const url = String(new FormData(form).get("url") ?? "");
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      onPage(await post<Page>(`/api/competitors/${competitor.id}/pages`, { url }));
      form.reset();
      setSaved(true);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="competitor" aria-labelledby={`competitor-${competitor.id}`}>
      <h2 id={`competitor-${competitor.id}`}>{competitor.name}</h2>
      <p className="muted">{competitor.website}</p>
      {competitor.pages.length ? (
        <ul className="page-list">{competitor.pages.map((page) => (
          <PageItem key={page.id} page={page} onChange={onPage} onDelete={onDelete} />
        ))}</ul>
      ) : <p>No pages saved.</p>}
      <form onSubmit={addPage} aria-label={`Add page for ${competitor.name}`}>
        <fieldset disabled={busy}>
          <label>Page URL
            <input name="url" type="url" placeholder="https://example.com/updates" required />
          </label>
          <button type="submit">{busy ? "Saving…" : "Add page"}</button>
        </fieldset>
        {error && <p role="alert">{error}</p>}
        {saved && <p role="status">Page saved.</p>}
      </form>
    </section>
  );
}

function App() {
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
    <>
      <header className="site-header">
        <div className="brand">SignalWatch</div>
        <nav aria-label="Resources">
          <a href="/docs">API docs</a>
        </nav>
      </header>
      <main className="configuration">
        <h1>Configuration</h1>
        <p className="muted">Save competitors and the pages you want to monitor.</p>
        <section aria-labelledby="add-competitor-title">
          <h2 id="add-competitor-title">Add competitor</h2>
          <form onSubmit={addCompetitor}>
            <fieldset disabled={saving || loading || Boolean(loadError)}>
              <label>Name<input name="name" required /></label>
              <label>Website
                <input name="website" type="url" placeholder="https://example.com" required />
              </label>
              <button type="submit">{saving ? "Saving…" : "Add competitor"}</button>
            </fieldset>
            {saveError && <p role="alert">{saveError}</p>}
            {saved && <p role="status">Competitor saved, including its homepage.</p>}
          </form>
        </section>
        <section aria-labelledby="competitors-title">
          <h2 id="competitors-title">Competitors</h2>
          {loading ? <p role="status">Loading competitors…</p> : loadError ? (
            <div>
              <p role="alert">{loadError}</p>
              <button onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry loading</button>
            </div>
          ) : competitors.length === 0 ? <p>No competitors saved.</p> : competitors.map((competitor) => (
            <CompetitorSection key={competitor.id} competitor={competitor}
              onPage={updatePage} onDelete={deletePage} />
          ))}
        </section>
      </main>
    </>
  );
}

const root = document.getElementById("root");

if (!root) {
  throw new Error("The page is missing its root element.");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
