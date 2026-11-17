import os
from http import HTTPMethod, HTTPStatus
from urllib.parse import quote
from uuid import UUID

import httpx2
from pydantic import BaseModel, HttpUrl, ValidationError

from signalwatch.network import send_request


class EngineError(Exception):
    def __init__(self, message: str, uncertain: bool = False, *, response: httpx2.Response | None = None) -> None:
        super().__init__(message)
        self.uncertain = uncertain
        self.response = response


class CreatedWatch(BaseModel):
    uuid: UUID


class WatchTarget(BaseModel):
    url: str


class EngineWatch(BaseModel):
    paused: bool
    last_checked: float | None
    check_count: int
    last_error: str | bool


class WatchStatus(BaseModel):
    available: bool = False
    paused: bool | None = None
    last_checked: float | None = None
    check_count: int | None = None
    error: str | None = None


def create_watch(url: str, title: str) -> str:
    data = _request(
        method=HTTPMethod.POST,
        path="watch",
        payload={"url": url, "title": title, "fetch_backend": "html_requests", "notification_muted": True},
    )
    try:
        watch_id = str(CreatedWatch.model_validate(data).uuid)
        data = _request(method=HTTPMethod.GET, path=f"watch/{quote(watch_id, safe='')}")
        target = WatchTarget.model_validate(data)
    except (ValidationError, EngineError):
        raise EngineError("Watch creation could not be confirmed; maintainer attention is required.", True) from None
    if target.url != url:
        raise EngineError("The engine created a watch for a different URL; maintainer attention is required.", True)
    return watch_id


def read_watch(watch_id: str) -> WatchStatus:
    data = _request(method=HTTPMethod.GET, path=f"watch/{quote(watch_id, safe='')}")
    try:
        watch = EngineWatch.model_validate(data)
    except ValidationError:
        raise EngineError("The engine returned an invalid watch status.") from None
    return WatchStatus(
        available=True,
        paused=watch.paused,
        last_checked=watch.last_checked or None,
        check_count=watch.check_count,
        error="Last page fetch failed." if watch.last_error else None,
    )


def delete_watch(watch_id: str) -> None:
    try:
        _request(method=HTTPMethod.DELETE, path=f"watch/{quote(watch_id, safe='')}")
    except EngineError as error:
        if error.response is None or error.response.status_code != HTTPStatus.BAD_REQUEST:
            raise
        try:
            missing = error.response.json() == {"message": f"No watch exists with the UUID of {watch_id}"}
        except ValueError:
            missing = False
        if not missing:
            raise


def _request(*, method: HTTPMethod, path: str, payload: dict[str, str | bool] | None = None) -> object:
    address, key = os.getenv("CHANGEDETECTION_URL"), os.getenv("CHANGEDETECTION_API_KEY")
    if not address or not key:
        raise EngineError("Configure the changedetection URL and API key on the server.")
    if not key.isascii() or not key.isprintable() or key != key.strip():
        raise EngineError("The server's changedetection API key is invalid.")
    try:
        base = HttpUrl(address)
    except ValidationError:
        raise EngineError("The server's changedetection URL is invalid.") from None
    if base.username or base.password or base.query or base.fragment:
        raise EngineError("The server's changedetection connection settings are invalid.")
    try:
        response = send_request(
            method=method, url=f"{str(base).rstrip('/')}/api/v1/{path}", headers={"x-api-key": key}, payload=payload
        )
    except (httpx2.ConnectError, httpx2.ConnectTimeout):
        raise EngineError("The engine connection could not be established; retry when it is available.") from None
    except httpx2.RequestError:
        raise EngineError(
            "The engine could not be reached; check its state before retrying.", method == HTTPMethod.POST
        ) from None
    except httpx2.HTTPStatusError as error:
        code = error.response.status_code
        raise EngineError(
            f"The engine rejected the request (HTTP {code}).",
            method == HTTPMethod.POST and not HTTPStatus.BAD_REQUEST <= code < HTTPStatus.INTERNAL_SERVER_ERROR,
            response=error.response,
        ) from None
    if method == HTTPMethod.DELETE:
        if response.status_code != HTTPStatus.NO_CONTENT:
            raise EngineError("The engine returned an invalid deletion response.")
        return None
    try:
        return response.json()
    except ValueError:
        raise EngineError("The engine returned an invalid response.", method == HTTPMethod.POST) from None
