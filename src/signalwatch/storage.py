import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, BaseModel, Field, HttpUrl, StringConstraints, model_validator

from signalwatch.analysis_models import (
    AnalysisQuality,
    ChangeAnalysisResult,
    PageChangeAnalysis,
    TeamInterest,
    TeamInterests,
)

_COMPETITORS_SCHEMA = "(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, website TEXT NOT NULL)"
_PAGES_SCHEMA = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT, competitor_id INTEGER NOT NULL REFERENCES competitors(id), url TEXT NOT NULL,
    watch_id TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT, attempted_at TEXT,
    UNIQUE (competitor_id, url)
)"""
_PAGE_CHANGES_SCHEMA = """(
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (typeof(version) = 'integer'), before TEXT, after TEXT NOT NULL,
    PRIMARY KEY (page_id, version)
)"""
_HISTORY_GAPS_SCHEMA = """(
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    watch_id TEXT NOT NULL,
    PRIMARY KEY (page_id, watch_id)
)"""
_CHANGE_ANALYSES_SCHEMA = """(
    page_id INTEGER NOT NULL, version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'completed', 'failed')), result TEXT,
    PRIMARY KEY (page_id, version),
    FOREIGN KEY (page_id, version) REFERENCES page_changes(page_id, version) ON DELETE CASCADE
)"""
_ANALYSIS_SETTINGS_SCHEMA = """(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    quality TEXT NOT NULL CHECK (quality IN ('economy', 'higher_quality'))
)"""
_TEAM_INTERESTS_SCHEMA = """(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    marketing TEXT NOT NULL, product TEXT NOT NULL, research_and_development TEXT NOT NULL,
    business_context TEXT NOT NULL DEFAULT ''
)"""
_ARTICLE_FEEDS_SCHEMA = """(
    source_id INTEGER PRIMARY KEY REFERENCES competitors(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    seeded INTEGER NOT NULL DEFAULT 0 CHECK (seeded IN (0, 1))
)"""
_DISCOVERED_ARTICLES_SCHEMA = """(
    source_id INTEGER NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    article_url TEXT NOT NULL, title TEXT NOT NULL, discovered_at INTEGER,
    PRIMARY KEY (source_id, article_url)
)"""
_DISCOVERED_ARTICLE_ANALYSES_SCHEMA = """(
    source_id INTEGER NOT NULL, article_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'completed', 'failed')), result TEXT,
    PRIMARY KEY (source_id, article_url),
    FOREIGN KEY (source_id, article_url) REFERENCES discovered_articles(source_id, article_url) ON DELETE CASCADE
)"""
_FEEDBACK_SCHEMA = """(
    id TEXT PRIMARY KEY NOT NULL, team TEXT NOT NULL, judgment TEXT NOT NULL, url TEXT NOT NULL, note TEXT,
    page_id INTEGER, version TEXT, source_id INTEGER, occurred_on TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)"""
_APPLIED_FEEDBACK_SCHEMA = "(feedback_id TEXT PRIMARY KEY NOT NULL REFERENCES feedback(id) ON DELETE CASCADE)"
_ARTICLE_EVIDENCE_SCHEMA = """(
    source_id INTEGER NOT NULL, article_url TEXT NOT NULL, publisher_text TEXT NOT NULL,
    PRIMARY KEY (source_id, article_url),
    FOREIGN KEY (source_id, article_url) REFERENCES discovered_articles(source_id, article_url) ON DELETE CASCADE
)"""
_SUGGESTIONS_SCHEMA = """(
    id TEXT PRIMARY KEY NOT NULL, team TEXT NOT NULL UNIQUE, baseline TEXT NOT NULL, result TEXT NOT NULL
)"""
_SLACK_DELIVERIES_SCHEMA = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL,
    page_id INTEGER, version INTEGER, source_id INTEGER, article_url TEXT, teams TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed', 'uncertain', 'skipped')),
    error TEXT,
    CHECK ((page_id IS NOT NULL AND version IS NOT NULL AND source_id IS NULL AND article_url IS NULL)
        OR (source_id IS NOT NULL AND article_url IS NOT NULL AND page_id IS NULL AND version IS NULL)),
    UNIQUE (channel_id, page_id, version), UNIQUE (channel_id, source_id, article_url),
    FOREIGN KEY (page_id, version) REFERENCES change_analyses(page_id, version) ON DELETE CASCADE,
    FOREIGN KEY (source_id, article_url)
        REFERENCES discovered_article_analyses(source_id, article_url) ON DELETE CASCADE
)"""
_SLACK_CONNECTION_SCHEMA = """(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    workspace_name TEXT NOT NULL, encrypted_token TEXT NOT NULL,
    team_channels TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    CHECK (enabled = 0 OR team_channels <> '{}')
)"""
_DIGEST_LIMIT = 50


class PageState(StrEnum):
    PENDING = "pending"
    CREATING = "creating"
    LINKED = "linked"
    ATTENTION = "attention"


class Page(BaseModel):
    id: int
    competitor_id: int
    url: str
    watch_id: str | None
    state: PageState
    error: str | None


class PageChange(BaseModel):
    version: str
    before: str
    after: str


class DigestTeam(StrEnum):
    MARKETING = "marketing"
    PRODUCT = "product"
    RESEARCH_AND_DEVELOPMENT = "research_and_development"


class DigestView(StrEnum):
    RELEVANT = "relevant"
    HELD = "held"


class _DigestAnalysis(BaseModel):
    version: str
    status: Literal["pending", "queued", "completed", "failed"]
    summary: str | None
    relevant_teams: list[DigestTeam]
    team_reasons: dict[DigestTeam, str]


class DigestEntry(_DigestAnalysis):
    page_id: int
    source_name: str
    page_url: str


class DigestArticle(_DigestAnalysis):
    kind: Literal["article"] = "article"
    source_id: int
    source_name: str
    title: str
    article_url: str
    hold_reason: Literal["business_fit_unverified"] | None = Field(
        default=None, exclude_if=lambda reason: reason is None
    )


