"""5sim activation API client."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urljoin

import requests

from core.base_sms import BaseSmsProvider, SmsActivation, SmsStatus


DEFAULT_BASE_URL = "https://5sim.net"
DEFAULT_SERVICE = "openai"
DEFAULT_COUNTRY = "vietnam"


class FiveSimError(RuntimeError):
    pass


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


class FiveSimClient(BaseSmsProvider):
    provider_key = "fivesim"
    provider_label = "5sim"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        default_country: str = DEFAULT_COUNTRY,
        default_service: str = DEFAULT_SERVICE,
        operator: str = "any",
        max_price: str = "",
        request_timeout: float = 15,
        poll_interval: float = 5,
        proxy: str | None = None,
        session: requests.Session | None = None,
        log_fn=None,
    ):
        self.api_key = _text(api_key)
        self.base_url = _text(base_url).rstrip("/") or DEFAULT_BASE_URL
        self.default_country = _text(default_country).lower() or DEFAULT_COUNTRY
        self.default_service = _text(default_service).lower() or DEFAULT_SERVICE
        self.operator = _text(operator).lower() or "any"
        self.max_price = _text(max_price)
        self.request_timeout = max(float(request_timeout or 15), 0.5)
        self.poll_interval = max(float(poll_interval or 5), 0)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()
        self._log_fn = log_fn if callable(log_fn) else None

    @classmethod
    def from_config(cls, config: dict) -> "FiveSimClient":
        client = cls(
            api_key=config.get("fivesim_api_key", ""),
            base_url=config.get("fivesim_base_url", DEFAULT_BASE_URL),
            default_country=config.get("fivesim_default_country", DEFAULT_COUNTRY),
            default_service=config.get("fivesim_default_service", DEFAULT_SERVICE),
            operator=config.get("fivesim_operator", "any"),
            max_price=config.get("fivesim_max_price", ""),
            request_timeout=config.get("fivesim_request_timeout", 15),
            poll_interval=config.get("fivesim_poll_interval", 5),
            proxy=config.get("proxy") or config.get("sms_proxy") or None,
            log_fn=config.get("_log_fn"),
        )
        client.buy_max_attempts = max(int(float(config.get("fivesim_buy_max_attempts") or 20)), 1)
        client.buy_retry_interval = max(float(config.get("fivesim_buy_retry_interval") or 3), 0)
        client.otp_timeout_seconds = max(int(float(config.get("fivesim_otp_timeout_seconds") or 180)), 1)
        return client

    def configuration_error(self) -> str:
        return "" if self.api_key else "5sim API Token 未配置"

    def _redact(self, value: object) -> str:
        text = str(value or "")
        return text.replace(self.api_key, "***") if self.api_key else text

    def _request(self, path: str, *, params: dict[str, Any] | None = None, require_auth: bool = True) -> Any:
        if require_auth and not self.api_key:
            raise FiveSimError("5sim API Token 未配置")
        headers = {"Accept": "application/json", "User-Agent": "freeAgentIdentity/fivesim"}
        if require_auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self.session.get(
                urljoin(self.base_url + "/", path.lstrip("/")),
                params={key: value for key, value in dict(params or {}).items() if value not in (None, "")},
                headers=headers,
                proxies=self.proxy,
                timeout=self.request_timeout,
            )
            raw = str(response.text or "").strip()
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            details = locals().get("raw", "")
            suffix = f": {details}" if details else ""
            raise FiveSimError(self._redact(f"5sim 请求失败{suffix}: {exc}")) from exc
        if not raw:
            raise FiveSimError("5sim 返回空响应")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            if re.search(r"no\s+(free\s+)?phones|not\s+found", raw, re.I):
                raise FiveSimError(f"5sim 暂无号码 (NO_NUMBERS): {raw}")
            raise FiveSimError(f"5sim 返回无效 JSON: {self._redact(raw)}")
        if isinstance(payload, dict) and payload.get("error"):
            raise FiveSimError(f"5sim 请求失败: {_text(payload.get('error'))}")
        return payload

    def get_balance(self) -> float:
        payload = self._request("/v1/user/profile")
        value = payload.get("balance") if isinstance(payload, dict) else None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise FiveSimError(f"5sim 余额响应格式无效: {payload}") from exc

    def get_number(self, service: str = "", country: str = "", **options) -> SmsActivation:
        service = (_text(service) or self.default_service).lower()
        country = (_text(country) or self.default_country).lower()
        operator = (_text(options.get("operator")) or self.operator).lower()
        if not service or not country:
            raise FiveSimError("5sim service/country 未配置")
        path = "/v1/user/buy/activation/{}/{}/{}".format(
            quote(country, safe=""), quote(operator, safe=""), quote(service, safe="")
        )
        max_price = options.get("max_price", self.max_price)
        payload = self._request(path, params={"maxPrice": max_price})
        if not isinstance(payload, dict):
            raise FiveSimError(f"5sim 买号响应格式无效: {payload}")
        activation_id = _text(payload.get("id") or payload.get("activationId"))
        phone = _text(payload.get("phone") or payload.get("phoneNumber"))
        if not activation_id or not phone:
            raise FiveSimError(f"5sim 买号响应缺少 id/phone: {payload}")
        if self._log_fn:
            self._log_fn(f"5sim 买号成功：activationId={activation_id}")
        return SmsActivation(
            activation_id=activation_id,
            phone_number=phone,
            provider="fivesim",
            service=service,
            country=country,
            raw=json.dumps(payload, ensure_ascii=False),
            metadata=payload,
        )

    def get_status(self, activation_id: str) -> SmsStatus:
        activation_id = _text(activation_id)
        if not activation_id:
            raise FiveSimError("5sim activation_id 不能为空")
        payload = self._request(f"/v1/user/check/{quote(activation_id, safe='')}")
        if not isinstance(payload, dict):
            raise FiveSimError(f"5sim 查码响应格式无效: {payload}")
        raw = json.dumps(payload, ensure_ascii=False)
        for sms in reversed(payload.get("sms") or []):
            if not isinstance(sms, dict):
                continue
            source = _text(sms.get("code") or sms.get("text"))
            match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", source)
            if match:
                return SmsStatus(status="ok", code=match.group(1), raw=raw, metadata=payload)
        state = _text(payload.get("status")).upper()
        if state in {"CANCELED", "BANNED", "FINISHED", "TIMEOUT"}:
            return SmsStatus(status="cancelled", raw=raw, metadata=payload)
        return SmsStatus(status="waiting", raw=raw, metadata=payload)

    def set_status(self, activation_id: str, status: int | str) -> str:
        activation_id = _text(activation_id)
        value = str(status).strip()
        if value == "6":
            endpoint = "finish"
        elif value == "8":
            endpoint = "cancel"
        elif value in {"1", "3"}:
            return "ACKNOWLEDGED"
        else:
            raise FiveSimError(f"5sim 不支持状态码: {status}")
        payload = self._request(f"/v1/user/{endpoint}/{quote(activation_id, safe='')}")
        return json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload

    def list_country_options(self) -> list[dict[str, str]]:
        payload = self._request("/v1/guest/countries", require_auth=False)
        options = []
        if isinstance(payload, dict):
            for slug, item in payload.items():
                if not isinstance(item, dict):
                    continue
                value = _text(slug).lower()
                name = _text(item.get("text_en") or item.get("name")) or value
                iso_codes = list((item.get("iso") or {}).keys()) if isinstance(item.get("iso"), dict) else []
                region_code = _text(iso_codes[0] if iso_codes else "").upper()
                options.append({
                    "value": value,
                    "label": f"{name} ({value})",
                    "english_name": name,
                    "localized_name": "",
                    "region_code": region_code,
                    "dial_code": "",
                })
        elif isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                value = _text(item.get("id") or item.get("slug")).lower()
                if value:
                    name = _text(item.get("text_en") or item.get("name")) or value
                    region_code = _text(item.get("iso") or item.get("country_code")).upper()
                    options.append({
                        "value": value,
                        "label": f"{name} ({value})",
                        "english_name": name,
                        "localized_name": "",
                        "region_code": region_code if len(region_code) == 2 else "",
                        "dial_code": "",
                    })
        return sorted(options, key=lambda item: item["label"].casefold())

    def list_service_options(self) -> list[dict[str, str]]:
        return [{"value": DEFAULT_SERVICE, "label": "OpenAI / ChatGPT (openai)"}]
