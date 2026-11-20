import sqlite3
from http import HTTPStatus
from secrets import compare_digest, token_urlsafe
from typing import Annotated

import structlog
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, StringConstraints

from signalwatch import slack_delivery, slack_oauth, storage
from signalwatch.slack_oauth import SLACK_SETTINGS_PATH, SlackChannel, SlackOAuthConfig, SlackOAuthError

_SESSION_COOKIE = "__Host-signalwatch-slack-session"
_STATE_COOKIE = "__Host-signalwatch-slack-state"
_SESSION_TTL = 900
_STATE_TTL = 300
_SETTINGS_CHANGED = "Slack settings changed. Review the current channels and try again."
slack_configuration_logger = structlog.get_logger()
router = APIRouter(prefix=SLACK_SETTINGS_PATH, tags=["Slack settings"])


class SlackStatus(BaseModel):
    available: bool
    authenticated: bool = False
    connected: bool = False
    workspace_name: str | None = None
    team_channels: dict[storage.DigestTeam, str] = Field(default_factory=dict)
    enabled: bool = False
    revision: str | None = None


class SlackDestinationInput(BaseModel):
    team_channels: dict[storage.DigestTeam, Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]]
    enabled: bool
    revision: str


class SlackTestInput(BaseModel):
    team: storage.DigestTeam


class SlackConnectURL(BaseModel):
    url: str


@router.get("")
def get_status(request: Request, response: Response) -> SlackStatus:
    response.headers["Cache-Control"] = "no-store"
    config = slack_oauth.load_config()
    if config is None:
        return SlackStatus(available=False)
    return _status(storage.get_slack_connection(), authenticated=_session_id(request, config) is not None)


@router.post("/session", status_code=HTTPStatus.NO_CONTENT)
async def start_session(request: Request, response: Response) -> None:
    config = _available_config()
    _require_origin(request, config)
    try:
        payload = await request.json()
        password = payload.get("password") if isinstance(payload, dict) else None
    except ValueError:
        password = None
    if not isinstance(password, str) or not compare_digest(
        password.encode(errors="surrogatepass"), config.password.encode()
    ):
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "Invalid maintainer passphrase.")
    session = config.cipher.encrypt(f"session:{token_urlsafe(32)}".encode()).decode()
    _set_cookie(response, _SESSION_COOKIE, session, _SESSION_TTL)
    response.headers["Cache-Control"] = "no-store"


@router.post("/connect")
def connect(request: Request, response: Response) -> SlackConnectURL:
    config = _available_config()
    session_id = _session_id(request, config)
    if session_id is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "Sign in to Slack settings first.")
    _require_origin(request, config)
    state = config.cipher.encrypt(f"oauth:{session_id}".encode()).decode()
    _set_cookie(response, _STATE_COOKIE, state, _STATE_TTL)
    response.headers["Cache-Control"] = "no-store"
    return SlackConnectURL(url=slack_oauth.authorization_url(config, state))