type FeedbackItemId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class FeedbackInput(BaseModel):
    id: UUID
    team: DigestTeam
    judgment: Literal["useful", "noisy", "missed"]
    url: Annotated[str, StringConstraints(strip_whitespace=True)]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    page_id: FeedbackItemId | None = None
    version: Annotated[str, StringConstraints(pattern=r"^(0|-?[1-9][0-9]{0,18})$")] | None = None
    source_id: FeedbackItemId | None = None
    occurred_on: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None = None

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        has_item_reference = self.page_id is not None or self.source_id is not None
        # Saved page URLs can grow during percent encoding.
        url = AnyHttpUrl(self.url) if has_item_reference else HttpUrl(self.url)
        if not self.url.lower().startswith(("http://", "https://")) or url.username or url.password:
            raise ValueError("Use a plain HTTP(S) URL without credentials.")
        if (self.page_id is None) != (self.version is None) or (
            self.page_id is not None and self.source_id is not None
        ):
            raise ValueError("Use either a page and version or an article source.")
        if not has_item_reference and self.judgment != "missed":
            raise ValueError("Only missed updates can be reported by URL alone.")
        if self.occurred_on is not None:
            date.fromisoformat(self.occurred_on)
        return self


class Feedback(FeedbackInput):
    created_at: str


class SuggestionCase(BaseModel):
    title: str
    url: str
    expected: bool | None = None
    current: list[DigestTeam] | None = None
    proposed: list[DigestTeam] | None = None


class Suggestion(BaseModel):
    id: str
    team: DigestTeam
    status: Literal["queued", "ready", "failed", "applied"]
    current_interest: TeamInterest
    proposed_interest: TeamInterest | None = None
    error: str | None = None
    cases: list[SuggestionCase]


class _SuggestionArticle(BaseModel):
    source_id: int
    title: str
    url: str
    expected: bool | None
    note: str | None
    judgment: str | None


class SuggestionCaseInput(_SuggestionArticle):
    publisher_text: str


class _SuggestionBaseline(BaseModel):
    interests: TeamInterests
    quality: AnalysisQuality
    cases: list[_SuggestionArticle]
    feedback_id: str | None = None


@dataclass(frozen=True)
class SuggestionInputs:
    team: DigestTeam
    interests: TeamInterests
    quality: AnalysisQuality
    cases: list[SuggestionCaseInput]


@dataclass(frozen=True)
class Article:
    title: str
    article_url: str


@dataclass(frozen=True)
class ArticleFeed:
    source_id: int
    url: str


@dataclass(frozen=True)
class SlackDelivery:
    id: int
    channel_id: str
    item: DigestEntry | DigestArticle
    teams: list[DigestTeam]


@dataclass(frozen=True)
class SlackConnection:
    workspace_name: str
    encrypted_token: str = field(repr=False)
    team_channels: dict[DigestTeam, str]
    enabled: bool

    @property
    def revision(self) -> str:
        snapshot = json.dumps((self.encrypted_token, self.team_channels, self.enabled), sort_keys=True)
        return sha256(snapshot.encode()).hexdigest()


class ArticleFeedSettings(BaseModel):
    source_id: int
    source_name: str
    website: str
    url: str | None


@dataclass(frozen=True)
class PageSnapshot:
    version: int
    after: str


@dataclass(frozen=True)
class ChangeToAnalyze:
    before: str
    after: str


@dataclass(frozen=True)
class LinkedWatch:
    page_id: int
    watch_id: str


class Competitor(BaseModel):
    id: int
    name: str
    website: str
    pages: list[Page]


@dataclass
class CompetitorSnapshot:
    competitor: sqlite3.Row
    pages: list[sqlite3.Row]


def _competitor_snapshot(database: sqlite3.Connection, competitor_id: int) -> CompetitorSnapshot | None:
    competitor = database.execute("SELECT * FROM competitors WHERE id = ?", (competitor_id,)).fetchone()
    if competitor is None:
        return None
    pages = database.execute("SELECT * FROM pages WHERE competitor_id = ? ORDER BY id", (competitor_id,)).fetchall()
    return CompetitorSnapshot(competitor, pages)


def competitor_snapshot(competitor_id: int) -> CompetitorSnapshot | None:
    with _database() as database:
        database.execute("BEGIN")
        return _competitor_snapshot(database, competitor_id)


def delete_competitor(snapshot: CompetitorSnapshot) -> bool:
    competitor_id = snapshot.competitor["id"]
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        current = _competitor_snapshot(database, competitor_id)
        if current is None:
            return True
        if current != snapshot:
            return False
        database.execute("DELETE FROM pages WHERE competitor_id = ?", (competitor_id,))
        database.execute("DELETE FROM competitors WHERE id = ?", (competitor_id,))
    return True


def get_page(page_id: int) -> Page | None:
    with _database() as database:
        row = database.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    return Page.model_validate(dict(row)) if row is not None else None


def get_linked_page_id(watch_id: str) -> int | None:
    with _database() as database:
        row = database.execute(
            "SELECT id FROM pages WHERE watch_id = ? AND state = ?", (watch_id, PageState.LINKED)
        ).fetchone()
    return row["id"] if row is not None else None


def list_linked_watches() -> list[LinkedWatch]:
    with _database() as database:
        rows = database.execute(
            "SELECT id, watch_id FROM pages WHERE state = ? AND watch_id IS NOT NULL ORDER BY id", (PageState.LINKED,)
        ).fetchall()
    return [LinkedWatch(page_id=row["id"], watch_id=row["watch_id"]) for row in rows]


def list_page_changes(page_id: int) -> list[PageChange]:
    with _database() as database:
        rows = database.execute(
            """SELECT CAST(version AS TEXT) AS version, before, after FROM page_changes
            WHERE page_id = ? AND before IS NOT NULL ORDER BY page_changes.version DESC""",
            (page_id,),
        ).fetchall()
    return [PageChange.model_validate(dict(row)) for row in rows]


_DIGEST_ITEMS_SQL = """WITH items AS (
    SELECT changes.page_id, changes.version, competitors.id AS source_id, competitors.name AS source_name,
           pages.url AS page_url,
           NULL AS title, NULL AS article_url, analyses.status, analyses.result,
           1 AS business_fit
    FROM page_changes AS changes
    JOIN pages ON pages.id = changes.page_id
    JOIN competitors ON competitors.id = pages.competitor_id
    LEFT JOIN change_analyses AS analyses
        ON analyses.page_id = changes.page_id AND analyses.version = changes.version
    WHERE changes.before IS NOT NULL AND pages.state = :linked AND pages.watch_id IS NOT NULL
    UNION ALL
    SELECT NULL, articles.discovered_at, articles.source_id, competitors.name, NULL, articles.title,
           articles.article_url, analyses.status, analyses.result,
           COALESCE(json_type(analyses.result, '$.change_quote') = 'text'
               AND TRIM(json_extract(analyses.result, '$.change_quote')) <> ''
               AND EXISTS (SELECT 1 FROM team_interests WHERE TRIM(business_context) <> ''), 0)
    FROM discovered_articles AS articles
    JOIN competitors ON competitors.id = articles.source_id
    LEFT JOIN discovered_article_analyses AS analyses USING (source_id, article_url)
    WHERE articles.discovered_at IS NOT NULL
)"""

