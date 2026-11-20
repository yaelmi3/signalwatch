import json
from base64 import b64decode
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from signalwatch import slack_delivery, storage
from signalwatch.analysis_models import ChangeAnalysisResult, TeamDecision
from signalwatch.main import app

_ROOT = "/api/maintainer/slack"
_ORIGIN = "https://signalwatch.test"


def test_slack_operator_connects_selects_tests_and_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "slack.sqlite3"
    monkeypatch.setenv("SIGNALWATCH_DB_PATH", str(db_path))
    key = Fernet.generate_key()
    monkeypatch.setenv("SIGNALWATCH_SLACK_CLIENT_ID", "123.456")
    monkeypatch.setenv("SIGNALWATCH_SLACK_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("SIGNALWATCH_SLACK_REDIRECT_URL", f"{_ORIGIN}{_ROOT}/callback")
    monkeypatch.setenv("SIGNALWATCH_SLACK_MAINTAINER_PASSWORD", "fake-passphrase")
    monkeypatch.setenv("SIGNALWATCH_SLACK_ENCRYPTION_KEY", key.decode())
    monkeypatch.setenv("SIGNALWATCH_SLACK_BOT_TOKEN", "fake-legacy-token")
    monkeypatch.setenv("SIGNALWATCH_SLACK_CHANNEL_ID", "C_OLD")
    requests: list[httpx2.Request] = []
    sent_to: list[str] = []
    channels_available = True

    def slack_response(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "slack.com"
        requests.append(request)
        if request.url.path == "/api/oauth.v2.access":
            assert request.method == HTTPMethod.POST
            assert b64decode(request.headers["authorization"].removeprefix("Basic ")) == b"123.456:fake-client-secret"
            assert parse_qs(request.content.decode()) == {
                "code": ["fake-install-code"],
                "redirect_uri": [f"{_ORIGIN}{_ROOT}/callback"],
            }
            body = {"token_type": "bot", "access_token": "fake-bot-token", "team": {"name": "Sandbox"}}
        else:
            assert request.headers["authorization"] == "Bearer fake-bot-token"
            if request.url.path == "/api/users.conversations":
                assert request.method == HTTPMethod.GET
                if not channels_available:
                    return httpx2.Response(HTTPStatus.SERVICE_UNAVAILABLE)
                params = request.url.params
                assert params["types"] == "public_channel,private_channel" and params["exclude_archived"] == "true"
                assert "user" not in params
                channels = [{"id": "G_TEAM", "name": "team-private"}]
                if not params.get("cursor"):
                    channels = [
                        {"id": "C_TEAM", "name": "team-public"},
                        {"id": "C_ARCHIVED", "name": "archived", "is_archived": True},
                    ]
                body = {
                    "channels": channels,
                    "response_metadata": {"next_cursor": "" if params.get("cursor") else "next"},
                }
            else:
                assert request.url.path == "/api/chat.postMessage" and request.method == HTTPMethod.POST
                body_data = json.loads(request.content)
                assert body_data["text"] == "SignalWatch test message. Slack is connected."
                assert not body_data["mrkdwn"] and not body_data["unfurl_links"]
                sent_to.append(body_data["channel"])
                body = {}
        return httpx2.Response(HTTPStatus.OK, json={"ok": True, **body})

    class FakeClient(httpx2.Client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(transport=httpx2.MockTransport(slack_response), **kwargs)

    monkeypatch.setattr(httpx2, "Client", FakeClient)
    public = {
        "available": True,
        "authenticated": False,
        "connected": False,
        "workspace_name": None,
        "team_channels": {},
        "enabled": False,
        "revision": None,
    }
    with TestClient(app, base_url=_ORIGIN) as operator, TestClient(app, base_url=_ORIGIN) as visitor:
        assert visitor.get(_ROOT).json() == public
        assert visitor.get(f"{_ROOT}/channels").status_code == HTTPStatus.UNAUTHORIZED
        assert visitor.post(f"{_ROOT}/connect", json={}).status_code == HTTPStatus.UNAUTHORIZED
        assert slack_delivery.get_settings() is None
        password = {"password": "fake-passphrase"}
        assert operator.post(f"{_ROOT}/session", json=password).status_code == HTTPStatus.FORBIDDEN
        operator.headers["Origin"] = _ORIGIN
        invalid = operator.post(f"{_ROOT}/session", json={"password": ["never-echo-this"]})
        assert invalid.status_code == HTTPStatus.UNAUTHORIZED and "never-echo-this" not in invalid.text
        session = operator.post(f"{_ROOT}/session", json=password)
        assert session.status_code == HTTPStatus.NO_CONTENT and not session.content
        cookie = session.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", "max-age="))
        assert operator.get(_ROOT).json() == {**public, "authenticated": True}
        auth_url = urlsplit(operator.post(f"{_ROOT}/connect", json={}).json()["url"])
        params = parse_qs(auth_url.query)
        assert auth_url.netloc == "slack.com" and auth_url.path == "/oauth/v2/authorize"
        assert params["scope"] == ["chat:write,channels:read,groups:read"] and "user_scope" not in params
        callback = f"{_ROOT}/callback"
        query = {"code": "fake-install-code", "state": params["state"][0]}
        forged = operator.get(callback, params={**query, "state": "other"}, follow_redirects=False)
        foreign = visitor.get(callback, params=query, follow_redirects=False)
        assert forged.headers["location"] == foreign.headers["location"] == "/?view=maintainer&slack=error"
        assert not requests
        returned = operator.get(callback, params=query, follow_redirects=False)
        assert returned.status_code == HTTPStatus.SEE_OTHER
        assert returned.headers["location"] == "/?view=maintainer&slack=connected"
        assert "fake-install-code" not in returned.text + str(returned.headers)
        replay = operator.get(callback, params=query, follow_redirects=False)
        assert replay.headers["location"] == "/?view=maintainer&slack=error" and len(requests) == 1
        assert visitor.get(_ROOT).json() == public
        connected = {**public, "authenticated": True, "connected": True, "workspace_name": "Sandbox"}
        status = operator.get(_ROOT).json()
        initial_revision = status["revision"]
        assert isinstance(initial_revision, str) and initial_revision
        assert status == connected | {"revision": initial_revision}
        connection = storage.get_slack_connection()
        assert connection and Fernet(key).decrypt(connection.encrypted_token) == b"bot:fake-bot-token"
        assert b"fake-bot-token" not in db_path.read_bytes()
        assert operator.get(f"{_ROOT}/channels").json() == [
            {"id": "G_TEAM", "name": "team-private"},
            {"id": "C_TEAM", "name": "team-public"},
        ]
        team_channels = {"marketing": "C_TEAM", "product": "G_TEAM", "research_and_development": "G_TEAM"}
        destination = {"team_channels": team_channels, "enabled": False, "revision": initial_revision}
        cross_site = operator.put(_ROOT, json=destination, headers={"Origin": "https://other.test"})
        assert cross_site.status_code == HTTPStatus.FORBIDDEN
        assert operator.post(f"{_ROOT}/test", json={"team": "product"}).status_code == HTTPStatus.BAD_REQUEST
        empty_enabled = destination | {"team_channels": {}, "enabled": True}
        assert operator.put(_ROOT, json=empty_enabled).status_code == HTTPStatus.BAD_REQUEST
        wrong_channel = destination | {"team_channels": {**team_channels, "marketing": "C_OTHER"}}
        assert operator.put(_ROOT, json=wrong_channel).status_code == HTTPStatus.BAD_REQUEST
        invalid_team = destination | {"team_channels": {"sales": "C_TEAM"}}
        assert operator.put(_ROOT, json=invalid_team).status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        before_save = len(requests)
        saved = operator.put(_ROOT, json=destination).json()
        assert saved["revision"] and saved["revision"] != initial_revision
        destination["revision"] = saved["revision"]
        assert saved == connected | destination
        assert [request.url.params.get("cursor") for request in requests[before_save:]] == [None, "next"]
        assert storage.save_slack_destination(connection, {}, False) is None
        assert visitor.get(_ROOT).json() == public
        assert slack_delivery.configured_channels() == {}
        for team in ("product", "marketing"):
            test = operator.post(f"{_ROOT}/test", json={"team": team})
            assert test.status_code == HTTPStatus.NO_CONTENT and not test.content
        assert sent_to == ["G_TEAM", "C_TEAM"]
        assert operator.get(_ROOT).json()["enabled"] is False
        enabled = operator.put(_ROOT, json=destination | {"enabled": True}).json()
        assert enabled["enabled"] is True and enabled["revision"] != saved["revision"]
        assert slack_delivery.configured_channels() == team_channels
        previously_enabled = slack_delivery.get_settings()
        assert previously_enabled is not None and previously_enabled.bot_token == "fake-bot-token"
        page = storage.save_competitor("Test source", "https://publisher.test/").pages[0]
        storage.link_watch(page.id, "fake-watch")
        assert storage.save_page_version(
            page_id=page.id, watch_id="fake-watch", version=1, before="Previous", after="Update"
        )
        held = TeamDecision(relevant=False, reason="No match.", quote=None)
        analysis = ChangeAnalysisResult(
            summary="Update",
            marketing=held,
            research_and_development=held,
            product=TeamDecision(relevant=True, reason="Test match.", quote="Update"),
        )
        storage.save_change_analysis(page.id, 1, analysis, slack_channels=slack_delivery.configured_channels())
        channels_available = False
        before_disable = len(requests)
        disabled = operator.put(_ROOT, json=destination | {"revision": enabled["revision"]}).json()
        assert disabled["enabled"] is False
        other_tab = destination | {"revision": disabled["revision"]}
        remaining = other_tab | {"team_channels": {"product": "G_TEAM"}}
        current = operator.put(_ROOT, json=remaining).json()
        assert current["revision"] and current["revision"] != other_tab["revision"]
        assert current == connected | remaining | {"revision": current["revision"]}
        for enable in (False, True):
            assert operator.put(_ROOT, json=other_tab | {"enabled": enable}).status_code == HTTPStatus.CONFLICT
        assert operator.get(_ROOT).json() == current
        assert len(requests) == before_disable
        assert slack_delivery.get_settings() is None
        slack_delivery.send_next(previously_enabled)
        assert sent_to == ["G_TEAM", "C_TEAM"]


def test_incomplete_oauth_is_unavailable_and_legacy_worker_mode_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIGNALWATCH_SLACK_BOT_TOKEN", "fake-legacy-token")
    monkeypatch.setenv("SIGNALWATCH_SLACK_CHANNEL_ID", "C_OLD")
    assert slack_delivery.configured_channels() == dict.fromkeys(storage.DigestTeam, "C_OLD")
    monkeypatch.setenv("SIGNALWATCH_SLACK_CLIENT_ID", "123.456")
    assert not TestClient(app, base_url=_ORIGIN).get(_ROOT).json()["available"]
    assert slack_delivery.get_settings() is None
