"""Shared fixtures. Every test gets its OWN temporary SQLite database file."""
from __future__ import annotations

import random
import socket
import threading
import time
from dataclasses import dataclass

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import build_engine, get_db, init_db, make_session_factory
from app.main import app
from app.worker.notification_worker import NotificationWorker
from tests.helpers import FakeClock, FakeNotificationService, future


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        notification_service_url="http://mock-service.test/notifications",
        max_delivery_attempts=5,
        retry_base_delay_seconds=2.0,
        retry_max_delay_seconds=60.0,
        retry_jitter_ratio=0.0,  # exact, predictable delays in tests
        lease_seconds=30,
    )


@pytest.fixture
def engine(settings):
    eng = build_engine(settings.database_url)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)  # no "with": lifespan (default DB init) is intentionally skipped
    app.dependency_overrides.clear()


class Api:
    """Thin helper around the HTTP API to keep tests readable."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self._counter = 0

    def workshop(self, **overrides) -> dict:
        payload = {"title": "Intro to FastAPI", "description": "Hands-on", "starts_at": future(), "capacity": 10}
        payload.update(overrides)
        response = self.client.post("/workshops", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    def member(self, name: str | None = None, email: str | None = None) -> dict:
        self._counter += 1
        payload = {"name": name or f"Member {self._counter}", "email": email or f"member{self._counter}@example.com"}
        response = self.client.post("/members", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    def register(self, workshop_id: int, member_id: int) -> httpx.Response:
        return self.client.post("/registrations", json={"workshop_id": workshop_id, "member_id": member_id})

    def register_ok(self, workshop_id: int, member_id: int) -> dict:
        response = self.register(workshop_id, member_id)
        assert response.status_code == 201, response.text
        return response.json()

    def cancel(self, workshop_id: int) -> httpx.Response:
        return self.client.post(f"/workshops/{workshop_id}/cancel")

    def jobs(self, **params) -> list[dict]:
        response = self.client.get("/notifications", params=params)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def api(client) -> Api:
    return Api(client)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_worker(settings, session_factory, clock):
    """Factory for workers wired to an in-process fake notification service."""
    clients: list[httpx.Client] = []

    def factory(service: FakeNotificationService | None = None, *, worker_id: str = "worker-1",
                sf=None, cfg: Settings | None = None, clk=None) -> NotificationWorker:
        service = service or FakeNotificationService()
        http = httpx.Client(transport=httpx.MockTransport(service))
        clients.append(http)
        return NotificationWorker(sf or session_factory, cfg or settings, http_client=http,
                                  clock=clk or clock, worker_id=worker_id, rng=random.Random(1))

    yield factory
    for c in clients:
        c.close()


# --------------------------------------------------------- live mock service
@dataclass
class LiveMock:
    url: str  # base url, e.g. http://127.0.0.1:54321

    @property
    def notifications_url(self) -> str:
        return f"{self.url}/notifications"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_mock(tmp_path):
    """Runs the real mock service with uvicorn in a background thread on a free port."""
    from mock_service.main import create_app

    mock_app = create_app(database_url=f"sqlite:///{tmp_path / 'mock.db'}")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:  # wait for readiness (condition-based, not a fixed sleep)
        assert time.monotonic() < deadline, "mock service did not start"
        time.sleep(0.01)
    yield LiveMock(url=f"http://127.0.0.1:{port}")
    server.should_exit = True
    thread.join(timeout=10)
