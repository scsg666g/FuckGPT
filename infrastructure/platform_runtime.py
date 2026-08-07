from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Callable

from sqlmodel import Session

from core.base_platform import RegisterConfig
from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.db import AccountModel, engine
from core.platform_accounts import build_platform_account
from core.proxy_resolution import (
    PROXY_MODE_DIRECT,
    PROXY_MODE_PROXY_SERVICE,
    mask_proxy_url,
    normalize_proxy_mode,
    resolve_proxy_by_mode,
)
from core.registry import get, list_platforms, load_all
from domain.actions import (
    ActionExecutionCommand,
    ActionExecutionResult,
    ActionParameter,
    PlatformAction,
)
from domain.platforms import PlatformCapabilities, PlatformDescriptor


PERSISTED_ACTION_DATA_KEYS = {
    "access_token",
    "refresh_token",
    "session_token",
    "id_token",
    "api_key",
    "client_id",
    "client_secret",
    "workspace_id",
    "cookies",
    "accessToken",
    "refreshToken",
    "sessionToken",
    "idToken",
    "clientId",
    "clientSecret",
    "workspaceId",
    "account_id",
    "accountId",
    "org_id",
    "orgId",
    "auth_token",
    "authToken",
    "codex_access_token",
    "codex_refresh_token",
    "codex_id_token",
    "codex_account_id",
    "codex_email",
    "codex_plan_type",
    "codex_expires_at",
    "codex_last_refresh",
    "codex_auth_path",
}

STATEFUL_ACTION_IDS = {"get_account_state", "switch_account", "query_state", "switch_desktop"}
ACCOUNT_STATE_ACTION_IDS = {"get_account_state", "query_state"}
RELOGIN_ACTION_IDS = {"relogin"}
RELOGIN_CREDENTIAL_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "cookies",
    "workspace_id",
    "account_id",
}
CODEX_OAUTH_SECRET_RESULT_KEYS = {
    "codex_access_token",
    "codex_refresh_token",
    "codex_id_token",
}


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 12:
        return "***"
    return f"{text[:6]}...{text[-4:]}"


def _mask_phone_number(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\+?\d{1,4}\*{3,}\d{2,4}", text):
        return text
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) <= 8:
        return "***"
    if text.startswith("+"):
        return f"+{digits[:3]}****{digits[-4:]}"
    return f"{digits[:4]}****{digits[-4:]}"


def _normalize_chatgpt_usage_plan(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if any(token in raw for token in ("team", "enterprise", "business")):
        return "team"
    if any(token in raw for token in ("plus", "pro", "premium", "paid")):
        return "plus"
    return "free"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _normalized_result_key(key: Any) -> str:
    text = str(key or "").strip().replace("-", "_")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"_+", "_", text.lower())


def _is_phone_result_key(key: Any) -> bool:
    normalized = _normalized_result_key(key)
    compact = normalized.replace("_", "")
    if compact in {
        "phonebound",
        "hasphone",
        "phoneverified",
        "mobileverified",
        "msisdnverified",
        "telverified",
    }:
        return False
    if "phone" in compact:
        return True
    if compact in {"mobile", "mobilenumber", "mobileno", "msisdn", "tel", "telephone", "telephonenumber"}:
        return True
    return bool(set(normalized.split("_")) & {"mobile", "msisdn", "tel", "telephone"})


def _is_sensitive_result_key(key: Any) -> bool:
    normalized = _normalized_result_key(key)
    compact = normalized.replace("_", "")
    if (
        normalized.startswith("has_")
        or normalized.endswith(("_present", "_preview", "_masked"))
        or normalized.endswith(
            (
                "_token_count",
                "_token_counts",
                "_token_used",
                "_token_usage",
                "_token_limit",
                "_token_remaining",
                "_token_total",
                "_token_type",
                "_token_status",
                "_token_expires_at",
            )
        )
    ):
        return False
    if normalized in {
        "token",
        "authorization",
        "api_key",
        "client_secret",
        "password",
        "secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "sso",
        "sso_rw",
        "wos_session",
    }:
        return True
    if compact in {
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "authtoken",
        "apikey",
        "clientsecret",
    }:
        return True
    return (
        normalized.endswith("_token")
        or "_token_" in normalized
        or "cookie" in normalized
        or "password" in normalized
        or normalized.endswith("_secret")
    )


