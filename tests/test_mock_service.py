import pytest
from fastapi.testclient import TestClient

from mock_service.main import create_app

BODY = {
    "notification_id": "registration_confirmation:1:1", "notification_type": "registration_confirmation",
    "recipient_member_id": 1, "recipient_name": "Asha", "recipient_email": "asha@example.com",
    "workshop_id": 1, "workshop_title": "Intro", "message": "You are registered.",
}


@pytest.fixture
def mock(tmp_path):
    return TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'mock.db'}"))


def test_delivery_is_accepted_and_stored(mock):
    response = mock.post("/notifications", json=BODY)
    assert response.status_code == 201
    assert response.json()["status"] == "delivered" and "No real email" in response.json()["note"]
    records = mock.get("/deliveries").json()
    assert len(records) == 1 and records[0]["recipient_email"] == "asha@example.com"


def test_duplicate_notification_id_is_idempotent(mock):
    """Requirement 22: the same notification_id never creates a second delivery record."""
    first = mock.post("/notifications", json=BODY)
    second = mock.post("/notifications", json=BODY)
    third = mock.post("/notifications", json={**BODY, "message": "changed text, same id"})
    assert (first.status_code, second.status_code, third.status_code) == (201, 200, 200)
    assert second.json()["status"] == "duplicate_ignored"
    assert second.json()["delivery_id"] == first.json()["delivery_id"]
    records = mock.get("/deliveries").json()
    assert len(records) == 1 and records[0]["duplicate_count"] == 2
    assert records[0]["message"] == "You are registered."  # original kept


def test_different_notification_ids_create_separate_records(mock):
    mock.post("/notifications", json=BODY)
    mock.post("/notifications", json={**BODY, "notification_id": "workshop_cancellation:1:1",
                                      "notification_type": "workshop_cancellation"})
    assert len(mock.get("/deliveries").json()) == 2
    assert len(mock.get("/deliveries", params={"notification_type": "workshop_cancellation"}).json()) == 1


def test_fail_next_n_requests_then_recover(mock):
    config = mock.post("/admin/fail-next", params={"count": 2, "status_code": 503}).json()
    assert config["mode"] == "error" and config["remaining"] == 2
    assert [mock.post("/notifications", json=BODY).status_code for _ in range(3)] == [503, 503, 201]
    assert mock.get("/admin/config").json()["mode"] == "normal"
    assert len(mock.get("/deliveries").json()) == 1  # failed requests stored nothing


def test_always_fail_http_500_until_reset(mock):
    mock.put("/admin/config", json={"mode": "error", "error_status": 500})
    assert all(mock.post("/notifications", json=BODY).status_code == 500 for _ in range(4))
    assert mock.get("/deliveries").json() == []
    reset = mock.post("/admin/reset").json()
    assert reset["mode"] == "normal"
    assert mock.post("/notifications", json=BODY).status_code == 201


def test_per_recipient_failure(mock):
    mock.put("/admin/config", json={"mode": "normal", "fail_recipients": ["Bad@Example.com"]})
    bad = {**BODY, "notification_id": "x:1", "recipient_email": "bad@example.com"}
    assert mock.post("/notifications", json=bad).status_code == 500
    assert mock.post("/notifications", json=BODY).status_code == 201


def test_timeout_mode_stalls_then_returns_504(mock):
    mock.post("/admin/timeout-next", params={"count": 1, "seconds": 0.05})
    assert mock.post("/notifications", json=BODY).status_code == 504
    assert mock.get("/deliveries").json() == []
    assert mock.post("/notifications", json=BODY).status_code == 201  # back to normal


def test_timeout_after_accept_then_retry_is_a_duplicate(mock):
    """The receiver stored the notification, but the sender never saw the answer."""
    mock.post("/admin/timeout-next", params={"count": 1, "seconds": 0.05, "record_before_timeout": True})
    assert mock.post("/notifications", json=BODY).status_code == 504
    assert len(mock.get("/deliveries").json()) == 1  # already stored!
    retry = mock.post("/notifications", json=BODY)
    assert retry.status_code == 200 and retry.json()["status"] == "duplicate_ignored"
    assert len(mock.get("/deliveries").json()) == 1


def test_reset_can_clear_deliveries(mock):
    mock.post("/notifications", json=BODY)
    mock.post("/admin/reset", params={"clear_deliveries": True})
    assert mock.get("/deliveries").json() == []


def test_invalid_delivery_request_rejected(mock):
    assert mock.post("/notifications", json={"notification_id": "only-id"}).status_code == 422
    assert mock.put("/admin/config", json={"mode": "explode"}).status_code == 422
    assert mock.post("/admin/fail-next", params={"count": 0}).status_code == 422


def test_delivery_records_are_persistent(tmp_path):
    url = f"sqlite:///{tmp_path / 'persist.db'}"
    TestClient(create_app(database_url=url)).post("/notifications", json=BODY)
    restarted = TestClient(create_app(database_url=url))  # "restart" the service
    assert len(restarted.get("/deliveries").json()) == 1
    assert restarted.post("/notifications", json=BODY).json()["status"] == "duplicate_ignored"
