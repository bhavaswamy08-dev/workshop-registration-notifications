from fastapi.testclient import TestClient

from mock_service.main import create_app


def test_main_api_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["database"] == "ok"


def test_docs_endpoints_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_mock_service_health(tmp_path):
    mock = TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'm.db'}"))
    response = mock.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok" and response.json()["mock"] is True