def _sanitize_action_result(value: Any, *, key: str = "") -> Any:
    phone_context = _is_phone_result_key(key)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for child_key, item in value.items():
            if _is_sensitive_result_key(child_key):
                continue
            child_context = str(child_key)
            if phone_context and not _is_phone_result_key(child_key):
                child_context = key
            safe[str(child_key)] = _sanitize_action_result(item, key=child_context)
        return safe
    if isinstance(value, list):
        return [_sanitize_action_result(item, key=key) for item in value]
    if phone_context and not isinstance(value, bool):
        return _mask_phone_number(value)
    return value


def _safe_action_result_data(action_id: str, data: Any) -> Any:
    prepared = dict(data) if isinstance(data, dict) else data
    if action_id == "codex_oauth_authorize" and isinstance(prepared, dict):
        for key in CODEX_OAUTH_SECRET_RESULT_KEYS:
            if key not in prepared:
                continue
            preview_key = f"{key}_preview"
            prepared[preview_key] = prepared.get(preview_key) or _mask_secret(prepared.get(key))
    return _sanitize_action_result(prepared)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _deactivation_reason(error: Any = "") -> str:
    text = str(error or "")
    normalized = re.sub(r"\s+", " ", text).strip()
    lowered = text.casefold()
    japanese_match = re.search(
        r"アカウント[はが][^。]*(?:削除|無効化|無効)[^。]*。(?:[^。]*(?:help\.openai\.com|ヘルプセンター|へルプセンタ一)[^。]*。)?",
        normalized,
    )
    if japanese_match:
        return japanese_match.group(0).strip()
    english_match = re.search(
        r"(?:your\s+)?account[^.\n]*(?:deleted|disabled|deactivated)[^.\n]*\.(?:[^.\n]*(?:help\.openai\.com|help center)[^.\n]*\.)?",
        normalized,
        flags=re.IGNORECASE,
    )
    if english_match:
        return english_match.group(0).strip()
    chinese_match = re.search(r"账号[^。]*(?:停用|禁用|封号|删除)[^。]*。?", normalized)
    if chinese_match:
        return chinese_match.group(0).strip()
    if "deleted or disabled" in lowered:
        return "OpenAI 返回账号已被删除或停用"
    if "削除" in text or "無効" in text:
        return "OpenAI 返回账号已被删除或停用（日文错误页）"
    if "account_deactivated" in lowered:
        return "OpenAI 返回 account_deactivated，账号已被停用/封号"
    return "账号已被 OpenAI 停用/封号"


