import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import time
from typing import Literal

from pydantic import BaseModel, Field

from signalwatch.analysis_models import AnalysisQuality, ChangeAnalysisResult, PageChangeAnalysis, TeamInterests

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
    url TEXT NOT NULL, discovery_only INTEGER NOT NULL CHECK (discovery_only IN (0, 1)),
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
_DISCOVERY_ONLY_PAGE_SQL = """EXISTS (
    SELECT 1 FROM article_feeds JOIN competitors ON competitors.id = article_feeds.source_id
    WHERE article_feeds.source_id = pages.competitor_id AND article_feeds.discovery_only
        AND competitors.website = pages.url
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
    hold_reason: Literal["discovery_only"] | None = Field(default=None, exclude_if=lambda reason: reason is None)


class DigestArticle(_DigestAnalysis):
    kind: Literal["article"] = "article"
    source_id: int
    source_name: str
    title: str
    article_url: str
    hold_reason: Literal["business_fit_unverified"] | None = Field(
        default=None, exclude_if=lambda reason: reason is None
    )


@dataclass(frozen=True)
class Article:
    title: str
    article_url: str


@dataclass(frozen=True)
class ArticleFeed:
    source_id: int
    url: str


class ArticleFeedSettings(BaseModel):
    source_id: int
    source_name: str
    website: str
    url: str | None
    discovery_only: bool


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


def is_discovery_only_page(page_id: int) -> bool:
    with _database() as database:
        row = database.execute(
            f"SELECT 1 FROM pages WHERE id = ? AND {_DISCOVERY_ONLY_PAGE_SQL}", (page_id,)
        ).fetchone()
    return row is not None


def list_digest(view: DigestView = DigestView.RELEVANT) -> list[DigestEntry | DigestArticle]:
    with _database() as database:
        rows = database.execute(
            f"""WITH items AS (
                SELECT changes.page_id, changes.version, competitors.id AS source_id, competitors.name AS source_name,
                       pages.url AS page_url,
                       NULL AS title, NULL AS article_url, analyses.status, analyses.result,
                       CASE WHEN {_DISCOVERY_ONLY_PAGE_SQL} THEN 'discovery_only' END AS hold_reason,
                       1 AS business_fit
                FROM page_changes AS changes
                JOIN pages ON pages.id = changes.page_id
                JOIN competitors ON competitors.id = pages.competitor_id
                LEFT JOIN change_analyses AS analyses
                    ON analyses.page_id = changes.page_id AND analyses.version = changes.version
                WHERE changes.before IS NOT NULL AND pages.state = ? AND pages.watch_id IS NOT NULL
                UNION ALL
                SELECT NULL, articles.discovered_at, articles.source_id, competitors.name, NULL, articles.title,
                       articles.article_url, analyses.status, analyses.result, NULL,
                       COALESCE(json_type(analyses.result, '$.change_quote') = 'text'
                           AND TRIM(json_extract(analyses.result, '$.change_quote')) <> ''
                           AND EXISTS (SELECT 1 FROM team_interests WHERE TRIM(business_context) <> ''), 0)
                FROM discovered_articles AS articles
                JOIN competitors ON competitors.id = articles.source_id
                LEFT JOIN discovered_article_analyses AS analyses USING (source_id, article_url)
                WHERE articles.discovered_at IS NOT NULL
            )
            SELECT * FROM items
            WHERE COALESCE(hold_reason IS NULL AND business_fit AND status = 'completed' AND (
                json_extract(result, '$.marketing.relevant') OR json_extract(result, '$.product.relevant')
                OR json_extract(result, '$.research_and_development.relevant')
            ), 0) = ?
            ORDER BY version DESC, article_url IS NOT NULL, page_id, source_id, article_url LIMIT ?""",
            (PageState.LINKED, view == DigestView.RELEVANT, _DIGEST_LIMIT),
        ).fetchall()
    entries: list[DigestEntry | DigestArticle] = []
    for row in rows:
        is_article = row["article_url"] is not None
        analysis = (
            ChangeAnalysisResult.model_validate_json(row["result"])
            if row["status"] == "completed" and row["result"] is not None
            else None
        )
        hold_reason = row["hold_reason"]
        if is_article and analysis is not None and not row["business_fit"]:
            hold_reason = "business_fit_unverified"
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
        entries.append(model.model_validate(item))
    return entries


def list_article_feed_settings() -> list[ArticleFeedSettings]:
    with _database() as database:
        rows = database.execute(
            """SELECT competitors.id AS source_id, competitors.name AS source_name, competitors.website,
                      article_feeds.url, COALESCE(article_feeds.discovery_only, 0) AS discovery_only
            FROM competitors LEFT JOIN article_feeds ON competitors.id = article_feeds.source_id
            ORDER BY competitors.id"""
        ).fetchall()
    return [ArticleFeedSettings.model_validate(dict(row)) for row in rows]


def set_article_feed(*, source_id: int, url: str | None, discovery_only: bool) -> ArticleFeedSettings | None:
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        source = database.execute("SELECT name, website FROM competitors WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            return None
        if url is None:
            database.execute("DELETE FROM article_feeds WHERE source_id = ?", (source_id,))
            discovery_only = False
        else:
            database.execute(
                """INSERT INTO article_feeds (source_id, url, discovery_only) VALUES (?, ?, ?)
                ON CONFLICT (source_id) DO UPDATE SET url = excluded.url, discovery_only = excluded.discovery_only,
                    seeded = CASE WHEN article_feeds.url = excluded.url THEN article_feeds.seeded ELSE 0 END""",
                (source_id, url, discovery_only),
            )
    return ArticleFeedSettings(
        source_id=source_id,
        source_name=source["name"],
        website=source["website"],
        url=url,
        discovery_only=discovery_only,
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


def save_article_analysis(source_id: int, article_url: str, result: ChangeAnalysisResult | None) -> None:
    status = "completed" if result is not None else "failed"
    result_json = result.model_dump_json() if result is not None else None
    with _database() as database:
        database.execute(
            """INSERT INTO discovered_article_analyses (source_id, article_url, status, result)
            SELECT source_id, article_url, ?, ? FROM discovered_articles
            WHERE source_id = ? AND article_url = ? AND discovered_at IS NOT NULL
            ON CONFLICT (source_id, article_url) DO UPDATE SET status = excluded.status, result = excluded.result
            WHERE discovered_article_analyses.status != 'completed'""",
            (status, result_json, source_id, article_url),
        )


def list_unqueued_changes(limit: int = 100) -> list[tuple[int, int]]:
    with _database() as database:
        rows = database.execute(
            f"""SELECT changes.page_id, changes.version FROM page_changes AS changes
            JOIN pages ON pages.id = changes.page_id
            WHERE changes.before IS NOT NULL AND NOT {_DISCOVERY_ONLY_PAGE_SQL} AND NOT EXISTS (
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
            f"""SELECT changes.before, changes.after FROM page_changes AS changes
            JOIN pages ON pages.id = changes.page_id
            WHERE changes.page_id = ? AND changes.version = ? AND changes.before IS NOT NULL
                AND NOT {_DISCOVERY_ONLY_PAGE_SQL}""",
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


def save_change_analysis(page_id: int, version: int, result: ChangeAnalysisResult | None) -> None:
    status = "completed" if result is not None else "failed"
    result_json = result.model_dump_json() if result is not None else None
    with _database() as database:
        database.execute(
            """INSERT INTO change_analyses (page_id, version, status, result)
            SELECT page_id, version, ?, ? FROM page_changes
            WHERE page_id = ? AND version = ? AND before IS NOT NULL
            ON CONFLICT (page_id, version) DO UPDATE SET status = excluded.status, result = excluded.result
            WHERE change_analyses.status != 'completed'""",
            (status, result_json, page_id, version),
        )


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
        row = database.execute("SELECT quality FROM analysis_settings WHERE id = 1").fetchone()
    return AnalysisQuality(row["quality"]) if row is not None else AnalysisQuality.ECONOMY


def get_team_interests() -> TeamInterests:
    with _database() as database:
        row = database.execute(
            "SELECT marketing, product, research_and_development, business_context FROM team_interests WHERE id = 1"
        ).fetchone()
    return TeamInterests.model_validate(dict(row)) if row is not None else TeamInterests()


def save_team_interests(interests: TeamInterests) -> None:
    with _database() as database:
        database.execute(
            """INSERT INTO team_interests (id, marketing, product, research_and_development, business_context)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET marketing = excluded.marketing, product = excluded.product,
                research_and_development = excluded.research_and_development,
                business_context = excluded.business_context""",
            (interests.marketing, interests.product, interests.research_and_development, interests.business_context),
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
        database.execute(f"CREATE TABLE IF NOT EXISTS discovered_articles {_DISCOVERED_ARTICLES_SCHEMA}")
        database.execute(
            f"CREATE TABLE IF NOT EXISTS discovered_article_analyses {_DISCOVERED_ARTICLE_ANALYSES_SCHEMA}"
        )
        yield database
