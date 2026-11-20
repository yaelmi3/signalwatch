import os
from base64 import b64encode
from dataclasses import dataclass, field
from http import HTTPMethod
from urllib.parse import urlencode, urlsplit

import httpx2
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ValidationError

from signalwatch import network, storage

SLACK_SETTINGS_PATH = "/api/maintainer/slack"
_CALLBACK_PATH = f"{SLACK_SETTINGS_PATH}/callback"
_OAUTH_ENV = (
    "SIGNALWATCH_SLACK_CLIENT_ID",
    "SIGNALWATCH_SLACK_CLIENT_SECRET",
    "SIGNALWATCH_SLACK_REDIRECT_URL",
    "SIGNALWATCH_SLACK_MAINTAINER_PASSWORD",
    "SIGNALWATCH_SLACK_ENCRYPTION_KEY",
)
_API_URL = "https://slack.com/api"


@dataclass(frozen=True)
class SlackOAuthConfig:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_url: str
    origin: str
    password: str = field(repr=False)
    cipher: Fernet = field(repr=False)


class SlackOAuthError(Exception):
    """Slack setup failed with a message safe for the operator."""


class SlackChannel(BaseModel):
    id: str
    name: str


class _ChannelEntry(SlackChannel):
    is_archived: bool = False
    is_member: bool = True
    is_private: bool | None = None


class _ChannelCursor(BaseModel):
    next_cursor: str = ""


class _ChannelPage(BaseModel):
    channels: list[_ChannelEntry]
    response_metadata: _ChannelCursor = _ChannelCursor()


def oauth_requested() -> bool:
    return any(os.getenv(name) for name in _OAUTH_ENV)


def load_config() -> SlackOAuthConfig | None:
    client_id, client_secret, redirect_url, password, encryption_key = (os.getenv(name, "") for name in _OAUTH_ENV)
    if not all(value.strip() for value in (client_id, client_secret, redirect_url, password, encryption_key)):
        return None
    try:
        url = urlsplit(redirect_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path != _CALLBACK_PATH
            or url.query
            or url.fragment
            or not redirect_url.isascii()
            or any(character.isspace() for character in redirect_url)
        ):
            return None
        port = url.port
        origin = f"https://{url.netloc.lower().removesuffix(':443') if port == 443 else url.netloc.lower()}"
        return SlackOAuthConfig(client_id, client_secret, redirect_url, origin, password, Fernet(encryption_key))
    except ValueError:
        return None


def authorization_url(config: SlackOAuthConfig, state: str) -> str:
    query = urlencode(
        {
            "client_id": config.client_id,
            "scope": "chat:write,channels:read,groups:read",
            "redirect_uri": config.redirect_url,
            "state": state,
        }
    )
    return f"https://slack.com/oauth/v2/authorize?{query}"


def install_connection(config: SlackOAuthConfig, code: str) -> None:
    credentials = b64encode(f"{config.client_id}:{config.client_secret}".encode()).decode()
    data = _slack_json(
        HTTPMethod.POST,
        "oauth.v2.access",
        f"Basic {credentials}",
        form={"code": code, "redirect_uri": config.redirect_url},
    )
    team = data.get("team")
    workspace = team.get("name") if isinstance(team, dict) else None
    token = data.get("access_token")
    if (
        data.get("is_enterprise_install") is True
        or data.get("token_type") != "bot"
        or data.get("expires_in") is not None
        or not isinstance(workspace, str)
        or not workspace.strip()
        or not isinstance(token, str)
        or not token
        or not token.isascii()
        or not token.isprintable()
        or " " in token
    ):
        raise SlackOAuthError("Slack did not return a workspace bot installation.")
    storage.save_slack_connection(workspace, config.cipher.encrypt(f"bot:{token}".encode()).decode())


def decrypt_bot_token(config: SlackOAuthConfig, encrypted_token: str) -> str:
    try:
        token = config.cipher.decrypt(encrypted_token).decode()
    except (InvalidToken, ValueError):
        raise SlackOAuthError("Reconnect Slack to use the saved workspace.") from None
    if not token.startswith("bot:") or not token[4:]:
        raise SlackOAuthError("Reconnect Slack to use the saved workspace.")
    return token[4:]


def list_member_channels(bot_token: str, *, public_only: bool = False) -> list[SlackChannel]:
    channel_types = "public_channel" if public_only else "public_channel,private_channel"
    params = {"types": channel_types, "exclude_archived": "true", "limit": "200"}
    channels: dict[str, SlackChannel] = {}
    seen_cursors: set[str] = set()
    while True:
        data = _slack_json(HTTPMethod.GET, f"users.conversations?{urlencode(params)}", f"Bearer {bot_token}")
        try:
            page = _ChannelPage.model_validate(data)
        except ValidationError:
            raise SlackOAuthError("Slack returned an invalid channel list.") from None
        channels |= {
            channel.id: SlackChannel(id=channel.id, name=channel.name)
            for channel in page.channels
            if channel.is_member and not channel.is_archived and (not public_only or channel.is_private is False)
        }
        cursor = page.response_metadata.next_cursor.strip()
        if not cursor:
            return sorted(channels.values(), key=lambda channel: (channel.name.casefold(), channel.id))
        if cursor in seen_cursors:
            raise SlackOAuthError("Slack returned an invalid channel list.")
        seen_cursors.add(cursor)
        params["cursor"] = cursor


def _slack_json(
    method: HTTPMethod, path: str, authorization: str, *, form: dict[str, str] | None = None
) -> dict[str, object]:
    try:
        response = network.send_request(
            method=method, url=f"{_API_URL}/{path}", headers={"Authorization": authorization}, form=form
        )
        data = response.json()
    except (httpx2.HTTPError, ValueError):
        raise SlackOAuthError("Slack could not complete the request.") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise SlackOAuthError("Slack rejected the request. Check the app permissions and try again.")
    return data