def _build_account_overview(platform: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    overview: dict[str, Any] = {
        "platform": platform,
        "checked_at": data.get("checked_at") or _utcnow_iso(),
        "chips": [],
    }
    observed_validity = _optional_bool(data.get("valid")) if "valid" in data else None
    if observed_validity is not None:
        overview["valid"] = observed_validity
        overview["chips"].append("有效" if observed_validity else "失效")
    explicit_status = str(
        data.get("validity_status")
        or data.get("subscription_status")
        or data.get("status")
        or ""
    ).strip().casefold()
    if explicit_status == "deactivated":
        overview["validity_status"] = "deactivated"
        if "封号" not in overview["chips"]:
            overview["chips"].append("封号")
    last_error = data.get("last_error") or data.get("check_error")
    if last_error not in (None, ""):
        overview["last_error"] = str(last_error)

    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    remote_email = str(data.get("remote_email") or profile.get("email") or "")
    if not remote_email and isinstance(data.get("remote_user"), dict):
        remote_email = str(data["remote_user"].get("email", "") or "")
    elif not remote_email and isinstance(data.get("portal_user"), dict):
        remote_email = str(data["portal_user"].get("email", "") or "")
    if remote_email:
        overview["remote_email"] = remote_email

    chatgpt_usage = data.get("chatgpt_usage")
    if not isinstance(chatgpt_usage, dict):
        chatgpt_usage = data.get("wham_usage")
    if not isinstance(chatgpt_usage, dict):
        chatgpt_usage = {}
    usage_plan_raw = chatgpt_usage.get("plan_type") if platform == "chatgpt" else ""
    usage_plan = (
        _normalize_chatgpt_usage_plan(usage_plan_raw)
        if str(usage_plan_raw or "").strip()
        else ""
    )
    check_source = (
        "backend-api/wham/usage"
        if str(usage_plan_raw or "").strip()
        else str(data.get("check_source") or data.get("subscription_source") or "").strip()
    )
    if check_source:
        overview["check_source"] = check_source
        overview["subscription_source"] = check_source
    if platform == "chatgpt":
        # Keep the raw usage response for the usage-snapshot adapter.  It does
        # not contain login credentials and is the canonical plan source.
        overview["chatgpt_usage"] = chatgpt_usage

        phone_keys = {"phone_bound", "phone_number_masked", "phone_number", "phoneNumber"}
        phone_observed = any(key in data for key in phone_keys) or any(key in profile for key in phone_keys)
        phone_value = (
            data.get("phone_number_masked")
            or data.get("phone_number")
            or data.get("phoneNumber")
            or profile.get("phone_number_masked")
            or profile.get("phone_number")
            or profile.get("phoneNumber")
            or ""
        )
        phone_number_masked = _mask_phone_number(phone_value)
        explicit_phone_bound = None
        for payload in (data, profile):
            if "phone_bound" in payload:
                explicit_phone_bound = _optional_bool(payload.get("phone_bound"))
                break
        if phone_observed:
            overview["phone_bound"] = bool(phone_number_masked) if explicit_phone_bound is None else explicit_phone_bound
            overview["phone_number_masked"] = phone_number_masked

        amr_observed = "amr" in data or "amr" in profile
        amr = _string_list(data.get("amr")) or _string_list(profile.get("amr"))
        explicit_mfa = None
        mfa_observed = False
        for payload in (data, profile):
            for key in ("mfa_enabled", "has_mfa", "mfa"):
                if key not in payload:
                    continue
                mfa_observed = True
                explicit_mfa = _optional_bool(payload.get(key))
                if explicit_mfa is not None:
                    break
            if explicit_mfa is not None:
                break
        if mfa_observed or amr_observed:
            overview["amr"] = amr
            overview["mfa_enabled"] = (
                explicit_mfa
                if explicit_mfa is not None
                else any("mfa" in item.lower() for item in amr)
            )

    plan = (
        (usage_plan if str(usage_plan or "").strip() else None)
        or data.get("subscription_status")
        or data.get("plan")
        or data.get("membership_type")
        or (data.get("billing_info") or {}).get("membershipType")
        or (data.get("usage_summary") or {}).get("plan_title")
        or (data.get("subscription") or {}).get("plan")
        or ""
    )
    if plan:
        overview["plan"] = plan
        overview["plan_name"] = str(plan)
        plan_lower = str(plan).strip().lower()
        if platform == "chatgpt" and any(token in plan_lower for token in ("team", "enterprise", "business")):
            overview["chips"].append("Team")
        elif platform == "chatgpt" and any(token in plan_lower for token in ("pro", "plus", "premium", "paid")):
            overview["chips"].append("Plus")
        elif platform == "chatgpt" and plan_lower in {"free", "basic", "starter", "hobby"}:
            overview["chips"].append("Free")
        else:
            overview["chips"].append(str(plan))
        if any(token in plan_lower for token in ("pro", "plus", "premium", "business", "team", "enterprise", "student")):
            overview["plan_state"] = "subscribed"
        elif "trial" in plan_lower:
            overview["plan_state"] = "trial"
        elif plan_lower in {"free", "basic", "starter", "hobby"}:
            overview["plan_state"] = "free"

    if platform == "chatgpt" and overview.get("phone_bound"):
        overview["chips"].append("已绑手机")

    if "trial_eligible" in data:
        overview["trial_eligible"] = data.get("trial_eligible")
        overview["chips"].append("可试用" if data.get("trial_eligible") else "不可试用")
    if data.get("trial_length_days"):
        overview["trial_length_days"] = data.get("trial_length_days")
        overview["chips"].append(f"{data['trial_length_days']}天试用")
    if "has_valid_payment_method" in data:
        overview["has_valid_payment_method"] = data.get("has_valid_payment_method")
        overview["chips"].append("已绑卡" if data.get("has_valid_payment_method") else "未绑卡")

    for key in (
        "remaining_credits",
        "usage_total",
        "plan_credits",
        "next_reset_at",
        "days_until_reset",
        "prompt_credits_limit",
        "flow_action_credits_limit",
        "prompt_remaining_percent",
        "flow_action_remaining_percent",
    ):
        if data.get(key) not in (None, ""):
            overview[key] = data.get(key)
    if isinstance(data.get("usage_breakdowns"), list):
        overview["usage_breakdowns"] = data.get("usage_breakdowns")
        for item in data.get("usage_breakdowns") or []:
            if not isinstance(item, dict):
                continue
            label = item.get("display_name") or item.get("resource_type") or "usage"
            remaining = item.get("remaining_usage")
            limit = item.get("usage_limit")
            chip = f"{label}"
            if remaining not in (None, ""):
                chip += f" 剩{remaining}"
            if limit not in (None, ""):
                chip += f" / {limit}"
            overview["chips"].append(chip)

    usage_summary = data.get("usage_summary") or {}
    if platform == "cursor" and isinstance(usage_summary.get("models"), dict):
        usage_models = []
        for model_name, info in usage_summary["models"].items():
            if not isinstance(info, dict):
                continue
            usage_models.append({
                "model": model_name,
                "num_requests": info.get("num_requests"),
                "num_requests_total": info.get("num_requests_total"),
                "num_tokens": info.get("num_tokens"),
                "remaining_requests": info.get("remaining_requests"),
                "remaining_tokens": info.get("remaining_tokens"),
            })
            chip = f"{model_name} {info.get('num_requests', 0)}次"
            if info.get("remaining_requests") is not None:
                chip += f" / 剩{info['remaining_requests']}"
            overview["chips"].append(chip)
        if usage_models:
            overview["usage_models"] = usage_models

    if platform == "kiro" and isinstance(usage_summary, dict):
        if usage_summary.get("next_reset_at"):
            overview["next_reset_at"] = usage_summary.get("next_reset_at")
        if usage_summary.get("days_until_reset") is not None:
            overview["days_until_reset"] = usage_summary.get("days_until_reset")
            overview["chips"].append(f"重置 {usage_summary.get('days_until_reset')} 天")
        breakdowns = []
        for item in usage_summary.get("breakdowns") or []:
            if not isinstance(item, dict):
                continue
            breakdowns.append({
                "display_name": item.get("display_name"),
                "current_usage": item.get("current_usage"),
                "usage_limit": item.get("usage_limit"),
                "remaining_usage": item.get("remaining_usage"),
                "trial_status": item.get("trial_status"),
                "trial_expiry": item.get("trial_expiry"),
                "trial_remaining_usage": item.get("trial_remaining_usage"),
            })
            label = item.get("display_name") or item.get("resource_type") or "usage"
            chip = f"{label} {item.get('current_usage', 0)}/{item.get('usage_limit', '-')}"
            if item.get("trial_status"):
                chip += f" · {item['trial_status']}"
            overview["chips"].append(chip)
        if breakdowns:
            overview["usage_breakdowns"] = breakdowns

    if isinstance(data.get("local_app_account"), dict):
        overview["local_matches_target"] = bool(data["local_app_account"].get("matches_target"))
        if data["local_app_account"].get("matches_target"):
            overview["chips"].append("当前")

    if isinstance(data.get("desktop_app_state"), dict):
        desktop_state = data["desktop_app_state"]
        overview["desktop_app_state"] = {
            "app_name": desktop_state.get("app_name"),
            "running": bool(desktop_state.get("running")),
            "ready": bool(desktop_state.get("ready")),
            "configured": bool(desktop_state.get("configured")),
            "installed": bool(desktop_state.get("installed")),
            "status_label": desktop_state.get("status_label", ""),
            "ready_label": desktop_state.get("ready_label", ""),
        }

    if data.get("quota_note"):
        overview["quota_note"] = data.get("quota_note")

    overview["chips"] = [chip for chip in overview["chips"] if chip]
    return overview if len(overview) > 2 else None


def _persist_action_result(command: ActionExecutionCommand, result: dict[str, Any]) -> None:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    action_ok = bool(result.get("ok"))
    credential_updates = {
        key: value
        for key, value in data.items()
        if action_ok and key in PERSISTED_ACTION_DATA_KEYS and value not in (None, "")
    }
    summary_updates: dict[str, Any] = {}
    if command.action_id in ACCOUNT_STATE_ACTION_IDS:
        if action_ok:
            overview = _build_account_overview(command.platform, data)
            if overview:
                summary_updates.update(overview)
        else:
            error = str(result.get("error") or data.get("error") or "账号状态查询失败")
            if error != "任务已取消":
                summary_updates = {"checked_at": _utcnow_iso(), "last_error": error}
    elif command.action_id in RELOGIN_ACTION_IDS:
        event_at = str(data.get("checked_at") or data.get("failed_at") or _utcnow_iso())
        if action_ok:
            profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
            auth_mode = str(data.get("registration_auth_mode") or "").strip()
            summary_updates = {
                "checked_at": event_at,
                "valid": True,
                "last_error": "",
                "remote_email": data.get("remote_email") or profile.get("email") or "",
                "profile": profile,
                "registration_auth_mode": auth_mode,
                "security_raw": {
                    "last_login_at": event_at,
                    "last_login_status": "succeeded",
                    "last_login_auth_mode": auth_mode,
                    "last_login_failure_code": "",
                    "deactivation_reason": "",
                    "deactivation_error": "",
                    "deactivation_detected_at": "",
                },
            }
        else:
            error = str(result.get("error") or data.get("error") or "重新登录失败")
            failure_code = str(data.get("failure_code") or "").strip() or (
                "account_deactivated"
                if "account_deactivated" in error.casefold() or "deleted or disabled" in error.casefold()
                else "unexpected"
            )
            summary_updates = {
                "checked_at": event_at,
                "last_error": error,
                "security_raw": {
                    "last_login_at": event_at,
                    "last_login_status": "failed",
                    "last_login_error": error,
                    "last_login_failure_code": failure_code,
                },
            }
            if failure_code == "account_deactivated":
                summary_updates["validity_status"] = "deactivated"
                summary_updates["security_raw"].update(
                    {
                        "deactivation_reason": _deactivation_reason(error),
                        "deactivation_error": error,
                        "deactivation_detected_at": event_at,
                    }
                )

    if not credential_updates and not summary_updates:
        return

    with Session(engine) as session:
        model = session.get(AccountModel, command.account_id)
        if not model or model.platform != command.platform:
            return
        lifecycle_status = None
        if summary_updates.get("valid") is True:
            current_graph = load_account_graphs(session, [int(model.id or 0)]).get(int(model.id or 0), {})
            merged_graph = dict(current_graph)
            merged_overview = dict(merged_graph.get("overview") or {})
            merged_overview.update(summary_updates)
            merged_graph["overview"] = merged_overview
            lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)
        model.updated_at = datetime.now(timezone.utc)
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates=summary_updates or None,
            credential_updates=credential_updates or None,
            replace_credential_keys=(
                set(RELOGIN_CREDENTIAL_KEYS)
                if command.action_id in RELOGIN_ACTION_IDS and action_ok
                else None
            ),
        )
        if command.action_id in RELOGIN_ACTION_IDS and action_ok:
            remote_account_id = str(data.get("account_id") or "").strip()
            if remote_account_id:
                model.user_id = remote_account_id
        session.add(model)
        session.commit()


