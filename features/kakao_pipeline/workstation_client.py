from __future__ import annotations

from urllib.parse import quote, urljoin, urlsplit

import requests

from .client import CustomerApiProblem, normalize_base_url


class WorkstationScannerClient:
    """Client for kakao.546789.shop payment-submission workstations."""

    def __init__(self, base_url: str, cdk_key: str = "", *, timeout: tuple[int, int] = (10, 35)):
        self.base_url = normalize_base_url(base_url)
        self.cdk_key = str(cdk_key or "").strip()
        self.timeout = timeout

    def _url(self, path: str) -> str:
        target = urljoin(f"{self.base_url}/", str(path or "").lstrip("/"))
        base = urlsplit(self.base_url)
        resolved = urlsplit(target)
        if (resolved.scheme, resolved.hostname, resolved.port) != (base.scheme, base.hostname, base.port):
            raise ValueError("扫码接口路径必须与已配置接口地址同源")
        return target

    def _request(self, method: str, path: str, *, json: dict | None = None) -> dict:
        try:
            response = requests.request(
                method,
                self._url(path),
                headers={"Accept": "application/json"},
                json=json,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CustomerApiProblem(503, "network_error", f"连接 546789 扫码接口失败: {exc}") from exc
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if 300 <= response.status_code < 400:
            raise CustomerApiProblem(response.status_code, "unexpected_redirect", "546789 接口返回了未允许的重定向")
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else ""
            message = str(detail or (payload.get("message") if isinstance(payload, dict) else "") or response.text or f"HTTP {response.status_code}")
            code = str((payload.get("code") if isinstance(payload, dict) else "") or f"http_{response.status_code}")
            raise CustomerApiProblem(response.status_code, code, message[:1000])
        if not isinstance(payload, dict):
            raise CustomerApiProblem(response.status_code, "invalid_response", "546789 接口返回格式无效")
        return payload

    def test_connection(self) -> dict:
        return self._request("GET", "/api/health")

    def check_cdk(self) -> dict:
        if not self.cdk_key:
            raise ValueError("546789 CDK 未配置")
        return self._request("GET", f"/api/payment-cdk/quota/{quote(self.cdk_key, safe='')}")

    def submit_payment(self, payment_url: str) -> dict:
        if not self.cdk_key:
            raise ValueError("546789 CDK 未配置")
        return self._request(
            "POST",
            "/api/payment-submissions/batch",
            json={"payment_urls": [payment_url], "submit_cdk": self.cdk_key},
        )

    def get_submission(self, submission_id: str) -> dict:
        if not str(submission_id or "").strip():
            raise ValueError("546789 submission ID 为空")
        payload = self._request(
            "POST",
            "/api/payment-submissions/status",
            json={"ids": [str(submission_id)]},
        )
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        item = next((value for value in items if isinstance(value, dict) and str(value.get("id")) == str(submission_id)), None)
        if item is None:
            raise CustomerApiProblem(502, "submission_missing", "546789 状态响应缺少对应任务")
        return {"ok": bool(payload.get("ok", True)), "data": item}

    def qr_url(self, submission_id: str) -> str:
        return self._url(f"/api/payment-submissions/{quote(str(submission_id), safe='')}/qr.png")
