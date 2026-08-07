from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from providers.registry import register_provider


@dataclass(slots=True)
class PushResponse:
    ok: bool
    http_status: int = 0
    error: str = ""


@register_provider("push", "cpa")
class CPAPushProvider:
    """Upload Codex auth files to a CLIProxyAPI management endpoint."""

    def __init__(self, api_url: str, api_key: str, timeout: float = 30.0):
        self.api_url = api_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = max(1.0, min(float(timeout), 120.0))

    @classmethod
    def from_config(cls, config: dict) -> "CPAPushProvider":
        return cls(
            api_url=str(config.get("cpa_api_url") or ""),
            api_key=str(config.get("cpa_api_key") or ""),
            timeout=float(config.get("cpa_timeout") or 30),
        )

    def configuration_error(self) -> str:
        if not self.api_url:
            return "请先配置 CPA API URL"
        if not self.api_key:
            return "请先配置 CPA 管理 API 密钥"
        try:
            parsed = urlsplit(self.api_url)
        except ValueError:
            return "CPA API URL 无效"
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "CPA API URL 必须是有效的 HTTP(S) 地址"
        return ""

    @staticmethod
    def _filename(email: str) -> str:
        safe = "".join(char if char.isalnum() or char in {".", "@", "-", "_"} else "_" for char in email)
        return f"{safe or 'codex-account'}_codex.json"

    def push(self, payload: dict) -> PushResponse:
        configuration_error = self.configuration_error()
        if configuration_error:
            return PushResponse(ok=False, error=configuration_error)

        email = str(payload.get("email") or "").strip()
        if not email:
            return PushResponse(ok=False, error="CPA 授权文件缺少 email")
        if not payload.get("account_id"):
            return PushResponse(ok=False, error="CPA 授权文件缺少 account_id")

        url = f"{self.api_url}/v0/management/auth-files?name={quote(self._filename(email))}"
        try:
            response = httpx.post(
                url,
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            return PushResponse(ok=False, error=f"CPA 请求超时（{self.timeout:g} 秒）")
        except httpx.HTTPError as exc:
            return PushResponse(ok=False, error=f"CPA 连接失败：{type(exc).__name__}")

        if 200 <= response.status_code < 300:
            return PushResponse(ok=True, http_status=response.status_code)
        return PushResponse(
            ok=False,
            http_status=response.status_code,
            error=f"CPA 远端返回 HTTP {response.status_code}",
        )

    def test_connection(self) -> dict:
        configuration_error = self.configuration_error()
        if configuration_error:
            return {"ok": False, "error": configuration_error}
        try:
            response = httpx.options(
                f"{self.api_url}/v0/management/auth-files",
                headers={"authorization": f"Bearer {self.api_key}"},
                timeout=min(self.timeout, 10.0),
            )
        except httpx.TimeoutException:
            return {"ok": False, "error": "CPA 连接测试超时"}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"CPA 连接失败：{type(exc).__name__}"}

        if response.status_code in {200, 204, 405}:
            return {"ok": True, "message": "CPA 管理接口连接成功"}
        if response.status_code in {401, 403}:
            return {"ok": False, "error": "CPA 管理密钥无效或没有管理接口权限"}
        return {"ok": False, "error": f"CPA 管理接口返回 HTTP {response.status_code}"}