_DIGEST_RELEVANCE_SQL = """COALESCE(business_fit AND status = 'completed' AND (
    json_extract(result, '$.marketing.relevant') OR json_extract(result, '$.product.relevant')
    OR json_extract(result, '$.research_and_development.relevant')
), 0)"""


def list_digest(view: DigestView = DigestView.RELEVANT) -> list[DigestEntry | DigestArticle]:
    with _database() as database:
        rows = database.execute(
            f"""{_DIGEST_ITEMS_SQL} SELECT * FROM items
            WHERE {_DIGEST_RELEVANCE_SQL} = :relevant
            ORDER BY version DESC, article_url IS NOT NULL, page_id, source_id, article_url LIMIT :limit""",
            {"linked": PageState.LINKED, "relevant": view == DigestView.RELEVANT, "limit": _DIGEST_LIMIT},
        ).fetchall()
    return [_digest_entry(row, view) for row in rows]


def _digest_entry(row: sqlite3.Row, view: DigestView) -> DigestEntry | DigestArticle:
    is_article = row["article_url"] is not None
    analysis = (
        ChangeAnalysisResult.model_validate_json(row["result"])
        if row["status"] == "completed" and row["result"] is not None
        else None
    )
    hold_reason = "business_fit_unverified" if is_article and analysis is not None and not row["business_fit"] else None
    decisions = analysis.model_dump(exclude={"summary"}) if analysis and hold_reason is None else {}
    relevant_teams = [team for team in DigestTeam if team in decisions and decisions[team]["relevant"]]
    reason_teams = list(decisions) if is_article and view == DigestView.HELD else relevant_teams
    item = dict(
        row,
        version=str(row["version"]),
        status=row["status"] or "pending",
        summary=analysis.summary if analysis else None,
        hold_reason=hold_reason,
        relevant_teams=relevant_teams,
        team_reasons={team: decisions[team]["reason"] for team in reason_teams},
    )
    model = DigestArticle if is_article else DigestEntry
    return model.model_validate(item)


def list_feedback() -> list[Feedback]:
    with _database() as database:
        rows = database.execute("SELECT * FROM feedback ORDER BY rowid DESC LIMIT 50").fetchall()
    return [Feedback.model_validate(dict(row)) for row in rows]


def save_feedback(feedback: FeedbackInput) -> None:
    fields = feedback.model_dump(mode="json")
    with _database() as database:
        if database.execute(
            """INSERT INTO feedback (id, team, judgment, url, note, page_id, version, source_id, occurred_on)
            SELECT :id, :team, :judgment, :url, :note, :page_id, :version, :source_id, :occurred_on
            WHERE CASE
                WHEN :page_id IS NOT NULL THEN EXISTS (
                    SELECT 1 FROM page_changes AS changes JOIN pages ON pages.id = changes.page_id
                    WHERE changes.page_id = :page_id AND changes.version = :version
                        AND changes.before IS NOT NULL AND pages.url = :url
                )
                WHEN :source_id IS NOT NULL THEN EXISTS (
                    SELECT 1 FROM discovered_articles WHERE source_id = :source_id AND article_url = :url
                        AND discovered_at IS NOT NULL
                )
                ELSE 1
            END ON CONFLICT (id) DO NOTHING""",
            fields,
        ).rowcount:
            return
        existing = database.execute("SELECT * FROM feedback WHERE id = ?", (fields["id"],)).fetchone()
        if existing is None:
            raise LookupError("The saved update was not found.")
        if any(existing[key] != value for key, value in fields.items()):
            raise ValueError("This feedback ID is already in use.")


_SUGGESTION_CASES_SQL = """WITH votes AS (
    SELECT source_id, url, COALESCE(
        MAX(feedback.rowid) FILTER (WHERE applied.feedback_id IS NULL AND judgment <> 'useful'),
        MAX(feedback.rowid) FILTER (WHERE applied.feedback_id IS NULL)
    ) AS latest,
           CASE WHEN MIN(judgment <> 'noisy') = MAX(judgment <> 'noisy') THEN MAX(judgment <> 'noisy') END AS expected
    FROM feedback LEFT JOIN applied_feedback AS applied ON applied.feedback_id = feedback.id
    WHERE team = :team AND source_id IS NOT NULL GROUP BY source_id, url
), cases AS (
    SELECT articles.source_id, articles.title, articles.article_url AS url, articles.discovered_at, votes.latest,
           votes.expected, feedback.id AS feedback_id, feedback.note, feedback.judgment,
           COALESCE(:business_fit AND json_type(analyses.result, '$.change_quote') = 'text'
                    AND TRIM(json_extract(analyses.result, '$.change_quote')) <> ''
                    AND json_extract(analyses.result, :team_path), 0) AS shown
    FROM article_evidence AS evidence JOIN discovered_articles AS articles USING (source_id, article_url)
    JOIN discovered_article_analyses AS analyses USING (source_id, article_url)
    LEFT JOIN votes ON votes.source_id = articles.source_id AND votes.url = articles.article_url
    LEFT JOIN feedback ON feedback.rowid = votes.latest
    WHERE articles.discovered_at IS NOT NULL AND analyses.status = 'completed'
)"""


def get_suggestion(team: DigestTeam) -> Suggestion | None:
    with _database() as database:
        row = database.execute("SELECT result FROM suggestions WHERE team = ?", (team,)).fetchone()
    return Suggestion.model_validate_json(row["result"]) if row is not None else None


def start_suggestion(team: DigestTeam) -> Suggestion:
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute("SELECT result FROM suggestions WHERE team = ?", (team,)).fetchone()
        if row is not None:
            existing = Suggestion.model_validate_json(row["result"])
            if existing.status in ("queued", "ready"):
                return existing

        baseline = _suggestion_baseline(database, team)
        suggestion = Suggestion(
            id=str(uuid4()),
            team=team,
            status="queued",
            current_interest=getattr(baseline.interests, team.value),
            cases=[SuggestionCase(title=case.title, url=case.url, expected=case.expected) for case in baseline.cases],
        )
        database.execute(
            """INSERT INTO suggestions (id, team, baseline, result) VALUES (?, ?, ?, ?)
            ON CONFLICT (team) DO UPDATE SET
                id = excluded.id, baseline = excluded.baseline, result = excluded.result""",
            (suggestion.id, team, baseline.model_dump_json(), suggestion.model_dump_json()),
        )
        return suggestion


