"""HeroSMS client using its SMS-Activate-compatible API."""
from __future__ import annotations

import json

from core.base_sms import SmsActivation
from core.smsbower_sms import SMSBowerClient, SMSBowerError, _string


DEFAULT_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"


class HeroSMSError(SMSBowerError):
    pass


class HeroSMSClient(SMSBowerClient):
    provider_key = "herosms"
    provider_label = "HeroSMS"
    lock_ref = False

    @classmethod
    def from_config(cls, config: dict) -> "HeroSMSClient":
        client = cls(
            api_key=config.get("herosms_api_key", ""),
            base_url=config.get("herosms_base_url", DEFAULT_BASE_URL),
            default_service=config.get("herosms_default_service", "dr"),
            default_country=config.get("herosms_default_country", ""),
            default_max_price=config.get("herosms_max_price", ""),
            default_phone_exception=config.get("herosms_phone_exception", ""),
            default_ref=config.get("herosms_ref", ""),
            number_api=config.get("herosms_number_api", "getNumberV2"),
            request_timeout=config.get("herosms_request_timeout", 15),
            request_max_attempts=config.get("herosms_request_max_attempts", 4),
            request_retry_delay=config.get("herosms_request_retry_delay", 1),
            request_retry_max_delay=config.get("herosms_request_retry_max_delay", 8),
            poll_interval=config.get("herosms_poll_interval", 5),
            proxy=config.get("proxy") or config.get("sms_proxy") or None,
            log_fn=config.get("_log_fn"),
        )
        client.buy_max_attempts = int(float(config.get("herosms_buy_max_attempts") or 20))
        client.buy_retry_interval = float(config.get("herosms_buy_retry_interval") or 3)
        client.otp_timeout_seconds = int(float(config.get("herosms_otp_timeout_seconds") or 120))
        client.default_operator = _string(config.get("herosms_operator", ""))
        client.default_fixed_price = _string(config.get("herosms_fixed_price", ""))
        return client

    def configuration_error(self) -> str:
        if not self.api_key:
            return "HeroSMS API Key 未配置"
        return ""

    def _number_params(self, service: str, country: str, options: dict) -> dict:
        params = super()._number_params(service, country, options)
        operator = options.get("operator", getattr(self, "default_operator", ""))
        fixed_price = options.get("fixed_price", options.get("fixedPrice", getattr(self, "default_fixed_price", "")))
        if operator not in (None, ""):
            params["operator"] = operator
        if fixed_price not in (None, ""):
            params["fixedPrice"] = "true" if str(fixed_price).strip().lower() in {"1", "true", "yes", "on"} else "false"
        return params

    @staticmethod
    def _activation_from_text(raw: str, *, service: str = "", country: str = "") -> SmsActivation:
        parts = raw.split(":", 2)
        if len(parts) != 3 or parts[0] != "ACCESS_NUMBER":
            raise HeroSMSError(f"HeroSMS 买号响应格式无效: {raw}")
        return SmsActivation(
            activation_id=parts[1].strip(),
            phone_number=parts[2].strip(),
            provider="herosms",
            service=service,
            country=country,
            raw=raw,
        )

    @staticmethod
    def _activation_from_json(raw: str, *, service: str = "", country: str = "") -> SmsActivation:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HeroSMSError(f"HeroSMS JSON 买号响应格式无效: {raw}") from exc
        if not isinstance(payload, dict):
            raise HeroSMSError("HeroSMS JSON 买号响应不是对象")
        activation_id = _string(payload.get("activationId") or payload.get("id"))
        phone_number = _string(payload.get("phoneNumber") or payload.get("number"))
        if not activation_id or not phone_number:
            raise HeroSMSError(f"HeroSMS JSON 买号响应缺少 activationId/phoneNumber: {raw}")
        return SmsActivation(
            activation_id=activation_id,
            phone_number=phone_number,
            provider="herosms",
            service=service,
            country=_string(payload.get("countryCode")) or country,
            raw=raw,
            metadata=payload,
        )
