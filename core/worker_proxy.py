"""Thread-safe worker-owned proxy leases.

Business workers own a proxy for the lifetime of one remote operation.  The
manager serializes extraction API calls, prevents concurrent workers from
sharing an IP:port endpoint, and keeps one task from reusing an endpoint for
multiple accounts.
"""
from __future__ import annotations

import threading
import time
from ipaddress import ip_address
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import requests

from core.proxy_resolution import mask_proxy_url


DEFAULT_PROXY_PROBE_URLS = (
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://api64.ipify.org?format=json",
)


class ProxyAcquireError(RuntimeError):
    """Raised when proxy-service mode cannot obtain a usable lease."""


class ProxyProbeError(RuntimeError):
    """Raised when an extracted proxy fails exit-IP or latency validation."""


@dataclass(frozen=True)
class ProxyProbeResult:
    exit_ip: str
    latency_ms: int
    endpoint: str


@dataclass(frozen=True)
class WorkerProxyPolicy:
    acquire_max_attempts: int = 8
    acquire_retry_delay: float = 1.0
    replace_max_attempts: int = 3
    preflight_enabled: bool = True
    preflight_timeout: float = 8.0
    preflight_max_latency_ms: int = 5000
    preflight_urls: tuple[str, ...] = DEFAULT_PROXY_PROBE_URLS

    @classmethod
    def from_config(cls, config: dict | None) -> "WorkerProxyPolicy":
        config = dict(config or {})
        def integer(key: str, default: int) -> int:
            try:
                return max(int(float(config.get(key) or default)), 1)
            except (TypeError, ValueError):
                return default

        def number(key: str, default: float) -> float:
            try:
                return max(float(config.get(key) if config.get(key) not in (None, "") else default), 0.0)
            except (TypeError, ValueError):
                return default

        def enabled(key: str, default: bool) -> bool:
            value = config.get(key)
            if value in (None, ""):
                return default
            return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开启", "启用"}

        custom_url = str(config.get("proxy_preflight_url") or "").strip()

        return cls(
            acquire_max_attempts=integer("proxy_acquire_max_attempts", 8),
            acquire_retry_delay=number("proxy_acquire_retry_delay", 1.0),
            replace_max_attempts=integer("proxy_replace_max_attempts", 3),
            preflight_enabled=enabled("proxy_preflight_enabled", True),
            preflight_timeout=max(number("proxy_preflight_timeout", 8.0), 0.5),
            preflight_max_latency_ms=integer("proxy_preflight_max_latency_ms", 5000),
            preflight_urls=(custom_url,) if custom_url else DEFAULT_PROXY_PROBE_URLS,
        )

    @classmethod
    def load(cls) -> "WorkerProxyPolicy":
        try:
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            settings = ProviderSettingsRepository().list_enabled("proxy")
            config = settings[0].get_config() if settings else {}
        except Exception:
            config = {}
        return cls.from_config(config)


def _extract_exit_ip(response: requests.Response) -> str:
    text = str(response.text or "").strip()
    candidates: list[str] = []
    for line in text.splitlines():
        if line.startswith("ip="):
            candidates.append(line.split("=", 1)[1].strip())
    try:
        data = response.json()
        if isinstance(data, dict):
            candidates.extend([str(data.get("ip") or ""), str(data.get("origin") or "")])
    except (ValueError, TypeError):
        pass
    candidates.append(text)
    for candidate in candidates:
        value = str(candidate or "").split(",", 1)[0].strip()
        try:
            return str(ip_address(value))
        except ValueError:
            continue
    raise ProxyProbeError("出口检测接口未返回有效 IP")


def probe_proxy(proxy_url: str, *, policy: WorkerProxyPolicy | None = None) -> ProxyProbeResult:
    """Measure HTTPS latency and discover the real exit IP through a proxy."""
    policy = policy or WorkerProxyPolicy.load()
    safe_proxy = mask_proxy_url(proxy_url)
    failures: list[str] = []
    for endpoint in policy.preflight_urls:
        started = time.perf_counter()
        try:
            response = requests.get(
                endpoint,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=policy.preflight_timeout,
            )
            response.raise_for_status()
            latency_ms = max(int(round((time.perf_counter() - started) * 1000)), 0)
            exit_ip = _extract_exit_ip(response)
            if policy.preflight_max_latency_ms > 0 and latency_ms > policy.preflight_max_latency_ms:
                raise ProxyProbeError(
                    f"延迟 {latency_ms}ms 超过上限 {policy.preflight_max_latency_ms}ms"
                )
            return ProxyProbeResult(exit_ip=exit_ip, latency_ms=latency_ms, endpoint=endpoint)
        except Exception as exc:
            error_type = type(exc).__name__
            safe_message = str(exc or "").replace(str(proxy_url or ""), safe_proxy)
            parsed_proxy = urlsplit(proxy_url)
            for secret in (parsed_proxy.username, parsed_proxy.password):
                if secret:
                    safe_message = safe_message.replace(secret, "***")
            failures.append(f"{urlsplit(endpoint).hostname or endpoint}: {error_type}: {safe_message}")
    raise ProxyProbeError("；".join(failures) or "代理预检失败")


