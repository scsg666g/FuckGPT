"""Network failure classification and bounded retry helpers.

The project talks to several third-party APIs through user supplied proxies.
Those proxies can reset an otherwise healthy connection at any point.  Keep
the classification here so callers do not have to match a growing collection
of requests/urllib3/Windows error strings independently.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import requests


T = TypeVar("T")

_RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 509}
_RETRYABLE_OS_ERROR_CODES = {
    54,     # ECONNRESET (macOS)
    104,    # ECONNRESET (Linux)
    110,    # ETIMEDOUT (Linux)
    111,    # ECONNREFUSED (Linux)
    10053,  # WSAECONNABORTED
    10054,  # WSAECONNRESET
    10060,  # WSAETIMEDOUT
    10061,  # WSAECONNREFUSED
}
_RETRYABLE_MESSAGE_TOKENS = (
    "proxyerror",
    "proxy error",
    "unable to connect to proxy",
    "cannot connect to proxy",
    "proxy connection failed",
    "connect tunnel failed",
    "curl: (56)",
    "response 509",
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_connection_reset",
    "err_connection_closed",
    "err_connection_refused",
    "err_timed_out",
    "socks connection failed",
    "connectionreseterror",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remotedisconnected",
    "remote end closed connection",
    "broken pipe",
    "temporarily unavailable",
    "connect timeout",
    "read timeout",
    "timed out",
    "sslerror",
    "ssleoferror",
    "unexpected_eof",
    "eof occurred",
    "network is unreachable",
    "name resolution",
    "getaddrinfo failed",
    "max retries exceeded",
    "远程主机强迫关闭",
    "连接超时",
    "代理连接失败",
)


def _exception_chain(exc: BaseException):
    """Yield an exception and its explicit/implicit causes without looping."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_retryable_network_error(exc: BaseException) -> bool:
    """Return whether *exc* represents a likely transient network failure.

    HTTP client libraries frequently wrap ``ProxyError`` several layers deep,
    so both the exception chain and sanitized message are inspected.  Normal
    provider/API errors are deliberately excluded.
    """
    messages: list[str] = []
    for current in _exception_chain(exc):
        messages.append(str(current or ""))
        if isinstance(current, (requests.exceptions.ProxyError, requests.exceptions.Timeout)):
            return True
        if isinstance(current, requests.exceptions.ConnectionError):
            return True
        if isinstance(current, requests.exceptions.HTTPError):
            response = getattr(current, "response", None)
            if int(getattr(response, "status_code", 0) or 0) in _RETRYABLE_HTTP_STATUS_CODES:
                return True
        if isinstance(current, OSError):
            code = getattr(current, "winerror", None) or getattr(current, "errno", None)
            if code in _RETRYABLE_OS_ERROR_CODES:
                return True

    lowered = " | ".join(messages).lower()
    return any(token in lowered for token in _RETRYABLE_MESSAGE_TOKENS)


def retry_network_call(
    operation: Callable[[], T],
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    on_retry: Callable[[int, int, float, BaseException], None] | None = None,
) -> T:
    """Run *operation*, retrying only transient network failures.

    ``max_attempts`` includes the first call.  Delays use capped exponential
    backoff (1, 2, 4, ... by default).  The last original exception is raised
    unchanged so provider wrappers retain their existing error semantics.
    """
    attempts = max(int(max_attempts or 1), 1)
    initial_delay = max(float(base_delay or 0), 0)
    delay_cap = max(float(max_delay or 0), initial_delay)
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= attempts or not is_retryable_network_error(exc):
                raise
            delay = min(initial_delay * (2 ** (attempt - 1)), delay_cap)
            if on_retry is not None:
                try:
                    on_retry(attempt, attempts, delay, exc)
                except Exception:
                    # Observability must never become another reason for a
                    # recoverable request to abort.
                    pass
            if delay > 0:
                time.sleep(delay)
    raise AssertionError("unreachable")
