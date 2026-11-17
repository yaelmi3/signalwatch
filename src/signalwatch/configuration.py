from ipaddress import ip_address
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import AfterValidator, BaseModel, BeforeValidator, HttpUrl, StringConstraints

from signalwatch import changedetection, storage
from signalwatch.storage import Competitor, Page, PageState

router = APIRouter(prefix="/api", tags=["Configuration"])


def _check_url_text(value: object) -> object:
    if isinstance(value, str) and any(marker in value for marker in ("\\", "{{", "{%")):
        raise ValueError("Use a plain HTTP(S) URL without templates or backslashes.")
    return value


def _check_target(url: HttpUrl) -> HttpUrl:
    if url.username or url.password or not url.host:
        raise ValueError("Use a public HTTP(S) URL without credentials.")
    try:
        address = ip_address(url.host.strip("[]"))
    except ValueError:
        return url
    if not address.is_global or address.is_multicast:
        raise ValueError("Monitored URLs must target public addresses.")
    return url


type MonitoredUrl = Annotated[HttpUrl, BeforeValidator(_check_url_text), AfterValidator(_check_target)]


class CompetitorInput(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    website: MonitoredUrl


class PageInput(BaseModel):
    url: MonitoredUrl


def _page(page_id: int) -> Page:
    page = storage.get_page(page_id)
    if page is None:
        raise HTTPException(404, "Page not found.")
    return page


@router.get("/competitors")
def list_competitors() -> list[Competitor]:
    return storage.list_competitors()


@router.post("/competitors", status_code=201)
def add_competitor(payload: CompetitorInput) -> Competitor:
    competitor = storage.save_competitor(payload.name, str(payload.website))
    competitor.pages = [connect_page(competitor.pages[0].id)]
    return competitor


@router.post("/competitors/{competitor_id}/pages", status_code=201)
def add_page(competitor_id: int, payload: PageInput) -> Page:
    try:
        page = storage.save_page(competitor_id, str(payload.url))
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return connect_page(page.id)


@router.post("/pages/{page_id}/connect")
def connect_page(page_id: int) -> Page:
    page = _page(page_id)
    if page.state == PageState.LINKED:
        return page
    title = storage.claim_watch_creation(page)
    if title is None:
        raise HTTPException(409, "Watch creation is unconfirmed; maintainer attention is required.")
    try:
        watch_id = changedetection.create_watch(page.url, title)
    except changedetection.EngineError as error:
        state = PageState.ATTENTION if error.uncertain else PageState.PENDING
        storage.record_watch_error(page_id=page_id, state=state, error=str(error))
    else:
        storage.link_watch(page_id, watch_id)
    return _page(page_id)


@router.get("/pages/{page_id}/status")
def page_status(page_id: int) -> changedetection.WatchStatus:
    page = _page(page_id)
    if page.watch_id is None:
        return changedetection.WatchStatus(error=page.error or "This page has no linked engine watch.")
    try:
        return changedetection.read_watch(page.watch_id)
    except changedetection.EngineError as error:
        return changedetection.WatchStatus(error=str(error))
