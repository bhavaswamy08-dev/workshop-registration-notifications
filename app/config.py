"""Environment-based configuration for the registration API and the worker.

Every value has a safe default so the project starts without editing code.
Override values with environment variables (or a local ``.env`` file, see
``.env.example``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (no extra dependency). Real environment variables win."""
    env_file = Path(path)
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/workshop.db"
    notification_service_url: str = "http://127.0.0.1:8001/notifications"

    # Retry policy: attempt n fails -> wait min(max_delay, base * 2**(n-1)) seconds.
    max_delivery_attempts: int = 5
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 60.0
    retry_jitter_ratio: float = 0.1  # +/-10 % random spread; set 0 for fixed delays

    # A claimed job must be finished (or its lease renewed) within this time,
    # otherwise another worker may take it over (crash recovery).
    lease_seconds: int = 30

    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 50  # max jobs handled per polling cycle

    http_connect_timeout_seconds: float = 2.0
    http_request_timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        env = os.environ.get
        return cls(
            database_url=env("DATABASE_URL", d.database_url),
            notification_service_url=env("NOTIFICATION_SERVICE_URL", d.notification_service_url),
            max_delivery_attempts=int(env("MAX_DELIVERY_ATTEMPTS", d.max_delivery_attempts)),
            retry_base_delay_seconds=float(env("RETRY_BASE_DELAY_SECONDS", d.retry_base_delay_seconds)),
            retry_max_delay_seconds=float(env("RETRY_MAX_DELAY_SECONDS", d.retry_max_delay_seconds)),
            retry_jitter_ratio=float(env("RETRY_JITTER_RATIO", d.retry_jitter_ratio)),
            lease_seconds=int(env("LEASE_SECONDS", d.lease_seconds)),
            worker_poll_interval_seconds=float(env("WORKER_POLL_INTERVAL_SECONDS", d.worker_poll_interval_seconds)),
            worker_batch_size=int(env("WORKER_BATCH_SIZE", d.worker_batch_size)),
            http_connect_timeout_seconds=float(env("HTTP_CONNECT_TIMEOUT_SECONDS", d.http_connect_timeout_seconds)),
            http_request_timeout_seconds=float(env("HTTP_REQUEST_TIMEOUT_SECONDS", d.http_request_timeout_seconds)),
        )


@lru_cache
def get_settings() -> Settings:
    load_dotenv()
    return Settings.from_env()
