from __future__ import annotations

import pytest
import requests

from core.network_retry import is_retryable_network_error
from core.worker_proxy import (
    ProxyAcquireError,
    ProxyProbeError,
    ProxyProbeResult,
    WorkerProxyManager,
    WorkerProxyPolicy,
    probe_proxy,
)


@pytest.mark.parametrize(
    "message",
    [
        "Failed to perform, curl: (56) receive failure",
        "CONNECT tunnel failed",
        "proxy CONNECT failed, response 509",
    ],
)
def test_curl_proxy_tunnel_failures_are_retryable(message):
    assert is_retryable_network_error(RuntimeError(message)) is True


def test_proxy_http_509_is_retryable():
    response = requests.Response()
    response.status_code = 509
    error = requests.exceptions.HTTPError("proxy returned 509", response=response)

    assert is_retryable_network_error(error) is True


def _policy(attempts: int = 4) -> WorkerProxyPolicy:
    return WorkerProxyPolicy(
        acquire_max_attempts=attempts,
        acquire_retry_delay=0,
        replace_max_attempts=3,
        preflight_enabled=False,
    )


def test_api_extract_allows_same_ip_with_different_ports(monkeypatch):
    manager = WorkerProxyManager()
    monkeypatch.setattr(manager, "_provider_key", lambda: "api_extract")
    values = iter([
        "http://user:pass@1.2.3.4:1001",
        "http://user:pass@1.2.3.4:1002",
    ])

    first = manager.acquire(scope_id="task-1", getter=lambda: next(values), policy=_policy())
    second = manager.acquire(scope_id="task-1", getter=lambda: next(values), policy=_policy())

    assert first.url.endswith("1.2.3.4:1001")
    assert second.url.endswith("1.2.3.4:1002")
    first.release()
    second.release()


def test_api_extract_does_not_reuse_released_ip_and_port_in_same_task(monkeypatch):
    manager = WorkerProxyManager()
    monkeypatch.setattr(manager, "_provider_key", lambda: "api_extract")
    first = manager.acquire(
        scope_id="task-1",
        getter=lambda: "http://1.2.3.4:1001",
        policy=_policy(),
    )
    first.release()

    with pytest.raises(ProxyAcquireError, match="重复代理节点"):
        manager.acquire(
            scope_id="task-1",
            getter=lambda: "http://1.2.3.4:1001",
            policy=_policy(2),
        )


def test_rotating_gateway_can_share_ingress_between_workers(monkeypatch):
    manager = WorkerProxyManager()
    monkeypatch.setattr(manager, "_provider_key", lambda: "rotating_gateway")
    getter = lambda: "http://user:pass@gateway.example:8080"

    first = manager.acquire(scope_id="task-1", getter=getter, policy=_policy())
    second = manager.acquire(scope_id="task-1", getter=getter, policy=_policy())

    assert first.url == second.url
    first.release()
    second.release()


def test_proxy_service_never_falls_back_to_direct(monkeypatch):
    manager = WorkerProxyManager()
    monkeypatch.setattr(manager, "_provider_key", lambda: "api_extract")

    with pytest.raises(ProxyAcquireError, match="未返回可用代理"):
        manager.acquire(scope_id="task-1", getter=lambda: None, policy=_policy(2))


def test_manager_reextracts_after_preflight_failure(monkeypatch):
    manager = WorkerProxyManager()
    monkeypatch.setattr(manager, "_provider_key", lambda: "api_extract")
    values = iter(["http://1.2.3.4:1001", "http://5.6.7.8:1002"])

    def fake_probe(proxy_url, *, policy):
        if "1.2.3.4" in proxy_url:
            raise ProxyProbeError("connection timeout")
        return ProxyProbeResult(
            exit_ip="203.0.113.20",
            latency_ms=320,
            endpoint="https://www.cloudflare.com/cdn-cgi/trace",
        )

    monkeypatch.setattr("core.worker_proxy.probe_proxy", fake_probe)
    policy = WorkerProxyPolicy(
        acquire_max_attempts=2,
        acquire_retry_delay=0,
        preflight_enabled=True,
    )

    lease = manager.acquire(scope_id="task-1", getter=lambda: next(values), policy=policy)

    assert lease.url == "http://5.6.7.8:1002"
    assert lease.probe.exit_ip == "203.0.113.20"
    lease.release()


def test_probe_proxy_reads_cloudflare_exit_and_latency(monkeypatch):
    class Response:
        text = "fl=1\nip=203.0.113.10\nloc=JP\n"

        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError

    monkeypatch.setattr("core.worker_proxy.requests.get", lambda *args, **kwargs: Response())
    ticks = iter([10.0, 10.25])
    monkeypatch.setattr("core.worker_proxy.time.perf_counter", lambda: next(ticks))
    policy = WorkerProxyPolicy(
        preflight_timeout=8,
        preflight_max_latency_ms=1000,
        preflight_urls=("https://www.cloudflare.com/cdn-cgi/trace",),
    )

    result = probe_proxy("http://proxy.example:8080", policy=policy)

    assert result.exit_ip == "203.0.113.10"
    assert result.latency_ms == 250


def test_probe_proxy_rejects_slow_node(monkeypatch):
    class Response:
        text = '{"ip":"203.0.113.10"}'

        def raise_for_status(self):
            pass

        def json(self):
            return {"ip": "203.0.113.10"}

    monkeypatch.setattr("core.worker_proxy.requests.get", lambda *args, **kwargs: Response())
    ticks = iter([10.0, 16.0])
    monkeypatch.setattr("core.worker_proxy.time.perf_counter", lambda: next(ticks))
    policy = WorkerProxyPolicy(
        preflight_max_latency_ms=5000,
        preflight_urls=("https://api64.ipify.org?format=json",),
    )

    with pytest.raises(ProxyProbeError, match="超过上限"):
        probe_proxy("http://proxy.example:8080", policy=policy)
