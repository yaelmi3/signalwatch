import os
from hmac import compare_digest
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel

from signalwatch import changedetection, storage

router = APIRouter(tags=["Webhooks"])


class ChangedetectionWebhook(BaseModel):
    message: UUID


class WebhookSetupStatus(BaseModel):
    configured: bool


@router.get("/api/maintainer/webhooks")
def get_webhook_setup_status() -> WebhookSetupStatus:
    return WebhookSetupStatus(configured=changedetection.webhook_notification_url() is not None)


def _verify_secret(secret: Annotated[str | None, Header(alias=changedetection.WEBHOOK_SECRET_HEADER)] = None) -> None:
    expected = os.getenv(changedetection.WEBHOOK_SECRET_ENV)
    if not expected:
        raise HTTPException(HTTPStatus.NOT_FOUND, "The changedetection webhook is disabled.")
    if secret is None or not compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "Invalid webhook secret.")


@router.post(changedetection.WEBHOOK_PATH, status_code=HTTPStatus.NO_CONTENT, dependencies=[Depends(_verify_secret)])
def receive_changedetection_webhook(payload: ChangedetectionWebhook) -> Response:
    watch_id = str(payload.message)
    page_id = storage.get_linked_page_id(watch_id)
    if page_id is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "No page is linked to this watch.")
    try:
        _save_new_changes(page_id, watch_id)
    except changedetection.EngineError as error:
        raise HTTPException(HTTPStatus.BAD_GATEWAY, str(error)) from None
    return Response(status_code=HTTPStatus.NO_CONTENT)


def _save_new_changes(page_id: int, watch_id: str) -> None:
    history = changedetection.read_history(watch_id)
    checkpoint = storage.latest_page_change(page_id)
    if checkpoint is None:
        if len(history) < 2:
            return
        baseline = history[0]
        before = changedetection.read_snapshot(watch_id, baseline)
    else:
        baseline = int(checkpoint.version)
        before = checkpoint.after

    for version in history:
        if version <= baseline:
            continue
        after = changedetection.read_snapshot(watch_id, version)
        change = storage.PageChange(version=str(version), before=before, after=after)
        storage.save_page_change(page_id=page_id, watch_id=watch_id, change=change)
        before = after
