from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from providers.registry import register_provider


@dataclass(slots=True)
class PushResponse:
    ok: bool
    http_status: int = 0
    error: str = ""


@register_provider("push", "nvtokens")
class NVTokensPushProvider:
    def __init__(self, endpoint: str, api_key: str, timeout: float = 20.0):
        self.endpoint = endpoint.strip()
        self.api_key = api_key.strip()
        self.timeout = max(1.0, min(float(timeout), 120.0))

    @classmethod
    def from_config(cls, config: dict) -> "NVTokensPushProvider":
        return cls(
            endpoint=str(config.get("nvtokens_endpoint") or "https://nvtokens.com/api/inventory/cards/import"),
            api_key=str(config.get("nvtokens_api_key") or ""),
            timeout=float(config.get("nvtokens_timeout") or 20),
        )

    def configuration_error(self) -> str:
        if not self.api_key:
            return "请先配置 NexusVault API Key"
        try:
            parsed = urlsplit(self.endpoint)
        except ValueError:
            return "NexusVault 推送地址无效"
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "NexusVault 推送地址必须是有效的 HTTP(S) 地址"
        return ""

    def push(self, payload: dict) -> PushResponse:
        configuration_error = self.configuration_error()
        if configuration_error:
            return PushResponse(ok=False, error=configuration_error)
        try:
            response = httpx.post(
                self.endpoint,
                headers={
                    "content-type": "application/json",
                    "x-api-key": self.api_key,
                },
                json=payload,
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            return PushResponse(ok=False, error=f"请求超时（{self.timeout:g} 秒）")
        except httpx.HTTPError as exc:
            return PushResponse(ok=False, error=f"连接失败：{type(exc).__name__}")

        if 200 <= response.status_code < 300:
            return PushResponse(ok=True, http_status=response.status_code)
        return PushResponse(
            ok=False,
            http_status=response.status_code,
            error=f"远端返回 HTTP {response.status_code}",
        )
