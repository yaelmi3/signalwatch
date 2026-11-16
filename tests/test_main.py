from fastapi.testclient import TestClient

from signalwatch.main import app


def test_serves_frontend_and_api_docs() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "<title>SignalWatch</title>" in page.text

        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "Swagger UI" in docs.text
