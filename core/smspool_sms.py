"""SMSPool REST API client."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

import requests

from core.base_sms import BaseSmsProvider, SmsActivation, SmsStatus


DEFAULT_BASE_URL = "https://api.smspool.net"
DEFAULT_SERVICE = "671"


class SMSPoolError(RuntimeError):
    pass


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


class SMSPoolClient(BaseSmsProvider):
    provider_key = "smspool"
    provider_label = "SMSPool"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        default_country: str = "9",
        default_service: str = DEFAULT_SERVICE,
        max_price: str = "",
        pricing_option: str = "0",
        request_timeout: float = 15,
        poll_interval: float = 5,
        proxy: str | None = None,
        session: requests.Session | None = None,
        log_fn=None,
    ):
        self.api_key = _text(api_key)
        self.base_url = _text(base_url).rstrip("/") or DEFAULT_BASE_URL
        self.default_country = _text(default_country)
        self.default_service = _text(default_service) or DEFAULT_SERVICE
        self.max_price = _text(max_price)
        self.pricing_option = _text(pricing_option) or "0"
        self.request_timeout = max(float(request_timeout or 15), 0.5)
        self.poll_interval = max(float(poll_interval or 5), 0)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()
        self._log_fn = log_fn if callable(log_fn) else None

    @classmethod
    def from_config(cls, config: dict) -> "SMSPoolClient":
        client = cls(
            api_key=config.get("smspool_api_key", ""),
            base_url=config.get("smspool_base_url", DEFAULT_BASE_URL),
            default_country=config.get("smspool_default_country", "9"),
            default_service=config.get("smspool_default_service", DEFAULT_SERVICE),
            max_price=config.get("smspool_max_price", ""),
            pricing_option=config.get("smspool_pricing_option", "0"),
            request_timeout=config.get("smspool_request_timeout", 15),
            poll_interval=config.get("smspool_poll_interval", 5),
            proxy=config.get("proxy") or config.get("sms_proxy") or None,
            log_fn=config.get("_log_fn"),
        )
        client.buy_max_attempts = max(int(float(config.get("smspool_buy_max_attempts") or 20)), 1)
        client.buy_retry_interval = max(float(config.get("smspool_buy_retry_interval") or 3), 0)
        client.otp_timeout_seconds = max(int(float(config.get("smspool_otp_timeout_seconds") or 120)), 1)
        return client

    def configuration_error(self) -> str:
        return "" if self.api_key else "SMSPool API Key 未配置"

    def _redact(self, value: object) -> str:
        text = str(value or "")
        if self.api_key:
            text = text.replace(self.api_key, "***")
        return re.sub(r"([?&]key=)[^&\s]+", r"\1***", text, flags=re.I)

    def _request(self, path: str, params: dict[str, Any] | None = None, *, require_key: bool = True) -> Any:
        if require_key and not self.api_key:
            raise SMSPoolError("SMSPool API Key 未配置")
        query = dict(params or {})
        if require_key:
            query.setdefault("key", self.api_key)
        try:
            response = self.session.get(
                urljoin(self.base_url + "/", path.lstrip("/")),
                params=query,
                headers={"Accept": "application/json", "User-Agent": "freeAgentIdentity/smspool"},
                proxies=self.proxy,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            raw = str(response.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise SMSPoolError(self._redact(f"SMSPool 请求失败: {exc}")) from exc
        if not raw:
            raise SMSPoolError("SMSPool 返回空响应")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SMSPoolError(f"SMSPool 返回无效 JSON: {self._redact(raw)}") from exc
        if isinstance(payload, dict) and str(payload.get("success", "1")).lower() in {"0", "false"}:
            message = _text(payload.get("message") or payload.get("error") or payload.get("status")) or "unknown"
            raise SMSPoolError(f"SMSPool 请求失败: {message}")
        return payload

    def get_balance(self) -> float:
        payload = self._request("/request/balance")
        value = payload.get("balance") if isinstance(payload, dict) else payload
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise SMSPoolError(f"SMSPool 余额响应格式无效: {payload}") from exc

    def get_number(self, service: str = "", country: str = "", **options) -> SmsActivation:
        service = _text(service) or self.default_service
        country = _text(country) or self.default_country
        if not service or not country:
            raise SMSPoolError("SMSPool service/country 未配置")
        params = {
            "country": country,
            "service": service,
            "max_price": options.get("max_price", self.max_price),
            "pricing_option": options.get("pricing_option", self.pricing_option),
        }
        payload = self._request("/purchase/sms", params)
        if not isinstance(payload, dict):
            raise SMSPoolError(f"SMSPool 买号响应格式无效: {payload}")
        activation_id = _text(payload.get("orderid") or payload.get("order_id") or payload.get("id"))
        phone = _text(payload.get("phonenumber") or payload.get("phone") or payload.get("number"))
        if not activation_id or not phone:
            raise SMSPoolError(f"SMSPool 买号响应缺少 orderid/phonenumber: {payload}")
        if self._log_fn:
            self._log_fn(f"SMSPool 买号成功：orderId={activation_id}")
        return SmsActivation(
            activation_id=activation_id,
            phone_number=phone,
            provider="smspool",
            service=service,
            country=country,
            raw=json.dumps(payload, ensure_ascii=False),
            metadata=payload,
        )

    def get_status(self, activation_id: str) -> SmsStatus:
        activation_id = _text(activation_id)
        if not activation_id:
            raise SMSPoolError("SMSPool orderid 不能为空")
        payload = self._request("/sms/check", {"orderid": activation_id})
        if not isinstance(payload, dict):
            raise SMSPoolError(f"SMSPool 查码响应格式无效: {payload}")
        code = _text(payload.get("sms") or payload.get("code") or payload.get("otp"))
        raw = json.dumps(payload, ensure_ascii=False)
        if code and code.lower() not in {"null", "none", "pending"}:
            match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", code)
            return SmsStatus(status="ok", code=match.group(1) if match else code, raw=raw, metadata=payload)
        state = _text(payload.get("status") or payload.get("message")).lower()
        if any(word in state for word in ("cancel", "refund", "expired")):
            return SmsStatus(status="cancelled", raw=raw, metadata=payload)
        return SmsStatus(status="waiting", raw=raw, metadata=payload)

    def set_status(self, activation_id: str, status: int | str) -> str:
        value = str(status).strip()
        if value == "8":
            payload = self._request("/sms/cancel", {"orderid": _text(activation_id)})
            return json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        if value in {"1", "3", "6"}:
            return "ACKNOWLEDGED"
        raise SMSPoolError(f"SMSPool 不支持状态码: {status}")

    def list_country_options(self) -> list[dict[str, str]]:
        payload = self._request("/country/retrieve_all", require_key=False)
        items = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
        options = []
        for item in items:
            if not isinstance(item, dict):
                continue
            value = _text(item.get("ID") or item.get("id"))
            if not value:
                continue
            name = _text(item.get("name")) or value
            short = _text(item.get("short_name") or item.get("cc")).upper()
            dial_code = _text(item.get("cc"))
            if dial_code and not dial_code.startswith("+"):
                dial_code = f"+{dial_code}"
            options.append({
                "value": value,
                "label": f"{name} ({short or value})",
                "english_name": name,
                "localized_name": "",
                "region_code": short if len(short) == 2 else "",
                "dial_code": dial_code,
            })
        return sorted(options, key=lambda item: item["label"].casefold())

    def list_service_options(self) -> list[dict[str, str]]:
        payload = self._request("/service/retrieve_all", require_key=False)
        items = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
        options = []
        for item in items:
            if not isinstance(item, dict):
                continue
            value = _text(item.get("ID") or item.get("id"))
            if value:
                options.append({"value": value, "label": f"{_text(item.get('name')) or value} ({value})"})
        return sorted(options, key=lambda item: item["label"].casefold())
