import os
from dataclasses import dataclass, field
from html import escape
from http import HTTPMethod, HTTPStatus

import httpx2
import structlog

from signalwatch import network, storage

_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_REJECTED_HTTP = {
    HTTPStatus.BAD_REQUEST,
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.TOO_MANY_REQUESTS,
}
_SLACK_REJECTIONS = {
    "account_inactive",
    "channel_not_found",
    "invalid_auth",
    "is_archived",
    "missing_scope",
    "no_permission",
    "not_authed",
    "not_in_channel",
    "ratelimited",
    "restricted_action",
    "token_revoked",
}
_TEAM_NAMES = {
    storage.DigestTeam.MARKETING: "Marketing",
    storage.DigestTeam.PRODUCT: "Product",
    storage.DigestTeam.RESEARCH_AND_DEVELOPMENT: "R&D",
}
slack_logger = structlog.get_logger()


@dataclass(frozen=True)
class SlackSettings:
    channel_id: str
    bot_token: str = field(repr=False)


def get_settings() -> SlackSettings | None:
    token = os.getenv("SIGNALWATCH_SLACK_BOT_TOKEN", "").strip()
    channel_id = os.getenv("SIGNALWATCH_SLACK_CHANNEL_ID", "").strip()
    if bool(token) != bool(channel_id):
        raise ValueError("Set both SIGNALWATCH_SLACK_BOT_TOKEN and SIGNALWATCH_SLACK_CHANNEL_ID, or leave both unset.")
    if not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("SIGNALWATCH_SLACK_BOT_TOKEN must be ASCII without whitespace.")
    return SlackSettings(channel_id, token) if token else None


def configured_channel() -> str | None:
    settings = get_settings()
    return settings.channel_id if settings else None


def send_next(settings: SlackSettings) -> None:
    delivery = storage.claim_slack_delivery(settings.channel_id)
    if delivery is None:
        return
    try:
        _post_message(settings, delivery.item)
    except SlackPostError as error:
        status = "uncertain" if error.uncertain else "failed"
        storage.finish_slack_delivery(delivery.id, status, error.code)
        slack_logger.warning(
            "Slack delivery did not complete", delivery_id=delivery.id, status=status, error=error.code
        )
    else:
        storage.finish_slack_delivery(delivery.id, "sent")


class SlackPostError(Exception):
    def __init__(self, code: str, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


def _post_message(settings: SlackSettings, item: storage.DigestArticle | storage.DigestEntry) -> None:
    heading = item.source_name[:160]
    source_url = item.page_url if isinstance(item, storage.DigestEntry) else item.article_url
    if isinstance(item, storage.DigestArticle):
        heading = f"{heading} · {item.title[:180]}"
    reasons = "\n".join(f"{_TEAM_NAMES[team]}: {item.team_reasons[team][:220]}" for team in item.relevant_teams)
    summary = (item.summary or "Change detected.")[:800]
    message = escape(f"{heading}\n{summary}\n\n{reasons}\n\n{source_url}", quote=False)
    try:
        response = network.send_request(
            method=HTTPMethod.POST,
            url=_POST_MESSAGE_URL,
            headers={"Authorization": f"Bearer {settings.bot_token}"},
            payload={
                "channel": settings.channel_id,
                "text": message,
                "mrkdwn": False,
                "link_names": False,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
    except httpx2.HTTPStatusError as error:
        code = error.response.status_code
        raise SlackPostError(f"http_{code}", uncertain=code not in _REJECTED_HTTP) from None
    except httpx2.RequestError:
        raise SlackPostError("transport_error", uncertain=True) from None

    try:
        result = response.json()
    except ValueError:
        raise SlackPostError("invalid_response", uncertain=True) from None
    if isinstance(result, dict):
        if result.get("ok") is True:
            return
        error = result.get("error")
        if result.get("ok") is False and isinstance(error, str) and error in _SLACK_REJECTIONS:
            raise SlackPostError(error)
    raise SlackPostError("unconfirmed_response", uncertain=True)