def _suggestion_baseline(database: sqlite3.Connection, team: DigestTeam) -> _SuggestionBaseline:
    interests = _team_interests(database)
    bindings = {"team": team, "team_path": f"$.{team.value}.relevant", "business_fit": bool(interests.business_context)}
    seed = database.execute(
        f"""{_SUGGESTION_CASES_SQL} SELECT * FROM cases WHERE latest IS NOT NULL
        ORDER BY expected IS NULL, judgment = 'useful', latest DESC LIMIT 1""",
        bindings,
    ).fetchone()
    if seed is None:
        raise ValueError("Give new feedback on a completed article with saved evidence before suggesting a change.")
    if seed["expected"] is None:
        raise ValueError("The team’s feedback disagrees on whether to show the available articles.")
    cases = [_SuggestionArticle.model_validate(dict(seed))]
    for shown in (True, False):
        row = database.execute(
            f"""{_SUGGESTION_CASES_SQL} SELECT * FROM cases WHERE shown = :shown AND url NOT IN (:seed, :last)
            ORDER BY expected IS TRUE DESC, discovered_at DESC, url, source_id LIMIT 1""",
            {**bindings, "shown": shown, "seed": cases[0].url, "last": cases[-1].url},
        ).fetchone()
        if row is not None:
            cases.append(_SuggestionArticle.model_validate(dict(row)))
    return _SuggestionBaseline(
        interests=interests, quality=_analysis_quality(database), cases=cases, feedback_id=seed["feedback_id"]
    )


def list_queued_suggestions() -> list[str]:
    with _database() as database:
        rows = database.execute("SELECT id FROM suggestions WHERE json_extract(result, '$.status') = 'queued'")
        return [row["id"] for row in rows]


def _case_evidence(database: sqlite3.Connection, case: _SuggestionArticle) -> str | None:
    row = database.execute(
        "SELECT publisher_text FROM article_evidence WHERE source_id = ? AND article_url = ?",
        (case.source_id, case.url),
    ).fetchone()
    return row["publisher_text"] if row is not None else None


