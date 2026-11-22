import os
from http import HTTPStatus
from ipaddress import ip_address
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import AfterValidator, BaseModel, BeforeValidator, HttpUrl, StringConstraints

from signalwatch import article_content, changedetection, storage
from signalwatch.analysis_models import AnalysisSettings, PageChangeAnalysis, TeamInterests
from signalwatch.storage import ArticleFeedSettings, Competitor, DigestArticle, DigestEntry, DigestView, Page, PageState

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


def _check_article_feed_url(url: HttpUrl) -> HttpUrl:
    if article_content.public_http_url(str(url)) is None:
        raise ValueError("Use a public HTTP(S) feed URL on the default port.")
    return url


class CompetitorInput(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    website: MonitoredUrl


class PageInput(BaseModel):
    url: MonitoredUrl


class ArticleFeedInput(BaseModel):
    url: Annotated[MonitoredUrl, AfterValidator(_check_article_feed_url)] | None = None


def _page(page_id: int) -> Page:
    page = storage.get_page(page_id)
    if page is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Page not found.")
    return page


def _feedback_origin() -> str:
    return os.getenv("SIGNALWATCH_FEEDBACK_ORIGIN", "").strip()


def _require_feedback_enabled() -> str:
    if origin := _feedback_origin():
        return origin
    raise HTTPException(HTTPStatus.NOT_FOUND, "Feedback is unavailable.")


def _require_feedback_origin(request: Request) -> None:
    if request.headers.get("origin") != _require_feedback_enabled():
        raise HTTPException(HTTPStatus.FORBIDDEN, "Use the SignalWatch site to submit feedback.")


@router.get("/competitors")
def list_competitors() -> list[Competitor]:
    return storage.list_competitors()


@router.get("/monitoring-health")
def monitoring_health() -> Response:
    status = HTTPStatus.SERVICE_UNAVAILABLE if storage.list_stale_sources() else HTTPStatus.OK
    return Response(status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/digest")
def digest(response: Response, view: DigestView = DigestView.RELEVANT) -> list[DigestEntry | DigestArticle]:
    if _feedback_origin():
        response.headers["X-Feedback-Enabled"] = "true"
    return storage.list_digest(view)


@router.get("/feedback", response_model_exclude_none=True, dependencies=[Depends(_require_feedback_enabled)])
def list_feedback(response: Response) -> list[storage.Feedback]:
    response.headers["Cache-Control"] = "no-store"
    return storage.list_feedback()


@router.get("/feedback/items/{feedback_token}", dependencies=[Depends(_require_feedback_enabled)])
def get_feedback_item(
    feedback_token: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")], response: Response
) -> DigestEntry | DigestArticle:
    item = storage.get_feedback_item(feedback_token)
    if item is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "The saved alert is no longer available.")
    response.headers["Cache-Control"] = "no-store"
    return item


@router.post("/feedback", status_code=HTTPStatus.NO_CONTENT, dependencies=[Depends(_require_feedback_origin)])
def submit_feedback(payload: storage.FeedbackInput) -> Response:
    try:
        storage.save_feedback(payload)
    except LookupError as error:
        raise HTTPException(HTTPStatus.NOT_FOUND, str(error)) from None
    except ValueError as error:
        raise HTTPException(HTTPStatus.CONFLICT, str(error)) from None
    return Response(status_code=HTTPStatus.NO_CONTENT)


class SuggestionRequest(BaseModel):
    team: storage.DigestTeam


@router.get("/feedback/suggestion", dependencies=[Depends(_require_feedback_enabled)])
def get_suggestion(team: storage.DigestTeam, response: Response) -> storage.Suggestion | None:
    response.headers["Cache-Control"] = "no-store"
    return storage.get_suggestion(team)


@router.post("/feedback/suggestion", dependencies=[Depends(_require_feedback_origin)])
def start_suggestion(payload: SuggestionRequest) -> storage.Suggestion:
    try:
        return storage.start_suggestion(payload.team)
    except ValueError as error:
        raise HTTPException(HTTPStatus.CONFLICT, str(error)) from None


@router.post("/feedback/suggestion/{suggestion_id}/apply", dependencies=[Depends(_require_feedback_origin)])
def apply_suggestion(suggestion_id: str) -> storage.Suggestion:
    try:
        return storage.apply_suggestion(suggestion_id)
    except LookupError as error:
        raise HTTPException(HTTPStatus.NOT_FOUND, str(error)) from None
    except ValueError as error:
        raise HTTPException(HTTPStatus.CONFLICT, str(error)) from None


@router.delete(
    "/feedback/suggestion/{suggestion_id}",
    status_code=HTTPStatus.NO_CONTENT,
    dependencies=[Depends(_require_feedback_origin)],
)
def dismiss_suggestion(suggestion_id: str) -> Response:
    storage.dismiss_suggestion(suggestion_id)
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.get("/team-interests")
def get_team_interests() -> TeamInterests:
    return storage.get_team_interests()


@router.put("/team-interests")
def set_team_interests(interests: TeamInterests) -> TeamInterests:
    storage.save_team_interests(interests)
    return interests


@router.patch("/team-interests")
def patch_team_interests(changes: TeamInterests) -> TeamInterests:
    return storage.patch_team_interests(changes)


@router.post("/competitors", status_code=HTTPStatus.CREATED)
def add_competitor(payload: CompetitorInput) -> Competitor:
    competitor = storage.save_competitor(payload.name, str(payload.website))
    competitor.pages = [connect_page(competitor.pages[0].id)]
    return competitor


@router.delete("/competitors/{competitor_id}", status_code=HTTPStatus.NO_CONTENT)
def delete_competitor(competitor_id: int) -> Response:
    snapshot = storage.competitor_snapshot(competitor_id)
    if snapshot is not None:
        if any(page["state"] in (PageState.CREATING, PageState.ATTENTION) for page in snapshot.pages):
            raise HTTPException(HTTPStatus.CONFLICT, "Watch creation is unconfirmed; maintainer attention is required.")
        try:
            for page in snapshot.pages:
                if page["watch_id"] is not None:
                    changedetection.delete_watch(page["watch_id"])
        except changedetection.EngineError as error:
            raise HTTPException(HTTPStatus.BAD_GATEWAY, str(error)) from None
        if not storage.delete_competitor(snapshot):
            raise HTTPException(HTTPStatus.CONFLICT, "The competitor or its pages changed; refresh before retrying.")
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.post("/competitors/{competitor_id}/pages", status_code=HTTPStatus.CREATED)
def add_page(competitor_id: int, payload: PageInput) -> Page:
    try:
        page = storage.save_page(competitor_id, str(payload.url))
    except LookupError as error:
        raise HTTPException(HTTPStatus.NOT_FOUND, str(error)) from None
    except ValueError as error:
        raise HTTPException(HTTPStatus.CONFLICT, str(error)) from None
    return connect_page(page.id)


@router.post("/pages/{page_id}/connect")
def connect_page(page_id: int) -> Page:
    page = _page(page_id)
    if page.state == PageState.LINKED:
        return page
    title = storage.claim_watch_creation(page)
    if title is None:
        raise HTTPException(HTTPStatus.CONFLICT, "Watch creation is unconfirmed; maintainer attention is required.")
    try:
        watch_id = changedetection.create_watch(page.url, title)
    except changedetection.EngineError as error:
        state = PageState.ATTENTION if error.uncertain else PageState.PENDING
        storage.record_watch_error(page_id=page_id, state=state, error=str(error))
    else:
        storage.link_watch(page_id, watch_id)
    return _page(page_id)


@router.delete("/pages/{page_id}", status_code=HTTPStatus.NO_CONTENT)
def delete_page(page_id: int) -> Response:
    page = storage.get_page(page_id)
    if page is not None:
        if page.state in (PageState.CREATING, PageState.ATTENTION):
            raise HTTPException(HTTPStatus.CONFLICT, "Watch creation is unconfirmed; maintainer attention is required.")
        if page.watch_id is not None:
            try:
                changedetection.delete_watch(page.watch_id)
            except changedetection.EngineError as error:
                raise HTTPException(HTTPStatus.BAD_GATEWAY, str(error)) from None
        if not storage.delete_page(page):
            raise HTTPException(HTTPStatus.CONFLICT, "The page changed during deletion; refresh before retrying.")
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.get("/pages/{page_id}/status")
def page_status(page_id: int) -> changedetection.WatchStatus:
    page = _page(page_id)
    if page.watch_id is None:
        return changedetection.WatchStatus(error=page.error or "This page has no linked engine watch.")
    try:
        return changedetection.read_watch(page.watch_id)
    except changedetection.EngineError as error:
        return changedetection.WatchStatus(error=str(error))


@router.get("/pages/{page_id}/changes")
def page_changes(page_id: int, response: Response) -> list[storage.PageChange]:
    page = _page(page_id)
    changes = storage.list_page_changes(page_id)
    if page.watch_id is not None and storage.has_history_gap(page_id, page.watch_id):
        response.headers["X-SignalWatch-History-Gap"] = "true"
    return changes


@router.get("/pages/{page_id}/analysis")
def page_analyses(page_id: int) -> dict[str, PageChangeAnalysis]:
    _page(page_id)
    return storage.list_page_analyses(page_id)


@router.get("/maintainer/analysis")
def get_analysis_settings() -> AnalysisSettings:
    return AnalysisSettings(quality=storage.get_analysis_quality())


@router.put("/maintainer/analysis")
def set_analysis_settings(settings: AnalysisSettings) -> AnalysisSettings:
    storage.save_analysis_quality(settings.quality)
    return settings


@router.get("/maintainer/article-feeds")
def list_article_feeds() -> list[ArticleFeedSettings]:
    return storage.list_article_feed_settings()


@router.get("/maintainer/monitoring")
def list_stale_sources(response: Response) -> list[storage.StaleSource]:
    response.headers["Cache-Control"] = "no-store"
    return storage.list_stale_sources()


@router.put("/maintainer/article-feeds/{source_id}")
def set_article_feed(source_id: int, payload: ArticleFeedInput) -> ArticleFeedSettings:
    settings = storage.set_article_feed(
        source_id=source_id,
        url=str(payload.url) if payload.url is not None else None,
    )
    if settings is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Source not found.")
    return settings
