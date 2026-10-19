"""Background notification worker (separate process).

Run:  python -m app.worker.notification_worker
Loop: claim one eligible job -> HTTP POST to the mock service -> record attempt
      -> mark delivered / schedule retry / give up. Jobs live in SQLite, so a
      restart simply continues where the previous worker stopped.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import socket
import threading
import uuid
from datetime import datetime
from typing import Callable

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.database import get_session_factory, init_db
from app.models import utcnow
from app.services import notification_service as svc
from app.services.notification_service import ClaimedJob, DeliveryResult

logger = logging.getLogger("notification_worker")


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class NotificationWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] = utcnow,
        worker_id: str | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.clock = clock  # injectable so tests can control time deterministically
        self.worker_id = worker_id or default_worker_id()
        self.rng = rng
        self._owns_client = http_client is None
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(settings.http_request_timeout_seconds, connect=settings.http_connect_timeout_seconds)
        )

    # ------------------------------------------------------------ delivery
    def deliver(self, job: ClaimedJob) -> DeliveryResult:
        """Send one HTTP request. Never raises; always returns a DeliveryResult."""
        body = {"notification_id": job.idempotency_key, "notification_type": job.notification_type, **job.payload}
        try:
            response = self.http.post(
                self.settings.notification_service_url,
                json=body,
                headers={"Idempotency-Key": job.idempotency_key},
            )
        except httpx.TimeoutException as exc:
            return DeliveryResult(False, "timeout", True, None, f"Request timed out ({type(exc).__name__})")
        except httpx.TransportError as exc:  # connection refused, DNS failure, reset ...
            return DeliveryResult(False, "connection_error", True, None, f"Connection error ({type(exc).__name__})")
        except httpx.HTTPError as exc:
            return DeliveryResult(False, "request_error", True, None, f"Request error ({type(exc).__name__})")
        except Exception as exc:  # noqa: BLE001 - one bad job must never kill the worker
            return DeliveryResult(False, "internal_error", True, None, f"Unexpected error ({type(exc).__name__})")

        status = response.status_code
        if 200 <= status < 300:
            return DeliveryResult(True, "success", True, status, None)
        # 4xx (except 408/429) means "your request is wrong": retrying cannot help.
        retryable = not (400 <= status < 500 and status not in (408, 429))
        return DeliveryResult(False, "http_error", retryable, status, f"HTTP {status} from notification service")

    # ------------------------------------------------------------ processing
    def process_next(self) -> bool:
        """Claim and process one job. Returns False when nothing is eligible."""
        with self.session_factory() as session:
            job = svc.claim_next_job(session, self.worker_id, self.clock(), self.settings)
        if job is None:
            return False

        started_at = self.clock()
        result = self.deliver(job)  # no DB transaction is open during the HTTP call
        finished_at = self.clock()
        with self.session_factory() as session:
            new_status = svc.finalize_attempt(
                session, job, self.worker_id, result, started_at, finished_at, self.settings, self.rng
            )
        logger.info("job=%s type=%s attempt=%s outcome=%s -> %s",
                    job.id, job.notification_type, job.attempts, result.outcome, new_status)
        return True

    def process_once(self, max_jobs: int | None = None) -> int:
        """Process eligible jobs until none are left (bounded by max_jobs)."""
        limit = max_jobs or self.settings.worker_batch_size
        processed = 0
        while processed < limit and self.process_next():
            processed += 1
        return processed

    def recover_stuck_jobs(self) -> int:
        with self.session_factory() as session:
            return svc.recover_expired_jobs(session, self.clock(), self.settings)

    def run_forever(self, stop: threading.Event) -> None:
        logger.info("worker %s started (poll every %.1fs)", self.worker_id, self.settings.worker_poll_interval_seconds)
        while not stop.is_set():
            try:
                processed = self.process_once()
            except Exception:  # noqa: BLE001 - keep the loop alive; error details only, no payloads
                logger.exception("unexpected error in worker loop")
                processed = 0
            if processed == 0:
                stop.wait(self.settings.worker_poll_interval_seconds)
        logger.info("worker %s stopped", self.worker_id)

    def close(self) -> None:
        if self._owns_client:
            self.http.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Notification delivery worker")
    parser.add_argument("--once", action="store_true", help="process the currently eligible jobs and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    init_db()
    worker = NotificationWorker(get_session_factory(), settings)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):  # pragma: no cover - platform specific
            pass
    try:
        if args.once:
            logger.info("processed %s job(s)", worker.process_once())
        else:
            worker.run_forever(stop)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
