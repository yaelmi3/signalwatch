from hmac import compare_digest
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from signalwatch import changedetection, storage
from signalwatch.changes import save_new_changes

router = APIRouter(tags=["Webhooks"])
_webhook_auth = HTTPBasic(auto_error=False)


class ChangedetectionWebhook(BaseModel):
    message: UUID


class WebhookSetupStatus(BaseModel):
    configured: bool


@router.get("/api/maintainer/webhooks")
def get_webhook_setup_status() -> WebhookSetupStatus:
    return WebhookSetupStatus(configured=changedetection.webhook_notification_url() is not None)


def _verify_secret(auth: Annotated[HTTPBasicCredentials | None, Depends(_webhook_auth)]) -> None:
    expected = changedetection.webhook_secret()
    if expected is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "The changedetection webhook is disabled.")
    if auth is None or auth.username != "signalwatch" or not compare_digest(auth.password, expected):
        raise HTTPException(
            HTTPStatus.UNAUTHORIZED, "Invalid webhook credentials.", headers=_webhook_auth.make_authenticate_headers()
        )


@router.post(changedetection.WEBHOOK_PATH, status_code=HTTPStatus.NO_CONTENT, dependencies=[Depends(_verify_secret)])
def receive_changedetection_webhook(payload: ChangedetectionWebhook) -> Response:
    watch_id = str(payload.message)
    page_id = storage.get_linked_page_id(watch_id)
    if page_id is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, "No page is linked to this watch.")
    try:
        save_new_changes(page_id, watch_id)
    except changedetection.EngineError as error:
        raise HTTPException(HTTPStatus.BAD_GATEWAY, str(error)) from None
    return Response(status_code=HTTPStatus.NO_CONTENT)
