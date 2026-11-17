import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


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


class Competitor(BaseModel):
    id: int
    name: str
    website: str
    pages: list[Page]


def get_page(page_id: int) -> Page | None:
    with _database() as database:
        row = database.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    return Page.model_validate(dict(row)) if row is not None else None


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
        title = database.execute("SELECT name FROM competitors WHERE id = ?", (page.competitor_id,)).fetchone()["name"]
    return title if claimed else None


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
        database.executescript("""
            CREATE TABLE IF NOT EXISTS competitors (id INTEGER PRIMARY KEY, name TEXT NOT NULL, website TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY, competitor_id INTEGER NOT NULL REFERENCES competitors(id), url TEXT NOT NULL,
                watch_id TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT, attempted_at TEXT,
                UNIQUE (competitor_id, url)
            );
        """)
        yield database
