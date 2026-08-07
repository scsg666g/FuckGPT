"""SMSBower 短信接码服务封装。

文档：https://smsbower.app/cn/api?page=client
API 兼容 sms-activate 风格的 ``handler_api.php``。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from core.base_sms import BaseSmsProvider, SmsActivation, SmsStatus
from core.network_retry import retry_network_call


DEFAULT_BASE_URL = "https://smsbower.page/stubs/handler_api.php"
DEFAULT_REF = "531498"

SMSBOWER_STATUS_SMS_SENT = 1
SMSBOWER_STATUS_RETRY = 3
SMSBOWER_STATUS_FINISH = 6
SMSBOWER_STATUS_CANCEL = 8

_ERROR_CODES = {
    "BAD_KEY": "API Key 无效",
    "BAD_ACTION": "接口 action 无效",
    "NO_ACTIVATION": "激活单不存在",
    "NO_BALANCE": "余额不足",
    "NO_NUMBERS": "当前服务/国家暂无号码",
    "BAD_SERVICE": "服务代码无效",
    "BAD_STATUS": "状态码无效",
    "BANNED": "账号受限",
}


class SMSBowerError(RuntimeError):
    pass


def _string(value: object) -> str:
    return "" if value is None else str(value).strip()


def _float_value(value: object, default: float, *, minimum: float = 0.0) -> float:
    try:
        result = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


def _int_value(value: object, default: int, *, minimum: int = 0) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


def _first_string(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


class SMSBowerClient(BaseSmsProvider):
    """SMSBower API 客户端。

    核心能力：

    * ``get_number`` / ``get_number_v2``：购买手机号；
    * ``get_status``：取短信验证码；
    * ``set_status`` 及便捷方法：取消、完成、重试等状态流转。
    """

    provider_key = "smsbower"
    provider_label = "SMSBower"
    lock_ref = True

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        default_service: str = "",
        default_country: str | int = "",
        default_max_price: str | float = "",
        default_min_price: str | float = "",
        default_provider_ids: str = "",
        default_except_provider_ids: str = "",
        default_phone_exception: str = "",
        default_user_id: str = "",
        default_ref: str = DEFAULT_REF,
        number_api: str = "getNumber",
        request_timeout: float | str = 15,
        request_max_attempts: int | str = 4,
        request_retry_delay: float | str = 1,
        request_retry_max_delay: float | str = 8,
        poll_interval: float | str = 3,
        proxy: str | None = None,
        session: requests.Session | None = None,
        log_fn=None,
    ):
        self.api_key = _string(api_key)
        self.base_url = _string(base_url) or DEFAULT_BASE_URL
        self.default_service = _string(default_service)
        self.default_country = _string(default_country)
        self.default_max_price = _string(default_max_price)
        self.default_min_price = _string(default_min_price)
        self.default_provider_ids = _string(default_provider_ids)
        self.default_except_provider_ids = _string(default_except_provider_ids)
        self.default_phone_exception = _string(default_phone_exception)
        self.default_user_id = _string(default_user_id)
        self.default_ref = DEFAULT_REF if self.lock_ref else _string(default_ref)
        self.number_api = "getNumberV2" if _string(number_api).lower() in {"getnumberv2", "v2", "2"} else "getNumber"
        self.request_timeout = _float_value(request_timeout, 15, minimum=0.5)
        self.request_max_attempts = _int_value(request_max_attempts, 4, minimum=1)
        self.request_retry_delay = _float_value(request_retry_delay, 1, minimum=0)
        self.request_retry_max_delay = _float_value(request_retry_max_delay, 8, minimum=0)
        self.poll_interval = _float_value(poll_interval, 3, minimum=0)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()
        self._log_fn = log_fn if callable(log_fn) else None

    @classmethod
    def from_config(cls, config: dict) -> "SMSBowerClient":
        client = cls(
            api_key=config.get("smsbower_api_key", ""),
            base_url=config.get("smsbower_base_url", DEFAULT_BASE_URL),
            default_service=config.get("smsbower_default_service", ""),
            default_country=config.get("smsbower_default_country", ""),
            default_max_price=config.get("smsbower_max_price", ""),
            default_min_price=config.get("smsbower_min_price", ""),
            default_provider_ids=config.get("smsbower_provider_ids", ""),
            default_except_provider_ids=config.get("smsbower_except_provider_ids", ""),
            default_phone_exception=config.get("smsbower_phone_exception", ""),
            default_user_id=config.get("smsbower_user_id", ""),
            default_ref=DEFAULT_REF,
            number_api=config.get("smsbower_number_api", "getNumber"),
            request_timeout=config.get("smsbower_request_timeout", 15),
            request_max_attempts=config.get("smsbower_request_max_attempts", 4),
            request_retry_delay=config.get("smsbower_request_retry_delay", 1),
            request_retry_max_delay=config.get("smsbower_request_retry_max_delay", 8),
            poll_interval=config.get("smsbower_poll_interval", 3),
            proxy=config.get("proxy") or config.get("sms_proxy") or None,
            log_fn=config.get("_log_fn"),
        )
        client.buy_max_attempts = _int_value(config.get("smsbower_buy_max_attempts"), 20, minimum=1)
        client.buy_retry_interval = _float_value(config.get("smsbower_buy_retry_interval"), 3, minimum=0)
        client.otp_timeout_seconds = _int_value(config.get("smsbower_otp_timeout_seconds"), 120, minimum=1)
        return client

    def configuration_error(self) -> str:
        if not self.api_key:
            return "SMSBower API Key 未配置"
        return ""

    def set_logger(self, log_fn) -> None:
        self._log_fn = log_fn if callable(log_fn) else None

    def _log(self, message: str) -> None:
        if not self._log_fn:
            return
        try:
            self._log_fn(self._redact(message))
        except Exception:
            pass

    def _redact(self, text: object) -> str:
        value = str(text or "")
        if self.api_key:
            value = value.replace(self.api_key, "***")
        value = re.sub(r"(api_key=)[^&\s]+", r"\1***", value, flags=re.IGNORECASE)
        return value

    def _raise_api_error(self, raw: str, *, action: str) -> None:
        code = raw.split(":", 1)[0].strip()
        message = _ERROR_CODES.get(code, raw or "unknown")
        raise SMSBowerError(f"SMSBower {action}失败: {message} ({code})")

    def _request(self, action: str, params: dict[str, Any] | None = None) -> str:
        if not self.api_key:
            raise SMSBowerError("SMSBower API Key 未配置")
        query = {"api_key": self.api_key, "action": action}
        for key, value in dict(params or {}).items():
            if value not in (None, ""):
                query[key] = value
        def perform_request() -> str:
            response = self.session.get(
                self.base_url,
                params=query,
                headers={"Accept": "application/json, text/plain, */*", "User-Agent": "freeAgentIdentity/smsbower"},
                proxies=self.proxy,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            return str(response.text or "").strip()

        try:
            raw = retry_network_call(
                perform_request,
                max_attempts=self.request_max_attempts,
                base_delay=self.request_retry_delay,
                max_delay=self.request_retry_max_delay,
                on_retry=lambda attempt, attempts, delay, exc: self._log(
                    f"SMSBower {action} 遇到瞬时网络/代理异常，{delay:g}s 后自动重试 "
                    f"({attempt}/{attempts}): {exc}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise SMSBowerError(self._redact(f"SMSBower 请求失败: {exc}")) from exc
        if not raw:
            raise SMSBowerError(f"SMSBower {action} 返回空响应")
        if raw.split(":", 1)[0].strip() in _ERROR_CODES:
            self._raise_api_error(raw, action=action)
        return raw

    @staticmethod
    def _activation_from_text(raw: str, *, service: str = "", country: str = "") -> SmsActivation:
        parts = raw.split(":", 2)
        if len(parts) != 3 or parts[0] != "ACCESS_NUMBER":
            raise SMSBowerError(f"SMSBower 买号响应格式无效: {raw}")
        return SmsActivation(
            activation_id=parts[1].strip(),
            phone_number=parts[2].strip(),
            provider="smsbower",
            service=service,
            country=country,
            raw=raw,
        )

    @staticmethod
    def _activation_from_json(raw: str, *, service: str = "", country: str = "") -> SmsActivation:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SMSBowerError(f"SMSBower JSON 买号响应格式无效: {raw}") from exc
        if not isinstance(payload, dict):
            raise SMSBowerError("SMSBower JSON 买号响应不是对象")
        activation_id = _string(payload.get("activationId") or payload.get("id"))
        phone_number = _string(payload.get("phoneNumber") or payload.get("number"))
        if not activation_id or not phone_number:
            raise SMSBowerError(f"SMSBower JSON 买号响应缺少 activationId/phoneNumber: {raw}")
        return SmsActivation(
            activation_id=activation_id,
            phone_number=phone_number,
            provider="smsbower",
            service=service,
            country=_string(payload.get("countryCode")) or country,
            raw=raw,
            metadata=payload,
        )

    @staticmethod
    def _status_from_text(raw: str) -> SmsStatus:
        if raw == "STATUS_WAIT_CODE":
            return SmsStatus(status="waiting", raw=raw)
        if raw == "STATUS_CANCEL":
            return SmsStatus(status="cancelled", raw=raw)
        if raw.startswith("STATUS_WAIT_RETRY:"):
            return SmsStatus(status="waiting_retry", last_code=raw.split(":", 1)[1].strip(), raw=raw)
        if raw.startswith("STATUS_OK:"):
            return SmsStatus(status="ok", code=raw.split(":", 1)[1].strip(), raw=raw)
        raise SMSBowerError(f"SMSBower 查码响应格式无效: {raw}")

    @staticmethod
    def _set_status_result(raw: str) -> str:
        ok = {"ACCESS_READY", "ACCESS_RETRY_GET", "ACCESS_ACTIVATION", "ACCESS_CANCEL"}
        if raw in ok:
            return raw
        if raw.split(":", 1)[0] in _ERROR_CODES:
            raise SMSBowerError(f"SMSBower 改状态失败: {_ERROR_CODES.get(raw, raw)} ({raw})")
        raise SMSBowerError(f"SMSBower 改状态响应格式无效: {raw}")

    def get_balance(self) -> float:
        raw = self._request("getBalance")
        value = raw.split(":", 1)[1] if raw.startswith("ACCESS_BALANCE:") else raw
        try:
            return float(value)
        except ValueError as exc:
            raise SMSBowerError(f"SMSBower 余额响应格式无效: {raw}") from exc

    def get_number(
        self,
        service: str = "",
        country: str = "",
        **options,
    ) -> SmsActivation:
        if self.number_api == "getNumberV2":
            return self.get_number_v2(service=service, country=country, **options)
        service = (_string(service) or self.default_service).lower()
        country = _string(country) or self.default_country
        if not service:
            raise SMSBowerError("SMSBower service 未配置")
        if not country:
            raise SMSBowerError("SMSBower country 未配置")

        params = self._number_params(service, country, options)
        raw = self._request("getNumber", params)
        activation = self._activation_from_text(raw, service=service, country=country)
        self._log(
            f"SMSBower 买号成功：activationId={activation.activation_id}, phone={activation.phone_number}"
        )
        return activation

    def get_number_v2(
        self,
        service: str = "",
        country: str = "",
        **options,
    ) -> SmsActivation:
        service = (_string(service) or self.default_service).lower()
        country = _string(country) or self.default_country
        if not service:
            raise SMSBowerError("SMSBower service 未配置")
        if not country:
            raise SMSBowerError("SMSBower country 未配置")

        params = self._number_params(service, country, options)
        raw = self._request("getNumberV2", params)
        if raw.startswith("{"):
            activation = self._activation_from_json(raw, service=service, country=country)
        else:
            activation = self._activation_from_text(raw, service=service, country=country)
        self._log(
            f"SMSBower 买号成功：activationId={activation.activation_id}, phone={activation.phone_number}"
        )
        return activation

    def _number_params(self, service: str, country: str, options: dict[str, Any]) -> dict[str, Any]:
        option_map = {
            "max_price": "maxPrice",
            "min_price": "minPrice",
            "provider_ids": "providerIds",
            "except_provider_ids": "exceptProviderIds",
            "phone_exception": "phoneException",
            "user_id": "userID",
        }
        params: dict[str, Any] = {"service": service, "country": country}
        if self.default_max_price:
            params["maxPrice"] = self.default_max_price
        if self.default_min_price:
            params["minPrice"] = self.default_min_price
        configured_options = {
            "providerIds": self.default_provider_ids,
            "exceptProviderIds": self.default_except_provider_ids,
            "phoneException": self.default_phone_exception,
            "userID": self.default_user_id,
            "ref": self.default_ref,
        }
        params.update({key: value for key, value in configured_options.items() if value})
        if not self.lock_ref:
            option_map["ref"] = "ref"
        for source_key, target_key in option_map.items():
            value = options.get(source_key, options.get(target_key))
            if value not in (None, ""):
                params[target_key] = value
        return params

    def get_status(self, activation_id: str) -> SmsStatus:
        activation_id = _string(activation_id)
        if not activation_id:
            raise SMSBowerError("SMSBower activation_id 不能为空")
        raw = self._request("getStatus", {"id": activation_id})
        status = self._status_from_text(raw)
        if status.code:
            self._log(f"SMSBower 收到验证码：activationId={activation_id}")
        return status

    def set_status(self, activation_id: str, status: int | str) -> str:
        activation_id = _string(activation_id)
        status_value = _int_value(status, -1, minimum=-1)
        if not activation_id:
            raise SMSBowerError("SMSBower activation_id 不能为空")
        if status_value < 0:
            raise SMSBowerError("SMSBower status 不能为空")
        raw = self._request("setStatus", {"id": activation_id, "status": status_value})
        result = self._set_status_result(raw)
        self._log(f"SMSBower 改状态成功：activationId={activation_id}, status={status_value}, result={result}")
        return result

    def wait_for_code(
        self,
        activation_id: str,
        *,
        timeout: int | float = 120,
        poll_interval: int | float | None = None,
    ) -> str:
        interval = self.poll_interval if poll_interval is None else poll_interval
        self._log(f"SMSBower 开始等待短信验证码：activationId={activation_id}")
        code = super().wait_for_code(activation_id, timeout=timeout, poll_interval=interval)
        self._log(f"SMSBower 等待验证码完成：activationId={activation_id}")
        return code

    def get_prices(self, service: str = "", country: str = "") -> str:
        params = {}
        if service:
            params["service"] = service
        if country:
            params["country"] = country
        return self._request("getPrices", params)

    def get_services_list(self) -> str:
        return self._request("getServicesList")

    def get_countries(self) -> str:
        return self._request("getCountries")

    def get_top_countries_by_service(self, service: str = "") -> str:
        service = _string(service) or self.default_service
        if not service:
            raise SMSBowerError("SMSBower service 未配置")
        return self._request("getTopCountriesByService", {"service": service})

    @staticmethod
    def _catalog_items(raw: str, *keys: str) -> list[dict[str, Any]]:
        """Normalize the catalog response variants used by SMSBower."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SMSBowerError("SMSBower 目录响应不是有效 JSON") from exc
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in (*keys, "data", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return [item for item in value.values() if isinstance(item, dict)]
        if payload and all(isinstance(item, dict) for item in payload.values()):
            return list(payload.values())
        return []

    def list_country_options(self) -> list[dict[str, str]]:
        items = self._catalog_items(self.get_countries(), "countries")
        options: list[dict[str, str]] = []
        for item in items:
            value = _first_string(item, "activate_org_code", "country", "id")
            if not value:
                continue
            title = _string(item.get("title") or item.get("name") or item.get("eng")) or value
            iso = _string(item.get("iso") or item.get("short_name") or item.get("country_code")).upper()
            prefix = _string(item.get("prefix"))
            details = " · ".join(part for part in (iso, f"+{prefix}" if prefix else "") if part)
            options.append({
                "value": value,
                "label": f"{title} ({details or value})",
                "english_name": title,
                "localized_name": _string(item.get("chn")),
                "region_code": iso,
                "dial_code": f"+{prefix}" if prefix else "",
            })
        return sorted(options, key=lambda item: item["label"].casefold())

    def list_service_options(self) -> list[dict[str, str]]:
        items = self._catalog_items(self.get_services_list(), "services")
        options: list[dict[str, str]] = []
        for item in items:
            if str(item.get("is_active", "1")).lower() in {"0", "false", "no"}:
                continue
            value = _first_string(item, "activate_org_code", "service", "code")
            if not value:
                continue
            title = _string(item.get("title") or item.get("name")) or value
            options.append({"value": value, "label": f"{title} ({value})"})
        return sorted(options, key=lambda item: item["label"].casefold())
