from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests


@dataclass(slots=True)
class CustomerApiProblem(RuntimeError):
    status_code: int
    code: str
    message: str
    details: Any = None

    def __str__(self) -> str:
        return self.message or self.code or f"HTTP {self.status_code}"


def normalize_base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接口地址必须是有效的 HTTP/HTTPS URL")
    return text


class CustomerApiClient:
    """Small client for the compatible Customer API used by both vendors."""

    def __init__(self, base_url: str, cdk_key: str, *, timeout: tuple[int, int] = (10, 35)):
        self.base_url = normalize_base_url(base_url)
        self.cdk_key = str(cdk_key or "").strip()
        self.timeout = timeout
        if not self.cdk_key:
            raise ValueError("X-CDK-Key 未配置")

    def _url(self, path: str) -> str:
        target = urljoin(f"{self.base_url}/", str(path or "").lstrip("/"))
        base = urlsplit(self.base_url)
        resolved = urlsplit(target)

        def origin(value):
            default_port = 443 if value.scheme == "https" else 80
            return value.scheme.lower(), str(value.hostname or "").lower(), value.port or default_port

        if origin(resolved) != origin(base):
            raise ValueError("订单 pollUrl 必须与已配置接口地址同源")
        return target

    @staticmethod
    def _problem(response: requests.Response) -> CustomerApiProblem:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else {}
        if not isinstance(error, dict):
            error = {}
        code = str(error.get("code") or (payload.get("code") if isinstance(payload, dict) else "") or f"http_{response.status_code}")
        message = str(error.get("message") or (payload.get("message") if isinstance(payload, dict) else "") or response.text or f"HTTP {response.status_code}")
        return CustomerApiProblem(
            status_code=int(response.status_code),
            code=code,
            message=message[:1000],
            details=error.get("details"),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        customer_token: str = "",
        json: dict | None = None,
        use_cdk_header: bool = True,
    ) -> dict:
        headers = {"Accept": "application/json"}
        if customer_token:
            headers["Authorization"] = f"Bearer {customer_token}"
        elif use_cdk_header:
            headers["X-CDK-Key"] = self.cdk_key
        try:
            response = requests.request(
                method,
                self._url(path),
                headers=headers,
                json=json,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CustomerApiProblem(503, "network_error", f"连接接口失败: {exc}") from exc
        if 300 <= response.status_code < 400:
            raise CustomerApiProblem(response.status_code, "unexpected_redirect", "接口返回了未允许的重定向")
        if response.status_code >= 400:
            raise self._problem(response)
        try:
            payload = response.json()
        except Exception as exc:
            raise CustomerApiProblem(response.status_code, "invalid_json", "接口返回的不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise CustomerApiProblem(response.status_code, "invalid_response", "接口返回格式无效")
        return payload

    def test_connection(self) -> dict:
        return self._request("GET", "/api/v1/customer/orders?page=1&pageSize=1")

    def check_cdk(self) -> dict:
        payload = self._request(
            "POST",
            "/api/customer/cdk/check",
            json={"code": self.cdk_key},
            use_cdk_header=False,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else None
        if not data:
            raise CustomerApiProblem(502, "invalid_cdk_check_response", "CDK 校验接口返回格式无效")
        return data

    def create_extraction(self, access_token: str, *, payment_method: str = "kakao_pay") -> dict:
        return self._request(
            "POST",
            "/api/v1/customer/orders",
            json={
                "channel": "KAKAO_KK",
                "mode": "EXTRACT",
                "access_token": access_token,
                "payment_method": payment_method,
            },
        )

    def create_scanner(
        self,
        access_token: str,
        payment_url: str,
        *,
        payment_method: str = "kakao_pay",
        session_cookie: str = "",
    ) -> dict:
        credential = str(session_cookie or "").strip()
        auth_payload = {"session_cookie": credential} if credential else {"access_token": access_token}
        if not credential and not str(access_token or "").strip():
            raise ValueError("账号缺少 session_cookie 和 access_token")
        return self._request(
            "POST",
            "/api/v1/customer/orders",
            json={
                "channel": "KAKAO_KK",
                "mode": "READY_LINK",
                "payment_url": payment_url,
                "payment_method": payment_method,
                **auth_payload,
            },
        )

    def get_order(self, poll_url: str, customer_token: str) -> dict:
        if not str(poll_url or "").strip():
            raise ValueError("订单 pollUrl 为空")
        if not str(customer_token or "").strip():
            raise ValueError("订单 customerToken 为空")
        return self._request("GET", poll_url, customer_token=customer_token)