class ProxyLease:
    def __init__(
        self,
        manager: "WorkerProxyManager",
        url: str,
        key: str,
        scope_id: str,
        probe: ProxyProbeResult | None = None,
    ):
        self.manager = manager
        self.url = url
        self.key = key
        self.scope_id = scope_id
        self.probe = probe
        self._released = False

    def report_success(self) -> None:
        try:
            from core.proxy_pool import proxy_pool

            proxy_pool.report_success(self.url)
        except Exception:
            pass

    def report_failure(self) -> None:
        try:
            from core.proxy_pool import proxy_pool

            proxy_pool.report_fail(self.url)
        except Exception:
            pass

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.manager.release(self)


class WorkerProxyManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._active: set[str] = set()
        self._used_by_scope: dict[str, set[str]] = {}

    @staticmethod
    def _provider_key() -> str:
        try:
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            return ProviderSettingsRepository().get_default_provider_key("proxy")
        except Exception:
            return ""

    @staticmethod
    def _key(url: str, *, unique_by_endpoint: bool) -> str:
        if not unique_by_endpoint:
            # Rotating gateways deliberately share an ingress endpoint.
            return ""
        try:
            parsed = urlsplit(url)
            host = str(parsed.hostname or "").strip().lower()
            if not host:
                return str(url or "").strip().lower()
            return f"{host}:{parsed.port}" if parsed.port else host
        except Exception:
            return str(url or "").strip().lower()

    def acquire(
        self,
        *,
        scope_id: str,
        getter: Callable[[], str | None] | None = None,
        log_fn: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        policy: WorkerProxyPolicy | None = None,
    ) -> ProxyLease:
        from core.proxy_pool import proxy_pool

        policy = policy or WorkerProxyPolicy.load()
        getter = getter or proxy_pool.get_next
        # Only a rotating gateway is expected to share one ingress while
        # assigning different exits. API-extracted and static nodes must be
        # unique by IP:port endpoint for every account in the task.
        unique_by_endpoint = self._provider_key() != "rotating_gateway"
        scope_id = str(scope_id or "global")
        last_reason = "代理服务未返回可用代理"

        for attempt in range(1, policy.acquire_max_attempts + 1):
            if callable(cancel_check) and cancel_check():
                raise RuntimeError("任务已取消")
            try:
                # Avoid extraction API bursts when many workers start together.
                with self._fetch_lock:
                    proxy = str(getter() or "").strip()
            except Exception as exc:
                proxy = ""
                last_reason = str(exc) or last_reason

            key = self._key(proxy, unique_by_endpoint=unique_by_endpoint) if proxy else ""
            duplicate = False
            if proxy:
                with self._lock:
                    used = self._used_by_scope.setdefault(scope_id, set())
                    duplicate = bool(key and (key in self._active or key in used))
                    if not duplicate:
                        if key:
                            self._active.add(key)
                            used.add(key)
                if not duplicate:
                    try:
                        probe = probe_proxy(proxy, policy=policy) if policy.preflight_enabled else None
                    except Exception as exc:
                        if key:
                            with self._lock:
                                self._active.discard(key)
                        last_reason = f"代理预检失败 {key}: {exc}"
                    else:
                        if probe is not None and callable(log_fn):
                            log_fn(
                                f"代理预检成功: 节点={key}，出口={probe.exit_ip}，"
                                f"延迟={probe.latency_ms}ms"
                            )
                        return ProxyLease(self, proxy, key, scope_id, probe)
            if duplicate:
                last_reason = f"提取到重复代理节点 {key}"
            if callable(log_fn):
                log_fn(
                    f"{last_reason}，{policy.acquire_retry_delay:g}s 后重新提取 "
                    f"({attempt}/{policy.acquire_max_attempts})"
                )
            if attempt < policy.acquire_max_attempts and policy.acquire_retry_delay > 0:
                time.sleep(policy.acquire_retry_delay)

        raise ProxyAcquireError(f"代理获取失败: {last_reason}")

    def release(self, lease: ProxyLease) -> None:
        if not lease.key:
            return
        with self._lock:
            self._active.discard(lease.key)

    def clear_scope(self, scope_id: str) -> None:
        with self._lock:
            self._used_by_scope.pop(str(scope_id or "global"), None)


worker_proxy_manager = WorkerProxyManager()