@router.get("/callback")
@router.get("/callback/", include_in_schema=False)
def callback(request: Request) -> RedirectResponse:
    params = request.query_params
    # Uvicorn reads the query string from the ASGI scope when it writes its access log.
    request.scope["query_string"] = b""
    config = slack_oauth.load_config()
    session_id = _session_id(request, config) if config is not None else None
    state = params.get("state", "")
    code = params.get("code")
    expected = request.cookies.get(_STATE_COOKIE, "")
    valid_state = bool(state) and compare_digest(state.encode(), expected.encode())
    result = "error"
    if (
        config is not None
        and session_id is not None
        and code
        and not params.get("error")
        and valid_state
        and _decrypt(config, state, _STATE_TTL) == f"oauth:{session_id}"
    ):
        try:
            slack_oauth.install_connection(config, code)
        except (SlackOAuthError, sqlite3.Error, OSError) as error:
            slack_configuration_logger.warning("Slack OAuth installation failed", error_type=type(error).__name__)
        else:
            result = "connected"
    response = RedirectResponse(
        f"/?view=maintainer&slack={result}",
        status_code=HTTPStatus.SEE_OTHER,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
    if valid_state:
        response.delete_cookie(_STATE_COOKIE, secure=True, httponly=True, samesite="lax")
    return response


@router.get("/channels")
def channels(request: Request, response: Response) -> list[SlackChannel]:
    config = _available_config()
    response.headers["Cache-Control"] = "no-store"
    token = _bot_token(config, _require_connection())
    return _member_channels(token, public_only=_session_id(request, config) is None)


@router.put("")
def save_destination(payload: SlackDestinationInput, request: Request, response: Response) -> SlackStatus:
    config = _available_config()
    _require_origin(request, config)
    response.headers["Cache-Control"] = "no-store"
    authenticated = _session_id(request, config) is not None
    connection = _require_connection()
    if payload.revision != connection.revision:
        raise HTTPException(HTTPStatus.CONFLICT, _SETTINGS_CHANGED)
    team_channels = payload.team_channels
    if payload.enabled and not team_channels:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Select a Slack channel before enabling delivery.")
    if channel_ids := {
        channel_id for team, channel_id in team_channels.items()
        if payload.enabled or channel_id != connection.team_channels.get(team)
    }:
        _require_members(_bot_token(config, connection), channel_ids, public_only=not authenticated)
    updated = storage.save_slack_destination(connection, team_channels, payload.enabled)
    if updated is None:
        raise HTTPException(HTTPStatus.CONFLICT, _SETTINGS_CHANGED)
    return _status(updated, authenticated=authenticated)


@router.post("/test", status_code=HTTPStatus.NO_CONTENT)
def send_test(payload: SlackTestInput, request: Request) -> None:
    config = _available_config()
    _require_origin(request, config)
    connection = _require_connection()
    channel_id = connection.team_channels.get(payload.team)
    if channel_id is None:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Save a Slack channel for this team before sending a test.")
    token = _bot_token(config, connection)
    _require_members(token, {channel_id}, public_only=_session_id(request, config) is None)
    try:
        slack_delivery.post_message(channel_id, token, "SignalWatch test message. Slack is connected.")
    except slack_delivery.SlackPostError as error:
        if error.uncertain:
            message = "SignalWatch could not confirm the test delivery. Check the channel before trying again."
        else:
            message = "Slack rejected the test. Check the bot permissions and channel."
        raise HTTPException(HTTPStatus.BAD_GATEWAY, message) from None


def _status(connection: storage.SlackConnection | None, *, authenticated: bool) -> SlackStatus:
    return SlackStatus(
        available=True,
        authenticated=authenticated,
        connected=connection is not None,
        workspace_name=connection.workspace_name if connection else None,
        team_channels=connection.team_channels if connection else {},
        enabled=connection.enabled if connection else False,
        revision=connection.revision if connection else None,
    )


def _set_cookie(response: Response, name: str, value: str, ttl: int) -> None:
    response.set_cookie(name, value, max_age=ttl, secure=True, httponly=True, samesite="lax")


def _decrypt(config: SlackOAuthConfig, token: str, ttl: int) -> str | None:
    try:
        return config.cipher.decrypt(token, ttl=ttl).decode()
    except (InvalidToken, ValueError):
        return None


def _session_id(request: Request, config: SlackOAuthConfig) -> str | None:
    session = _decrypt(config, request.cookies.get(_SESSION_COOKIE, ""), _SESSION_TTL)
    return session.removeprefix("session:") if session and session.startswith("session:") else None


def _require_origin(request: Request, config: SlackOAuthConfig) -> None:
    if request.headers.get("origin") != config.origin:
        raise HTTPException(HTTPStatus.FORBIDDEN, "Use the SignalWatch site to change Slack settings.")


def _available_config() -> SlackOAuthConfig:
    if (config := slack_oauth.load_config()) is None:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, "Slack setup is unavailable.")
    return config


def _require_connection() -> storage.SlackConnection:
    if (connection := storage.get_slack_connection()) is None:
        raise HTTPException(HTTPStatus.CONFLICT, "Connect Slack before choosing a channel.")
    return connection


def _bot_token(config: SlackOAuthConfig, connection: storage.SlackConnection) -> str:
    try:
        return slack_oauth.decrypt_bot_token(config, connection.encrypted_token)
    except SlackOAuthError as error:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, str(error)) from None


def _member_channels(token: str, *, public_only: bool) -> list[SlackChannel]:
    try:
        return slack_oauth.list_member_channels(token, public_only=public_only)
    except SlackOAuthError as error:
        raise HTTPException(HTTPStatus.BAD_GATEWAY, str(error)) from None


def _require_members(token: str, channel_ids: set[str], *, public_only: bool) -> None:
    if not channel_ids.issubset(channel.id for channel in _member_channels(token, public_only=public_only)):
        message = "Choose public channels the Slack bot has joined." if public_only else (
            "Choose channels the Slack bot has joined."
        )
        raise HTTPException(HTTPStatus.BAD_REQUEST, message)
