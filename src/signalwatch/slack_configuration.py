import sqlite3
from http import HTTPStatus
from secrets import compare_digest, token_urlsafe

import structlog
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from signalwatch import slack_delivery, slack_oauth, storage
from signalwatch.slack_oauth import SLACK_SETTINGS_PATH, SlackChannel, SlackOAuthConfig, SlackOAuthError

_SESSION_COOKIE = "__Host-signalwatch-slack-session"
_STATE_COOKIE = "__Host-signalwatch-slack-state"
_SESSION_TTL = 900
_STATE_TTL = 300
slack_configuration_logger = structlog.get_logger()
router = APIRouter(prefix=SLACK_SETTINGS_PATH, tags=["Slack settings"])


class SlackStatus(BaseModel):
    available: bool
    authenticated: bool = False
    connected: bool = False
    workspace_name: str | None = None
    channel_id: str | None = None
    enabled: bool = False


class SlackDestinationInput(BaseModel):
    channel_id: str | None
    enabled: bool


class SlackConnectURL(BaseModel):
    url: str


@router.get("")
def get_status(request: Request, response: Response) -> SlackStatus:
    response.headers["Cache-Control"] = "no-store"
    config = slack_oauth.load_config()
    if config is None or _session_id(request, config) is None:
        return SlackStatus(available=config is not None)
    return _status(storage.get_slack_connection())


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
    config = _authorized_config(request, mutation=True)
    state = config.cipher.encrypt(f"oauth:{_session_id(request, config)}".encode()).decode()
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
    config = _authorized_config(request)
    response.headers["Cache-Control"] = "no-store"
    token = _bot_token(config, _require_connection())
    return _member_channels(token)


@router.put("")
def save_destination(payload: SlackDestinationInput, request: Request, response: Response) -> SlackStatus:
    config = _authorized_config(request, mutation=True)
    response.headers["Cache-Control"] = "no-store"
    connection = _require_connection()
    channel_id = payload.channel_id
    if payload.enabled and channel_id is None:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Select a Slack channel before enabling delivery.")
    if channel_id is not None and (channel_id != connection.channel_id or payload.enabled):
        _require_member(_bot_token(config, connection), channel_id)
    updated = storage.save_slack_destination(connection, channel_id, payload.enabled)
    if updated is None:
        raise HTTPException(HTTPStatus.CONFLICT, "Slack was reconnected. Refresh and try again.")
    return _status(updated)


@router.post("/test", status_code=HTTPStatus.NO_CONTENT)
def send_test(request: Request) -> None:
    config = _authorized_config(request, mutation=True)
    connection = _require_connection()
    if connection.channel_id is None:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Save a Slack channel before sending a test.")
    token = _bot_token(config, connection)
    _require_member(token, connection.channel_id)
    try:
        slack_delivery.post_message(
            slack_delivery.SlackSettings(connection.channel_id, token), "SignalWatch test message. Slack is connected."
        )
    except slack_delivery.SlackPostError as error:
        if error.uncertain:
            message = "SignalWatch could not confirm the test delivery. Check the channel before trying again."
        else:
            message = "Slack rejected the test. Check the bot permissions and channel."
        raise HTTPException(HTTPStatus.BAD_GATEWAY, message) from None


def _status(connection: storage.SlackConnection | None) -> SlackStatus:
    return SlackStatus(
        available=True,
        authenticated=True,
        connected=connection is not None,
        workspace_name=connection.workspace_name if connection else None,
        channel_id=connection.channel_id if connection else None,
        enabled=connection.enabled if connection else False,
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


def _authorized_config(request: Request, *, mutation: bool = False) -> SlackOAuthConfig:
    config = _available_config()
    if _session_id(request, config) is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "Sign in to Slack settings first.")
    if mutation:
        _require_origin(request, config)
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


def _member_channels(token: str) -> list[SlackChannel]:
    try:
        return slack_oauth.list_member_channels(token)
    except SlackOAuthError as error:
        raise HTTPException(HTTPStatus.BAD_GATEWAY, str(error)) from None


def _require_member(token: str, channel_id: str) -> None:
    if not any(channel.id == channel_id for channel in _member_channels(token)):
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Choose a channel the Slack bot has joined.")