def persist_action_failure(
    *,
    platform: str,
    account_id: int,
    action_id: str,
    error: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Persist failures that happen before :class:`PlatformRuntime` executes.

    Proxy acquisition and other task-owned preparation can fail before
    ``execute_action`` gets a chance to write account-level diagnostics.
    Keeping this small public boundary avoids duplicating graph persistence in
    the task layer.
    """
    _persist_action_result(
        ActionExecutionCommand(
            platform=str(platform or ""),
            account_id=int(account_id or 0),
            action_id=str(action_id or ""),
            params={},
        ),
        {"ok": False, "error": str(error or ""), "data": dict(data or {})},
    )


class PlatformRuntime:
    def list_platforms(self) -> list[PlatformDescriptor]:
        load_all()
        descriptors: list[PlatformDescriptor] = []
        for item in list_platforms():
            descriptors.append(
                PlatformDescriptor(
                    name=item["name"],
                    display_name=item["display_name"],
                    version=item["version"],
                    capabilities=PlatformCapabilities(
                        supported_executors=list(item.get("supported_executors", [])),
                        supported_identity_modes=list(item.get("supported_identity_modes", [])),
                        supported_oauth_providers=list(item.get("supported_oauth_providers", [])),
                    ),
                )
            )
        return descriptors

    def list_actions(self, platform: str) -> list[PlatformAction]:
        load_all()
        platform_cls = get(platform)
        instance = platform_cls(config=RegisterConfig())
        actions = []
        for item in instance.get_platform_actions():
            params = [
                ActionParameter(
                    key=str(param.get("key", "")),
                    label=str(param.get("label", "")),
                    type=str(param.get("type", "text")),
                    options=list(param.get("options", []) or []),
                )
                for param in item.get("params", [])
            ]
            actions.append(
                PlatformAction(
                    id=str(item.get("id", "")),
                    label=str(item.get("label", "")),
                    params=params,
                    sync=bool(item.get("sync", False)),
                )
            )
        return actions
    
    def get_desktop_state(self, platform: str) -> dict[str, Any]:
        load_all()
        platform_cls = get(platform)
        instance = platform_cls(config=RegisterConfig())
        return instance.get_desktop_state() or {"available": False}

    def execute_action(
        self,
        command: ActionExecutionCommand,
        *,
        log_fn=None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> ActionExecutionResult:
        load_all()
        if callable(cancel_check) and cancel_check():
            return ActionExecutionResult(ok=False, error="任务已取消")

        platform_cls = get(command.platform)
        params = dict(command.params or {})
        proxy_mode = normalize_proxy_mode(
            str(params.get("platform_proxy_mode") or "").strip(),
            default=PROXY_MODE_DIRECT,
        )
        proxy_value = str(params.get("platform_proxy_value") or "").strip()
        try:
            from core.proxy_pool import proxy_pool

            action_proxy = resolve_proxy_by_mode(
                proxy_mode,
                manual_proxy=proxy_value,
                proxy_getter=proxy_pool.get_next,
            )
        except Exception as exc:
            if proxy_mode == PROXY_MODE_PROXY_SERVICE:
                return ActionExecutionResult(ok=False, error=f"代理获取失败: {exc}")
            action_proxy = None
        if proxy_mode == PROXY_MODE_PROXY_SERVICE and not action_proxy:
            return ActionExecutionResult(ok=False, error="代理服务未返回可用代理")
        instance = platform_cls(
            config=RegisterConfig(
                proxy=action_proxy,
                extra={"disable_proxy_pool": proxy_mode == PROXY_MODE_DIRECT},
            )
        )
        if log_fn:
            instance.set_logger(log_fn)
            if params.get("platform_proxy_mode") or action_proxy:
                proxy_log_mode = str(params.get("_proxy_log_mode") or proxy_mode)
                log_fn(
                    f"ChatGPT/Codex 代理: {mask_proxy_url(action_proxy) if action_proxy else '直连'}"
                    f"（{proxy_log_mode}）"
                )
        if callable(cancel_check):
            if hasattr(instance, "set_cancel_checker"):
                instance.set_cancel_checker(cancel_check)
            else:
                instance._cancel_check_fn = cancel_check

        # Build a detached platform account in a short read session.  Browser,
        # OAuth and remote API work must not hold a SQLite transaction open.
        with Session(engine) as session:
            model = session.get(AccountModel, command.account_id)
            if not model or model.platform != command.platform:
                return ActionExecutionResult(ok=False, error="账号不存在")
            account = build_platform_account(session, model)
            try:
                setattr(account, "id", int(model.id or 0))
            except Exception:
                pass

        try:
            if callable(cancel_check) and cancel_check():
                return ActionExecutionResult(ok=False, error="任务已取消")
            action_params = dict(command.params or {})
            action_params.pop("_proxy_log_mode", None)
            result = instance.execute_action(command.action_id, account, action_params)
        except NotImplementedError as exc:
            return ActionExecutionResult(ok=False, data={"error_type": "not_supported"}, error=str(exc))
        except Exception as exc:
            failure = {"ok": False, "error": str(exc)}
            _persist_action_result(command, failure)
            return ActionExecutionResult(ok=False, error=str(exc))

        if not isinstance(result, dict):
            result = {"ok": False, "error": "平台动作返回格式无效"}
        _persist_action_result(command, result)
        return ActionExecutionResult(
            ok=bool(result.get("ok")),
            data=_safe_action_result_data(command.action_id, result.get("data")),
            error=str(result.get("error", "")),
        )
