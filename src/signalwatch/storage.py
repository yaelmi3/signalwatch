import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

_COMPETITORS_SCHEMA = "(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, website TEXT NOT NULL)"
_PAGES_SCHEMA = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT, competitor_id INTEGER NOT NULL REFERENCES competitors(id), url TEXT NOT NULL,
    watch_id TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT, attempted_at TEXT,
    UNIQUE (competitor_id, url)
)"""
_PAGE_CHANGES_SCHEMA = """(
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    version TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL,
    PRIMARY KEY (page_id, version)
)"""


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
            "SELECT version, before, after FROM page_changes WHERE page_id = ? ORDER BY CAST(version AS INTEGER) DESC",
            (page_id,),
        ).fetchall()
    return [PageChange.model_validate(dict(row)) for row in rows]


def latest_page_change(page_id: int) -> PageChange | None:
    with _database() as database:
        row = database.execute(
            """SELECT version, before, after FROM page_changes WHERE page_id = ?
            ORDER BY CAST(version AS INTEGER) DESC LIMIT 1""",
            (page_id,),
        ).fetchone()
    return PageChange.model_validate(dict(row)) if row is not None else None


def save_page_change(*, page_id: int, watch_id: str, change: PageChange) -> None:
    with _database() as database:
        database.execute(
            """INSERT INTO page_changes (page_id, version, before, after)
            SELECT id, ?, ?, ? FROM pages WHERE id = ? AND watch_id = ? AND state = ?
            ON CONFLICT (page_id, version) DO NOTHING""",
            (change.version, change.before, change.after, page_id, watch_id, PageState.LINKED),
        )


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
            database.execute("BEGIN IMMEDIATE")
            # Another connection may have upgraded the table while this one waited for the write lock.
            if "AUTOINCREMENT" not in database.execute(schema_query).fetchone()["sql"].upper():
                database.execute(f"CREATE TABLE pages_upgraded {_PAGES_SCHEMA}")
                database.execute("INSERT INTO pages_upgraded SELECT * FROM pages")
                database.execute("DROP TABLE pages")
                database.execute("ALTER TABLE pages_upgraded RENAME TO pages")
            database.commit()
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
        yield database
