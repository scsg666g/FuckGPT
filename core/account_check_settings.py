"""Runtime settings for scheduled account validity checks."""
from __future__ import annotations

from dataclasses import dataclass

from core.config_store import config_store
from core.proxy_resolution import (
    PROXY_MODE_DIRECT,
    PROXY_MODE_MANUAL,
    PROXY_MODE_PROXY_SERVICE,
    normalize_proxy_mode,
)


INVALID_CHECK_LIMIT = 2

DEFAULTS: dict[str, str] = {
    "account_validity_auto_enabled": "false",
    "account_validity_startup_delay_seconds": "300",
    "account_validity_interval_minutes": "360",
    "account_validity_batch_limit": "100",
    "account_validity_concurrency": "2",
    "account_validity_request_timeout_seconds": "20",
    "account_validity_proxy_mode": PROXY_MODE_DIRECT,
    "account_validity_proxy_url": "",
}

SETTING_KEYS = frozenset(DEFAULTS)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


@dataclass(frozen=True, slots=True)
class AccountCheckSettings:
    enabled: bool = False
    startup_delay_seconds: int = 300
    interval_minutes: int = 360
    batch_limit: int = 100
    concurrency: int = 2
    request_timeout_seconds: int = 20
    proxy_mode: str = PROXY_MODE_DIRECT
    proxy_url: str = ""
    invalid_check_limit: int = INVALID_CHECK_LIMIT


def get_account_check_settings() -> AccountCheckSettings:
    stored = config_store.get_all()
    data = {**DEFAULTS, **{key: str(value or "") for key, value in stored.items() if key in SETTING_KEYS}}
    proxy_mode = normalize_proxy_mode(
        data["account_validity_proxy_mode"],
        default=PROXY_MODE_DIRECT,
    )
    if proxy_mode not in {PROXY_MODE_DIRECT, PROXY_MODE_MANUAL, PROXY_MODE_PROXY_SERVICE}:
        proxy_mode = PROXY_MODE_DIRECT
    return AccountCheckSettings(
        enabled=_truthy(data["account_validity_auto_enabled"]),
        startup_delay_seconds=_bounded_int(data["account_validity_startup_delay_seconds"], 300, 0, 86400),
        interval_minutes=_bounded_int(data["account_validity_interval_minutes"], 360, 5, 43200),
        batch_limit=_bounded_int(data["account_validity_batch_limit"], 100, 1, 1000),
        concurrency=_bounded_int(data["account_validity_concurrency"], 2, 1, 20),
        request_timeout_seconds=_bounded_int(data["account_validity_request_timeout_seconds"], 20, 5, 300),
        proxy_mode=proxy_mode,
        proxy_url=str(data["account_validity_proxy_url"] or "").strip(),
    )
