from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.provider_settings import ProviderSettingsService

router = APIRouter(prefix="/provider-settings", tags=["provider-settings"])
service = ProviderSettingsService()


class ProviderSettingUpsertRequest(BaseModel):
    id: int | None = None
    provider_type: str
    provider_key: str
    display_name: str = ""
    auth_mode: str = ""
    enabled: bool = True
    is_default: bool = False
    config: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, str] = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


@router.get("")
def list_provider_settings(provider_type: str):
    return service.list_settings(provider_type)


@router.put("")
def save_provider_setting(body: ProviderSettingUpsertRequest):
    try:
        return service.save_setting(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("")
def create_provider_setting(body: ProviderSettingUpsertRequest):
    try:
        return service.save_setting(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/{setting_id}")
def delete_provider_setting(setting_id: int):
    result = service.delete_setting(setting_id)
    if not result["ok"]:
        raise HTTPException(404, "provider setting 不存在")
    return result


class ProviderTestRequest(BaseModel):
    provider_type: str
    provider_key: str
    config: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, str] = Field(default_factory=dict)


class ProviderOptionsRequest(ProviderTestRequest):
    field_key: str


@router.post("/options")
def list_provider_options(body: ProviderOptionsRequest):
    """Load provider-owned select options using the credentials in the edit form."""
    if body.provider_type != "sms":
        return {"ok": False, "error": "当前 provider 不支持动态选项", "options": []}

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from providers.registry import create_provider, load_all

    definition = ProviderDefinitionsRepository().get_by_key("sms", body.provider_key)
    if not definition:
        return {"ok": False, "error": f"未找到 provider 定义: {body.provider_key}", "options": []}
    try:
        load_all()
        client = create_provider("sms", definition.driver_type or body.provider_key, {**body.config, **body.auth})
        if body.field_key.endswith("_default_service"):
            options = client.list_service_options()
        elif body.field_key.endswith("_default_country") or body.field_key == "smstome_country_slugs":
            options = client.list_country_options()
        else:
            return {"ok": False, "error": f"不支持的选项字段: {body.field_key}", "options": []}
        return {"ok": True, "options": options}
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if body.provider_key == "smsbower" and "API Key 未配置" in message:
            return {"ok": False, "error": "请先填写 SMSBower API Key", "options": []}
        return {"ok": False, "error": f"{definition.label} 选项加载失败: {message}", "options": []}


@router.post("/test")
def test_provider(body: ProviderTestRequest):
    """测试 provider 配置是否正确。"""
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    definitions = ProviderDefinitionsRepository()
    definition = definitions.get_by_key(body.provider_type, body.provider_key)
    if not definition:
        return {"ok": False, "error": f"未找到 provider 定义: {body.provider_key}"}

    # Merge config + auth into a flat dict (same as runtime)
    extra = {**body.config, **body.auth}

    if body.provider_type == "mailbox":
        return _test_mailbox(definition.driver_type or body.provider_key, extra, definition)
    elif body.provider_type == "captcha":
        return {"ok": True, "message": "验证码服务暂不支持在线测试，请在注册任务中验证"}
    elif body.provider_type == "sms":
        return _test_sms(definition.driver_type or body.provider_key, extra, definition.label)
    elif body.provider_type == "proxy":
        return _test_proxy(definition.driver_type or body.provider_key, extra)
    elif body.provider_type == "push":
        return _test_push(definition.driver_type or body.provider_key, extra, definition.label)
    return {"ok": False, "error": f"不支持测试的 provider 类型: {body.provider_type}"}


def _test_mailbox(driver_type: str, extra: dict, definition) -> dict:
    """尝试用给定配置创建一个邮箱，验证配置是否正确。"""
    from core.base_mailbox import MAILBOX_FACTORY_REGISTRY

    factory = MAILBOX_FACTORY_REGISTRY.get(driver_type)
    if not factory:
        return {"ok": False, "error": f"未找到邮箱驱动: {driver_type}"}

    try:
        mailbox = factory(extra, None)

        if hasattr(mailbox, "peek_email"):
            email = mailbox.peek_email()
            return {
                "ok": True,
                "message": f"测试成功！可用邮箱: {email}",
                "email": email,
            }

        account = mailbox.get_email()
        return {
            "ok": True,
            "message": f"测试成功！生成邮箱: {account.email}",
            "email": account.email,
        }
    except Exception as exc:
        message = str(exc)
        for key, value in extra.items():
            key_lower = str(key or "").lower()
            if not any(marker in key_lower for marker in ("key", "token", "secret", "password", "bearer", "auth")):
                continue
            secret = str(value or "")
            if secret:
                message = message.replace(secret, "***")
        return {
            "ok": False,
            "error": f"测试失败: {message}",
        }


def _test_sms(driver_type: str, extra: dict, label: str = "短信服务") -> dict:
    """通过余额接口测试短信 provider 配置，不购买手机号。"""
    from providers.registry import create_provider, load_all

    try:
        load_all()
        client = create_provider("sms", driver_type, extra)
        configuration_error = getattr(client, "configuration_error", lambda: "")()
        if configuration_error:
            return {"ok": False, "error": configuration_error}
        if hasattr(client, "test_connection"):
            details = client.test_connection()
            return {"ok": True, **dict(details or {})}
        balance = client.get_balance()
        balance_text = f"{balance:g}"
        return {
            "ok": True,
            "message": f"{label} 连接成功，账户余额：{balance_text}",
            "balance": balance,
            "base_url": getattr(client, "base_url", ""),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{label} 连接失败: {str(exc)}"}


def _mask_proxy(proxy_url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    text = str(proxy_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        if not parsed.username and not parsed.password:
            return text
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"***:***@{host}{port}", parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return text


def _test_proxy(driver_type: str, extra: dict) -> dict:
    """Validate proxy config with the same exit-IP probe used by workers."""
    from core.proxy_providers import create_proxy_provider
    from core.worker_proxy import WorkerProxyPolicy, probe_proxy

    try:
        provider = create_proxy_provider(driver_type, extra)
        proxy = provider.get_proxy()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"代理配置无效: {str(exc)}"}

    if not proxy:
        return {"ok": False, "error": "代理服务未返回可用代理"}

    try:
        probe = probe_proxy(proxy, policy=WorkerProxyPolicy.from_config(extra))
        return {
            "ok": True,
            "message": (
                f"代理连接成功：{_mask_proxy(proxy)}，"
                f"出口 {probe.exit_ip}，延迟 {probe.latency_ms}ms"
            ),
            "proxy": _mask_proxy(proxy),
            "origin": probe.exit_ip,
            "latency_ms": probe.latency_ms,
            "check_endpoint": probe.endpoint,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"代理连接失败: {str(exc)}",
            "proxy": _mask_proxy(proxy),
        }


def _test_push(driver_type: str, extra: dict, label: str) -> dict:
    """Validate a push target without sending account credentials."""
    from providers.registry import create_provider, load_all

    try:
        load_all()
        provider = create_provider("push", driver_type, extra)
        error = getattr(provider, "configuration_error", lambda: "")()
        if error:
            return {"ok": False, "error": error}
        test_connection = getattr(provider, "test_connection", None)
        if callable(test_connection):
            return dict(test_connection() or {})
        return {"ok": True, "message": f"{label} 配置有效，可在账号列表选择账号后推送"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{label} 配置无效: {type(exc).__name__}"}
