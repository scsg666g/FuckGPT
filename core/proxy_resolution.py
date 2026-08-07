from __future__ import annotations

from typing import Callable


PROXY_MODE_DIRECT = "direct"
PROXY_MODE_MANUAL = "manual"
PROXY_MODE_PROXY_SERVICE = "proxy_service"
PROXY_MODE_FOLLOW_PLATFORM = "follow_platform"


def normalize_proxy_mode(value: str | None, *, default: str = PROXY_MODE_DIRECT) -> str:
    mode = str(value or "").strip().lower()
    if mode in {
        PROXY_MODE_DIRECT,
        PROXY_MODE_MANUAL,
        PROXY_MODE_PROXY_SERVICE,
        PROXY_MODE_FOLLOW_PLATFORM,
    }:
        return mode
    return default


def resolve_proxy_by_mode(
    mode: str | None,
    *,
    manual_proxy: str | None = None,
    follow_proxy: str | None = None,
    proxy_getter: Callable[[], str | None] | None = None,
    default: str = PROXY_MODE_DIRECT,
) -> str | None:
    resolved_mode = normalize_proxy_mode(mode, default=default)
    if resolved_mode == PROXY_MODE_MANUAL:
        return str(manual_proxy or "").strip() or None
    if resolved_mode == PROXY_MODE_PROXY_SERVICE:
        return proxy_getter() if proxy_getter else None
    if resolved_mode == PROXY_MODE_FOLLOW_PLATFORM:
        return str(follow_proxy or "").strip() or None
    return None


def mask_proxy_url(proxy_url: str | None) -> str:
    from urllib.parse import urlsplit, urlunsplit

    text = str(proxy_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        if not parsed.username and not parsed.password:
            return text
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"***:***@{host}{port}", parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return text