def get_suggestion_inputs(suggestion_id: str) -> SuggestionInputs | None:
    with _database() as database:
        row = database.execute(
            "SELECT team, baseline FROM suggestions WHERE id = ? AND json_extract(result, '$.status') = 'queued'",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            return None
        baseline = _SuggestionBaseline.model_validate_json(row["baseline"])
        cases = []
        for case in baseline.cases:
            publisher_text = _case_evidence(database, case)
            if publisher_text is None:
                break
            cases.append(SuggestionCaseInput(**case.model_dump(), publisher_text=publisher_text))
        else:
            return SuggestionInputs(DigestTeam(row["team"]), baseline.interests, baseline.quality, cases)
    finish_suggestion(suggestion_id, None, [], "Saved article evidence is unavailable.")
    return None


def finish_suggestion(
    suggestion_id: str, proposed_interest: str | None, cases: list[SuggestionCase], error: str | None
) -> None:
    with _database() as database:
        row = database.execute(
            "SELECT result FROM suggestions WHERE id = ? AND json_extract(result, '$.status') = 'queued'",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            return
        suggestion = Suggestion.model_validate_json(row["result"])
        if [(case.url, case.expected) for case in cases] != [(case.url, case.expected) for case in suggestion.cases]:
            cases = suggestion.cases
        if not error and (
            proposed_interest is None or any(case.current is None or case.proposed is None for case in cases)
        ):
            error = "The comparison is incomplete."
        fields = {
            "status": "failed" if error else "ready",
            "proposed_interest": proposed_interest,
            "error": error,
            "cases": cases,
        }
        result = Suggestion.model_validate(suggestion.model_dump() | fields)
        database.execute(
            "UPDATE suggestions SET result = ? WHERE id = ? AND json_extract(result, '$.status') = 'queued'",
            (result.model_dump_json(), suggestion_id),
        )


def apply_suggestion(suggestion_id: str) -> Suggestion:
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute("SELECT baseline, result FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None:
            raise LookupError("The suggestion is no longer available.")
        suggestion = Suggestion.model_validate_json(row["result"])
        if suggestion.status == "applied":
            return suggestion
        if (
            suggestion.status != "ready"
            or suggestion.proposed_interest is None
            or any(case.current is None or case.proposed is None for case in suggestion.cases)
        ):
            raise ValueError("Complete the comparison before applying the suggestion.")
        baseline = _SuggestionBaseline.model_validate_json(row["baseline"])
        if baseline.interests != _team_interests(database) or baseline.quality != _analysis_quality(database):
            raise ValueError("Settings changed since this suggestion started. Dismiss it and request another.")
        if any(_case_evidence(database, case) is None for case in baseline.cases):
            raise ValueError("Saved article evidence is unavailable. Dismiss this suggestion and request another.")
        interests = baseline.interests.model_copy(update={suggestion.team.value: suggestion.proposed_interest})
        _save_team_interests(database, interests)
        if baseline.feedback_id is not None:
            database.execute("INSERT INTO applied_feedback (feedback_id) VALUES (?)", (baseline.feedback_id,))
        applied = suggestion.model_copy(update={"status": "applied"})
        database.execute("UPDATE suggestions SET result = ? WHERE id = ?", (applied.model_dump_json(), suggestion_id))
        return applied


def dismiss_suggestion(suggestion_id: str) -> None:
    with _database() as database:
        database.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))


def claim_slack_delivery(
    team_channels: dict[DigestTeam, str], connection: SlackConnection | None = None
) -> SlackDelivery | None:
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        if connection is not None:
            current = _slack_connection(database.execute("SELECT * FROM slack_connection WHERE id = 1").fetchone())
            if not connection.enabled or current != connection:
                return None
        # Persist an uncertain outcome before the request so a restart cannot repeat a possibly accepted post.
        claim = database.execute(
            """UPDATE slack_deliveries SET status = 'uncertain', error = 'result_not_recorded'
            WHERE id = (
                SELECT id FROM slack_deliveries WHERE status = 'pending'
                AND channel_id IN (SELECT value FROM json_each(?))
                ORDER BY id LIMIT 1
            )
            RETURNING id, channel_id, page_id, version, source_id, article_url, teams""",
            (json.dumps(team_channels),),
        ).fetchone()
        if claim is None:
            return None
        target = _relevant_digest_item(
            database,
            page_id=claim["page_id"],
            version=claim["version"],
            source_id=claim["source_id"],
            article_url=claim["article_url"],
        )
        if target is not None:
            item = _digest_entry(target, DigestView.RELEVANT)
            channel_id = claim["channel_id"]
            queued_teams = set(json.loads(claim["teams"]))
            if teams := [team for team in item.relevant_teams
                         if team in queued_teams and team_channels.get(team) == channel_id]:
                return SlackDelivery(claim["id"], channel_id, item, teams)
        database.execute(
            "UPDATE slack_deliveries SET status = 'skipped', error = 'no_longer_eligible' WHERE id = ?",
            (claim["id"],),
        )
        return None


def _slack_connection(row: sqlite3.Row | None) -> SlackConnection | None:
    if row is None:
        return None
    team_channels = {DigestTeam(team): channel for team, channel in json.loads(row["team_channels"]).items()}
    return SlackConnection(row["workspace_name"], row["encrypted_token"], team_channels, bool(row["enabled"]))


def get_slack_connection() -> SlackConnection | None:
    with _database() as database:
        return _slack_connection(database.execute("SELECT * FROM slack_connection WHERE id = 1").fetchone())


def save_slack_connection(workspace_name: str, encrypted_token: str) -> None:
    with _database() as database:
        database.execute(
            """INSERT INTO slack_connection (id, workspace_name, encrypted_token) VALUES (1, ?, ?)
            ON CONFLICT (id) DO UPDATE SET workspace_name = excluded.workspace_name,
                encrypted_token = excluded.encrypted_token, team_channels = '{}', enabled = 0""",
            (workspace_name, encrypted_token),
        )


def save_slack_destination(
    connection: SlackConnection, team_channels: dict[DigestTeam, str], enabled: bool
) -> SlackConnection | None:
    with _database() as database:
        row = database.execute(
            """UPDATE slack_connection SET team_channels = ?, enabled = ?
            WHERE id = 1 AND encrypted_token = ? AND json(team_channels) = json(?) AND enabled = ?
            RETURNING *""",
            (json.dumps(team_channels), enabled, connection.encrypted_token,
             json.dumps(connection.team_channels), connection.enabled),
        ).fetchone()
    return _slack_connection(row)


def finish_slack_delivery(
    delivery_id: int, status: Literal["sent", "failed", "uncertain"], error: str | None = None
) -> None:
    with _database() as database:
        database.execute("UPDATE slack_deliveries SET status = ?, error = ? WHERE id = ?", (status, error, delivery_id))


def _relevant_digest_item(
    database: sqlite3.Connection,
    *,
    page_id: int | None = None,
    version: int | None = None,
    source_id: int | None = None,
    article_url: str | None = None,
) -> sqlite3.Row | None:
    return database.execute(
        f"""{_DIGEST_ITEMS_SQL} SELECT * FROM items WHERE {_DIGEST_RELEVANCE_SQL}
        AND ((page_id = :page_id AND version = :version) OR (source_id = :source_id AND article_url = :article_url))""",
        {
            "linked": PageState.LINKED,
            "page_id": page_id,
            "version": version,
            "source_id": source_id,
            "article_url": article_url,
        },
    ).fetchone()


def _enqueue_slack_delivery(
    database: sqlite3.Connection,
    team_channels: dict[DigestTeam, str],
    *,
    page_id: int | None = None,
    version: int | None = None,
    source_id: int | None = None,
    article_url: str | None = None,
) -> None:
    target = _relevant_digest_item(
        database, page_id=page_id, version=version, source_id=source_id, article_url=article_url
    )
    if target is None:
        return
    item = _digest_entry(target, DigestView.RELEVANT)
    channel_teams: dict[str, list[DigestTeam]] = {}
    for team in item.relevant_teams:
        if (channel := team_channels.get(team)) is not None:
            channel_teams.setdefault(channel, []).append(team)
    database.executemany(
        """INSERT INTO slack_deliveries (channel_id, page_id, version, source_id, article_url, teams)
        VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        [(channel, page_id, version, source_id, article_url, json.dumps(teams))
         for channel, teams in channel_teams.items()],
    )


def list_article_feed_settings() -> list[ArticleFeedSettings]:
    with _database() as database:
        rows = database.execute(
            """SELECT competitors.id AS source_id, competitors.name AS source_name,
                      competitors.website, article_feeds.url
            FROM competitors LEFT JOIN article_feeds ON competitors.id = article_feeds.source_id
            ORDER BY competitors.id"""
        ).fetchall()
    return [ArticleFeedSettings.model_validate(dict(row)) for row in rows]


def set_article_feed(*, source_id: int, url: str | None) -> ArticleFeedSettings | None:
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        source = database.execute("SELECT name, website FROM competitors WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            return None
        if url is None:
            database.execute("DELETE FROM article_feeds WHERE source_id = ?", (source_id,))
        else:
            database.execute(
                """INSERT INTO article_feeds (source_id, url) VALUES (?, ?)
                ON CONFLICT (source_id) DO UPDATE SET url = excluded.url,
                    seeded = CASE WHEN article_feeds.url = excluded.url THEN article_feeds.seeded ELSE 0 END""",
                (source_id, url),
            )
    return ArticleFeedSettings(
        source_id=source_id,
        source_name=source["name"],
        website=source["website"],
        url=url,
    )


def list_article_feeds() -> list[ArticleFeed]:
    with _database() as database:
        rows = database.execute("SELECT source_id, url FROM article_feeds ORDER BY source_id").fetchall()
    return [ArticleFeed(source_id=row["source_id"], url=row["url"]) for row in rows]


def save_discovered_articles(feed: ArticleFeed, articles: list[Article]) -> None:
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        current = database.execute(
            "SELECT seeded FROM article_feeds WHERE source_id = ? AND url = ?", (feed.source_id, feed.url)
        ).fetchone()
        if current is None:
            return
        discovered_at = int(time()) if current["seeded"] else None
        database.executemany(
            """INSERT INTO discovered_articles (source_id, article_url, title, discovered_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (source_id, article_url) DO NOTHING""",
            [(feed.source_id, article.article_url, article.title, discovered_at) for article in articles],
        )
        if not current["seeded"]:
            database.execute("UPDATE article_feeds SET seeded = 1 WHERE source_id = ?", (feed.source_id,))


def list_unqueued_articles(limit: int = 100) -> list[tuple[int, str]]:
    with _database() as database:
        rows = database.execute(
            """SELECT articles.source_id, articles.article_url FROM discovered_articles AS articles
            WHERE articles.discovered_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM discovered_article_analyses AS analyses
                WHERE analyses.source_id = articles.source_id AND analyses.article_url = articles.article_url
            )
            ORDER BY articles.discovered_at, articles.source_id, articles.article_url LIMIT ?""",
            (max(0, limit),),
        ).fetchall()
    return [(row["source_id"], row["article_url"]) for row in rows]


def get_discovered_article(source_id: int, article_url: str) -> Article | None:
    with _database() as database:
        row = database.execute(
            """SELECT title, article_url FROM discovered_articles
            WHERE source_id = ? AND article_url = ? AND discovered_at IS NOT NULL""",
            (source_id, article_url),
        ).fetchone()
    return Article(**dict(row)) if row is not None else None


def mark_article_analysis_queued(source_id: int, article_url: str) -> None:
    with _database() as database:
        database.execute(
            """INSERT INTO discovered_article_analyses (source_id, article_url, status)
            SELECT source_id, article_url, 'queued' FROM discovered_articles
            WHERE source_id = ? AND article_url = ? AND discovered_at IS NOT NULL
            ON CONFLICT (source_id, article_url) DO NOTHING""",
            (source_id, article_url),
        )


def save_article_evidence(source_id: int, article_url: str, publisher_text: str) -> str | None:
    with _database() as database:
        row = database.execute(
            """INSERT INTO article_evidence (source_id, article_url, publisher_text)
            SELECT source_id, article_url, ? FROM discovered_articles
            WHERE source_id = ? AND article_url = ? AND discovered_at IS NOT NULL
            ON CONFLICT (source_id, article_url) DO UPDATE SET publisher_text = article_evidence.publisher_text
            RETURNING publisher_text""",
            (publisher_text, source_id, article_url),
        ).fetchone()
    return row["publisher_text"] if row is not None else None


def save_article_analysis(
    source_id: int, article_url: str, result: ChangeAnalysisResult | None,
    *, slack_channels: dict[DigestTeam, str] | None = None,
) -> None:
    status = "completed" if result is not None else "failed"
    result_json = result.model_dump_json() if result is not None else None
    with _database() as database:
        saved = database.execute(
            """INSERT INTO discovered_article_analyses (source_id, article_url, status, result)
            SELECT source_id, article_url, ?, ? FROM discovered_articles
            WHERE source_id = ? AND article_url = ? AND discovered_at IS NOT NULL
            ON CONFLICT (source_id, article_url) DO UPDATE SET status = excluded.status, result = excluded.result
            WHERE discovered_article_analyses.status != 'completed'""",
            (status, result_json, source_id, article_url),
        )
        if saved.rowcount and result is not None and slack_channels:
            _enqueue_slack_delivery(database, slack_channels, source_id=source_id, article_url=article_url)


def list_unqueued_changes(limit: int = 100) -> list[tuple[int, int]]:
    with _database() as database:
        rows = database.execute(
            """SELECT changes.page_id, changes.version FROM page_changes AS changes
            WHERE changes.before IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM change_analyses AS analyses
                WHERE analyses.page_id = changes.page_id AND analyses.version = changes.version
            )
            ORDER BY changes.version, changes.page_id LIMIT ?""",
            (max(0, limit),),
        ).fetchall()
    return [(row["page_id"], row["version"]) for row in rows]


def get_change_for_analysis(page_id: int, version: int) -> ChangeToAnalyze | None:
    with _database() as database:
        row = database.execute(
            "SELECT before, after FROM page_changes WHERE page_id = ? AND version = ? AND before IS NOT NULL",
            (page_id, version),
        ).fetchone()
    return ChangeToAnalyze(before=row["before"], after=row["after"]) if row is not None else None


def mark_analysis_queued(page_id: int, version: int) -> None:
    with _database() as database:
        database.execute(
            """INSERT INTO change_analyses (page_id, version, status)
            SELECT page_id, version, 'queued' FROM page_changes
            WHERE page_id = ? AND version = ? AND before IS NOT NULL
            ON CONFLICT (page_id, version) DO NOTHING""",
            (page_id, version),
        )


def save_change_analysis(
    page_id: int, version: int, result: ChangeAnalysisResult | None,
    *, slack_channels: dict[DigestTeam, str] | None = None,
) -> None:
    status = "completed" if result is not None else "failed"
    result_json = result.model_dump_json() if result is not None else None
    with _database() as database:
        saved = database.execute(
            """INSERT INTO change_analyses (page_id, version, status, result)
            SELECT page_id, version, ?, ? FROM page_changes
            WHERE page_id = ? AND version = ? AND before IS NOT NULL
            ON CONFLICT (page_id, version) DO UPDATE SET status = excluded.status, result = excluded.result
            WHERE change_analyses.status != 'completed'""",
            (status, result_json, page_id, version),
        )
        if saved.rowcount and result is not None and slack_channels:
            _enqueue_slack_delivery(database, slack_channels, page_id=page_id, version=version)


def list_page_analyses(page_id: int) -> dict[str, PageChangeAnalysis]:
    with _database() as database:
        rows = database.execute(
            "SELECT version, status, result FROM change_analyses WHERE page_id = ?", (page_id,)
        ).fetchall()
    return {
        str(row["version"]): PageChangeAnalysis(
            status=row["status"],
            result=ChangeAnalysisResult.model_validate_json(row["result"]) if row["result"] is not None else None,
        )
        for row in rows
    }


def get_analysis_quality() -> AnalysisQuality:
    with _database() as database:
        return _analysis_quality(database)


def _analysis_quality(database: sqlite3.Connection) -> AnalysisQuality:
    row = database.execute("SELECT quality FROM analysis_settings WHERE id = 1").fetchone()
    return AnalysisQuality(row["quality"]) if row is not None else AnalysisQuality.ECONOMY


def get_team_interests() -> TeamInterests:
    with _database() as database:
        return _team_interests(database)


def _team_interests(database: sqlite3.Connection) -> TeamInterests:
    row = database.execute(
        "SELECT marketing, product, research_and_development, business_context FROM team_interests WHERE id = 1"
    ).fetchone()
    return TeamInterests.model_validate(dict(row)) if row is not None else TeamInterests()


def save_team_interests(interests: TeamInterests) -> None:
    with _database() as database:
        _save_team_interests(database, interests)


def patch_team_interests(changes: TeamInterests) -> TeamInterests:
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        interests = _team_interests(database).model_copy(update=changes.model_dump(exclude_unset=True))
        _save_team_interests(database, interests)
        return interests


def _save_team_interests(database: sqlite3.Connection, interests: TeamInterests) -> None:
    database.execute(
        """INSERT INTO team_interests (id, marketing, product, research_and_development, business_context)
        VALUES (1, :marketing, :product, :research_and_development, :business_context)
        ON CONFLICT (id) DO UPDATE SET marketing = excluded.marketing, product = excluded.product,
            research_and_development = excluded.research_and_development,
            business_context = excluded.business_context""",
        interests.model_dump(),
    )


def save_analysis_quality(quality: AnalysisQuality) -> None:
    with _database() as database:
        database.execute(
            """INSERT INTO analysis_settings (id, quality) VALUES (1, ?)
            ON CONFLICT (id) DO UPDATE SET quality = excluded.quality""",
            (quality,),
        )


def latest_page_snapshot(page_id: int) -> PageSnapshot | None:
    with _database() as database:
        row = database.execute(
            "SELECT version, after FROM page_changes WHERE page_id = ? ORDER BY version DESC LIMIT 1", (page_id,)
        ).fetchone()
    return PageSnapshot(version=row["version"], after=row["after"]) if row is not None else None


def has_history_gap(page_id: int, watch_id: str) -> bool:
    with _database() as database:
        gap = database.execute(
            "SELECT 1 FROM history_gaps WHERE page_id = ? AND watch_id = ?", (page_id, watch_id)
        ).fetchone()
    return gap is not None


def record_history_gap(page_id: int, watch_id: str) -> None:
    with _database() as database:
        database.execute(
            """INSERT INTO history_gaps (page_id, watch_id)
            SELECT id, watch_id FROM pages WHERE id = ? AND watch_id = ? AND state = ?
            ON CONFLICT (page_id, watch_id) DO NOTHING""",
            (page_id, watch_id, PageState.LINKED),
        )


def save_page_version(*, page_id: int, watch_id: str, version: int, before: str | None, after: str) -> bool:
    with _database() as database:
        inserted = database.execute(
            """INSERT INTO page_changes (page_id, version, before, after)
            SELECT id, ?, ?, ? FROM pages WHERE id = ? AND watch_id = ? AND state = ?
            ON CONFLICT (page_id, version) DO NOTHING""",
            (version, before, after, page_id, watch_id, PageState.LINKED),
        ).rowcount
    return bool(inserted)


def list_competitors() -> list[Competitor]:
    with _database() as database:
        database.execute("BEGIN")
        competitors = {
            row["id"]: Competitor(**dict(row), pages=[])
            for row in database.execute("SELECT * FROM competitors ORDER BY id")
        }
        for row in database.execute("SELECT * FROM pages ORDER BY id"):
            page = Page.model_validate(dict(row))
            competitors[page.competitor_id].pages.append(page)
    return list(competitors.values())


def save_competitor(name: str, website: str) -> Competitor:
    with _database() as database:
        competitor = database.execute(
            "INSERT INTO competitors (name, website) VALUES (?, ?) RETURNING *", (name, website)
        ).fetchone()
        page = database.execute(
            "INSERT INTO pages (competitor_id, url) VALUES (?, ?) RETURNING *", (competitor["id"], website)
        ).fetchone()
    return Competitor(**dict(competitor), pages=[Page.model_validate(dict(page))])


def save_page(competitor_id: int, url: str) -> Page:
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        if database.execute("SELECT id FROM competitors WHERE id = ?", (competitor_id,)).fetchone() is None:
            raise LookupError("Competitor not found.")
        try:
            page = database.execute(
                "INSERT INTO pages (competitor_id, url) VALUES (?, ?) RETURNING *", (competitor_id, url)
            ).fetchone()
        except sqlite3.IntegrityError:
            raise ValueError("That page is already configured for this competitor.") from None
    return Page.model_validate(dict(page))


def claim_watch_creation(page: Page) -> str | None:
    with _database() as database:
        claimed = database.execute(
            "UPDATE pages SET state = ?, attempted_at = ?, error = ? WHERE id = ? AND state = ?",
            (
                PageState.CREATING,
                datetime.now(UTC).isoformat(),
                "Creation is not yet confirmed.",
                page.id,
                PageState.PENDING,
            ),
        ).rowcount
        if not claimed:
            return None
        title = database.execute("SELECT name FROM competitors WHERE id = ?", (page.competitor_id,)).fetchone()["name"]
    return title


def delete_page(page: Page) -> bool:
    with _database() as database:
        deleted = database.execute(
            "DELETE FROM pages WHERE id = ? AND state = ? AND watch_id IS ?",
            (page.id, page.state, page.watch_id),
        ).rowcount
        return bool(deleted) or database.execute("SELECT id FROM pages WHERE id = ?", (page.id,)).fetchone() is None


def record_watch_error(*, page_id: int, state: PageState, error: str) -> None:
    with _database() as database:
        database.execute("UPDATE pages SET state = ?, error = ? WHERE id = ?", (state, error, page_id))


def link_watch(page_id: int, watch_id: str) -> None:
    with _database() as database:
        database.execute(
            "UPDATE pages SET watch_id = ?, state = ?, error = NULL WHERE id = ?",
            (watch_id, PageState.LINKED, page_id),
        )


def _migrate_slack_tables(database: sqlite3.Connection) -> None:
    legacy_connection = "SELECT 1 FROM pragma_table_info('slack_connection') WHERE name = 'channel_id'"
    new_deliveries = "SELECT 1 FROM pragma_table_info('slack_deliveries') WHERE name = 'teams'"
    if database.execute(legacy_connection).fetchone() is None and database.execute(new_deliveries).fetchone():
        return
    database.execute("BEGIN IMMEDIATE")
    if database.execute(legacy_connection).fetchone() is not None:
        database.execute(f"CREATE TABLE slack_connection_upgraded {_SLACK_CONNECTION_SCHEMA}")
        team_pairs = ", ".join("?, channel_id" for _ in DigestTeam)
        database.execute(
            f"""INSERT INTO slack_connection_upgraded (id, workspace_name, encrypted_token, team_channels, enabled)
            SELECT id, workspace_name, encrypted_token,
                CASE WHEN channel_id IS NULL THEN '{{}}' ELSE json_object({team_pairs}) END, enabled
            FROM slack_connection""",
            tuple(DigestTeam),
        )
        database.execute("DROP TABLE slack_connection")
        database.execute("ALTER TABLE slack_connection_upgraded RENAME TO slack_connection")
    if database.execute(new_deliveries).fetchone() is None:
        database.execute(f"CREATE TABLE slack_deliveries_upgraded {_SLACK_DELIVERIES_SCHEMA}")
        database.execute(
            """INSERT INTO slack_deliveries_upgraded
            (id, channel_id, page_id, version, source_id, article_url, teams, status, error)
            SELECT id, channel_id, page_id, version, source_id, article_url, ?, status, error FROM slack_deliveries""",
            (json.dumps(list(DigestTeam)),),
        )
        database.execute("DROP TABLE slack_deliveries")
        database.execute("ALTER TABLE slack_deliveries_upgraded RENAME TO slack_deliveries")
    database.commit()


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    path = Path(os.getenv("SIGNALWATCH_DB_PATH", str(Path.home() / ".local/share/signalwatch/configuration.sqlite3")))
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database, database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        database.execute(f"CREATE TABLE IF NOT EXISTS competitors {_COMPETITORS_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS pages {_PAGES_SCHEMA}")
        schema_query = "SELECT sql FROM sqlite_schema WHERE name = 'pages'"
        if "AUTOINCREMENT" not in database.execute(schema_query).fetchone()["sql"].upper():
            database.execute("PRAGMA foreign_keys = OFF")
            database.execute("BEGIN IMMEDIATE")
            # Another connection may have upgraded the table while this one waited for the write lock.
            if "AUTOINCREMENT" not in database.execute(schema_query).fetchone()["sql"].upper():
                database.execute(f"CREATE TABLE pages_upgraded {_PAGES_SCHEMA}")
                database.execute("INSERT INTO pages_upgraded SELECT * FROM pages")
                database.execute("DROP TABLE pages")
                database.execute("ALTER TABLE pages_upgraded RENAME TO pages")
                if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.IntegrityError("Page upgrade failed its foreign-key check.")
            database.commit()
            database.execute("PRAGMA foreign_keys = ON")
        schema_query = "SELECT sql FROM sqlite_schema WHERE name = 'competitors'"
        if "AUTOINCREMENT" not in database.execute(schema_query).fetchone()["sql"].upper():
            database.execute("PRAGMA foreign_keys = OFF")
            database.execute("BEGIN IMMEDIATE")
            if "AUTOINCREMENT" not in database.execute(schema_query).fetchone()["sql"].upper():
                database.execute(f"CREATE TABLE competitors_upgraded {_COMPETITORS_SCHEMA}")
                database.execute("INSERT INTO competitors_upgraded SELECT * FROM competitors")
                database.execute("DROP TABLE competitors")
                database.execute("ALTER TABLE competitors_upgraded RENAME TO competitors")
                if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.IntegrityError("Competitor upgrade failed its foreign-key check.")
            database.commit()
            database.execute("PRAGMA foreign_keys = ON")
        database.execute(f"CREATE TABLE IF NOT EXISTS page_changes {_PAGE_CHANGES_SCHEMA}")
        schema_query = "SELECT type FROM pragma_table_info('page_changes') WHERE name = 'version'"
        if database.execute(schema_query).fetchone()["type"].upper() == "TEXT":
            database.execute("PRAGMA foreign_keys = OFF")
            database.execute("BEGIN IMMEDIATE")
            if database.execute(schema_query).fetchone()["type"].upper() == "TEXT":
                database.execute(f"CREATE TABLE page_changes_upgraded {_PAGE_CHANGES_SCHEMA}")
                database.execute("INSERT INTO page_changes_upgraded SELECT * FROM page_changes")
                database.execute("DROP TABLE page_changes")
                database.execute("ALTER TABLE page_changes_upgraded RENAME TO page_changes")
                if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.IntegrityError("Page history upgrade failed its foreign-key check.")
            database.commit()
            database.execute("PRAGMA foreign_keys = ON")
        database.execute(f"CREATE TABLE IF NOT EXISTS history_gaps {_HISTORY_GAPS_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS change_analyses {_CHANGE_ANALYSES_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS analysis_settings {_ANALYSIS_SETTINGS_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS team_interests {_TEAM_INTERESTS_SCHEMA}")
        schema_query = "SELECT 1 FROM pragma_table_info('team_interests') WHERE name = 'business_context'"
        if database.execute(schema_query).fetchone() is None:
            database.execute("BEGIN IMMEDIATE")
            if database.execute(schema_query).fetchone() is None:
                database.execute("ALTER TABLE team_interests ADD COLUMN business_context TEXT NOT NULL DEFAULT ''")
            database.commit()
        database.execute(f"CREATE TABLE IF NOT EXISTS article_feeds {_ARTICLE_FEEDS_SCHEMA}")
        schema_query = "SELECT 1 FROM pragma_table_info('article_feeds') WHERE name = 'discovery_only'"
        if database.execute(schema_query).fetchone() is not None:
            database.execute("BEGIN IMMEDIATE")
            if database.execute(schema_query).fetchone() is not None:
                database.execute("ALTER TABLE article_feeds DROP COLUMN discovery_only")
            database.commit()
        database.execute(f"CREATE TABLE IF NOT EXISTS discovered_articles {_DISCOVERED_ARTICLES_SCHEMA}")
        database.execute(
            f"CREATE TABLE IF NOT EXISTS discovered_article_analyses {_DISCOVERED_ARTICLE_ANALYSES_SCHEMA}"
        )
        database.execute(f"CREATE TABLE IF NOT EXISTS feedback {_FEEDBACK_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS applied_feedback {_APPLIED_FEEDBACK_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS article_evidence {_ARTICLE_EVIDENCE_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS suggestions {_SUGGESTIONS_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS slack_deliveries {_SLACK_DELIVERIES_SCHEMA}")
        database.execute(f"CREATE TABLE IF NOT EXISTS slack_connection {_SLACK_CONNECTION_SCHEMA}")
        _migrate_slack_tables(database)
        yield database
