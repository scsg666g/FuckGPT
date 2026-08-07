from __future__ import annotations

import json
import os
import random
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from application.account_recovery import (
    AccountReloginResult,
    AccountStateSnapshot,
    check_and_recover_account,
    execute_runtime_action_with_worker_proxy,
)
from application.accounts import AccountsService
from application.tasks import (
    create_codex_oauth_batch_task,
    enqueue_nvtokens_push_after_codex_oauth,
    get_nvtokens_auto_push_state,
)
from core.db import AccountModel, KakaoPipelineModel, ProviderSettingModel, engine
from core.db import AccountCodexAuthModel, AccountPushDeliveryModel, TaskModel
from core.platform_accounts import build_platform_account
from core.proxy_resolution import (
    PROXY_MODE_DIRECT,
    PROXY_MODE_MANUAL,
    PROXY_MODE_PROXY_SERVICE,
    mask_proxy_url,
    normalize_proxy_mode,
)
from domain.actions import ActionExecutionResult
from infrastructure.platform_runtime import PlatformRuntime  # re-exported for existing integration hooks
from infrastructure.provider_settings_repository import ProviderSettingsRepository

from .client import CustomerApiClient, CustomerApiProblem, normalize_base_url
from .workstation_client import WorkstationScannerClient


KAKAO_PROVIDER_TYPE = "kakao_pipeline"
SCANNER_KINDS = ("scanner", "scanner_546789")
ACCOUNT_PROXY_MODES = {PROXY_MODE_DIRECT, PROXY_MODE_MANUAL, PROXY_MODE_PROXY_SERVICE}
KAKAO_ACCOUNT_VIEWS = {"workspace", "completed", "archived", "all"}
ARCHIVE_DISPOSITIONS = {"auto", "completed", "abandoned"}
ARCHIVE_UNCERTAIN_STATES = {
    "supplier_poll_failed",
    "supplier_submit_unconfirmed",
    "scanner_poll_failed",
    "scanner_submit_unconfirmed",
    "scanner_recovery_unconfirmed",
    "plus_unconfirmed",
    "plus_check_failed",
}
MAX_ARCHIVE_BATCH_SIZE = 500
SCANNER_BASE_URL = "https://customer.i7wap.xyz"
LEGACY_SCANNER_BASE_URLS = {"https://upi.i7wap.xyz"}
SETTING_DEFAULTS = {
    "supplier": {
        "display_name": "Kakao 提链供应商",
        "base_url": "http://127.0.0.1:8788",
        "env_url": "KAKAO_SUPPLIER_BASE_URL",
        "env_key": "KAKAO_SUPPLIER_CDK_KEY",
    },
    "scanner": {
        "display_name": "I7wap 扫码平台",
        "base_url": SCANNER_BASE_URL,
        "env_url": "KAKAO_SCANNER_BASE_URL",
        "env_key": "KAKAO_SCANNER_CDK_KEY",
        "driver_type": "customer_api",
    },
    "scanner_546789": {
        "display_name": "546789 扫码平台",
        "base_url": "https://kakao.546789.shop",
        "env_url": "KAKAO_546789_BASE_URL",
        "env_key": "KAKAO_546789_CDK_KEY",
        "driver_type": "payment_submission",
    },
}

ACTIVE_STATES = {
    "supplier_submitting",
    "supplier_processing",
    "scanner_submitting",
    "scanner_processing",
    "scanner_accepted_untracked",
    "scanner_succeeded",
    "plus_checking",
    "plus_pending",
}

BACKGROUND_POLL_STATES = {
    "supplier_submitting",
    "supplier_processing",
    "scanner_submitting",
    "scanner_processing",
    "scanner_accepted_untracked",
    "scanner_succeeded",
    "plus_checking",
    "plus_pending",
    "codex_post_action",
}

CODEX_POST_ACTION_STATE = "codex_post_action"
REMOTE_BACKGROUND_POLL_STATES = BACKGROUND_POLL_STATES - {CODEX_POST_ACTION_STATE}
TASK_PENDING_STATUSES = {"pending", "claimed"}
TASK_RUNNING_STATUSES = {"running", "cancel_requested"}
TASK_ACTIVE_STATUSES = TASK_PENDING_STATUSES | TASK_RUNNING_STATUSES
TASK_FAILED_STATUSES = {"failed", "cancelled", "interrupted"}
MAX_CODEX_INTERRUPTED_RETRIES = 1

TERMINAL_REMOTE_FAILURES = {"FAILED", "CANCELLED", "EXPIRED", "REJECTED"}
I7_SCANNER_SUCCESS_STATUSES = {"SUCCESS", "SUCCEEDED", "COMPLETED", "CONFIRMED"}
I7_SUBSCRIPTION_SUCCESS_STATUSES = {"VERIFIED", "PLUS"}
WORKSTATION_PROCESSING_STATUSES = {"QUEUED", "PENDING", "PROCESSING", "ASSIGNED", "RUNNING", "CHECKING"}
WORKSTATION_FAILURE_STATUSES = {"FAILED", "EXPIRED", "CANCELLED", "REJECTED", "ERROR", "CLOSED"}
MAX_SCANNER_POLL_FAILURES = 6
SUPPLIER_PROCESSING_WINDOW_SECONDS = 15 * 60
SCANNER_PROCESSING_WINDOW_SECONDS = 30 * 60
SUBMIT_RECOVERY_GRACE_SECONDS = 60
UNTRACKED_PLUS_INITIAL_DELAY_SECONDS = 30
UNTRACKED_PLUS_WINDOW_SECONDS = 30 * 60
UNTRACKED_PLUS_INITIAL_DELAYS_SECONDS = (30, 60)
PLUS_CONFIRM_WINDOW_SECONDS = 10 * 60
PLUS_CONFIRM_INITIAL_DELAYS_SECONDS = (5, 10, 30, 30, 30)
PLUS_CONFIRM_MIN_INTERVAL_SECONDS = 60
PLUS_CONFIRM_MAX_INTERVAL_SECONDS = 120

_account_locks: dict[int, threading.RLock] = {}
_account_locks_guard = threading.Lock()
_cdk_pool_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _age_seconds(value: datetime | None) -> float:
    if value is None:
        return float("inf")
    return max((_utcnow() - _as_utc(value)).total_seconds(), 0.0)


def _as_utc(value: datetime) -> datetime:
    current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _earlier_deadline(default_deadline: datetime, remote_value: Any) -> datetime:
    remote_deadline = _parse_datetime(remote_value)
    if remote_deadline is None:
        return default_deadline
    return min(_as_utc(default_deadline), _as_utc(remote_deadline))


def _next_plus_delay_seconds(check_count: int) -> int:
    index = max(int(check_count or 1) - 1, 0)
    if index < len(PLUS_CONFIRM_INITIAL_DELAYS_SECONDS):
        return PLUS_CONFIRM_INITIAL_DELAYS_SECONDS[index]
    return random.randint(PLUS_CONFIRM_MIN_INTERVAL_SECONDS, PLUS_CONFIRM_MAX_INTERVAL_SECONDS)


def _next_untracked_plus_delay_seconds(check_count: int) -> int:
    index = max(int(check_count or 1) - 1, 0)
    if index < len(UNTRACKED_PLUS_INITIAL_DELAYS_SECONDS):
        return UNTRACKED_PLUS_INITIAL_DELAYS_SECONDS[index]
    return random.randint(PLUS_CONFIRM_MIN_INTERVAL_SECONDS, PLUS_CONFIRM_MAX_INTERVAL_SECONDS)


def _account_lock(account_id: int) -> threading.RLock:
    with _account_locks_guard:
        return _account_locks.setdefault(int(account_id), threading.RLock())


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}{'*' * min(len(text) - 6, 12)}{text[-3:]}"


def _parse_cdk_keys(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[\r\n,]+", str(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        key = _text(item)
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _is_cdk_depleted(code: str, message: str) -> bool:
    text = f"{code} {message}".lower().replace("-", "_")
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)
    return (
        any(token in text for token in (
            "insufficient_cdk",
            "cdk_insufficient",
            "cdk_quota_exhausted",
            "cdk_uses_exhausted",
        ))
        or ("cdk" in compact and any(token in compact for token in ("insufficient", "nouses", "exhausted", "quota")))
        or any(token in compact for token in ("cdk可用次数不足", "cdk次数不足", "cdk额度不足", "cdk已用完"))
    )


def _is_workstation_cdk_depleted(code: str, message: str) -> bool:
    if _is_cdk_depleted(code, message):
        return True
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", f"{code} {message}".lower())
    return any(token in compact for token in ("额度不足", "余额不足", "次数不足", "quotaexhausted", "insufficientquota"))


def _normalized_key(value: object) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _is_secret_key(value: object) -> bool:
    key = _normalized_key(value)
    compact = key.replace("_", "")
    if compact in {
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "customertoken",
        "cdkkey",
        "authorization",
        "password",
        "cookie",
        "cookies",
        "secret",
    }:
        return True
    return compact.endswith(("accesstoken", "customertoken", "cdkkey", "password", "secret"))


def sanitize_remote(value: Any, *, key: object = "") -> Any:
    if _is_secret_key(key):
        return "***"
    if isinstance(value, dict):
        return {str(child_key): sanitize_remote(child_value, key=child_key) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [sanitize_remote(item, key=key) for item in value]
    return value


def _data(payload: dict) -> dict:
    value = payload.get("data") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _nested_text(payload: dict, *paths: tuple[str, ...]) -> str:
    for path in paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        text = _text(current)
        if text:
            return text
    return ""


def _append_event(model: KakaoPipelineModel, message: str, *, level: str = "info", detail: dict | None = None) -> None:
    events = model.get_events()
    events.append(
        {
            "time": _utcnow_iso(),
            "level": level,
            "message": str(message or "")[:1000],
            "detail": sanitize_remote(detail or {}),
        }
    )
    model.set_events(events[-200:])


def _set_error(model: KakaoPipelineModel, state: str, code: str, message: str) -> None:
    model.state = state
    model.last_error_code = str(code or "unknown_error")[:120]
    model.last_error_message = str(message or "未知错误")[:1000]
    model.updated_at = _utcnow()
    _append_event(model, model.last_error_message, level="error", detail={"code": model.last_error_code})


def _has_valid_codex_auth(auth: AccountCodexAuthModel | None) -> bool:
    return bool(auth and auth.has_access_token and auth.has_refresh_token)


def _task_account_result(task: TaskModel | None, account_id: int) -> dict:
    if task is None:
        return {}
    result = task.get_result()
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
    return next(
        (
            item
            for item in accounts
            if isinstance(item, dict) and int(item.get("account_id") or 0) == int(account_id)
        ),
        {},
    )


_ERROR_URL_RE = re.compile(r"(?i)\b(?:https?|socks4|socks5)://[^\s<>\"']+")
_ERROR_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_ERROR_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        [\"']?
        (?:
            access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|
            client[_-]?secret|api[_-]?key|authorization|password|passwd|
            proxy[_-]?password|cookie
        )
        [\"']?\s*[:=]\s*
    )
    (?:
        \"(?:\\.|[^\"])*\"|
        '(?:\\.|[^'])*'|
        [^\s,;}\]]+
    )
    """
)
_ERROR_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_ERROR_PHONE_RE = re.compile(r"(?<![\w])\+?(?:\d[\s().-]?){8,15}(?![\w])")


def _redact_error_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    suffix = ""
    while raw and raw[-1] in ".,;!)]}":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname or ""
        if not hostname:
            return "[redacted-url]" + suffix
        safe_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{safe_host}:{port}" if port else safe_host
        safe_url = urlunsplit(
            (
                parsed.scheme,
                netloc,
                parsed.path,
                "***" if parsed.query else "",
                "***" if parsed.fragment else "",
            )
        )
        return safe_url + suffix
    except Exception:
        return "[redacted-url]" + suffix


def _redact_sensitive_error(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = _ERROR_URL_RE.sub(_redact_error_url, text)
    text = _ERROR_BEARER_RE.sub("Bearer ***", text)
    text = _ERROR_SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group('prefix')}***", text)
    text = _ERROR_EMAIL_RE.sub("***@***", text)
    text = _ERROR_PHONE_RE.sub("***", text)
    return text[:1000]


def _safe_task_error(task: TaskModel | None, account_id: int = 0) -> str:
    account_result = _task_account_result(task, account_id) if account_id else {}
    return _redact_sensitive_error(
        (account_result or {}).get("error") or (task.error if task else "") or ""
    )


def _delivery_covers_codex_auth(
    delivery: AccountPushDeliveryModel | None,
    auth: AccountCodexAuthModel | None,
    *,
    not_before: datetime | None = None,
) -> bool:
    if (
        delivery is None
        or auth is None
        or delivery.status != "success"
        or delivery.pushed_at is None
    ):
        return False
    credential_at = auth.last_refresh or auth.created_at
    if credential_at is None:
        return False
    required_at = _as_utc(credential_at)
    if not_before is not None:
        required_at = max(required_at, _as_utc(not_before))
    return _as_utc(delivery.pushed_at) >= required_at


def _codex_auth_was_saved_for_task(
    auth: AccountCodexAuthModel | None,
    task: TaskModel | None,
) -> bool:
    """Distinguish credentials saved by this task from older valid tokens."""
    if not _has_valid_codex_auth(auth) or task is None or auth.last_refresh is None:
        return False
    task_started_at = task.started_at or task.created_at
    return _as_utc(auth.last_refresh) >= _as_utc(task_started_at)


def _push_task_account_ok(task: TaskModel | None, account_id: int) -> bool:
    if task is None:
        return False
    result = task.get_result()
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    items = data.get("results") if isinstance(data.get("results"), list) else []
    return any(
        isinstance(item, dict)
        and int(item.get("account_id") or 0) == int(account_id)
        and bool(item.get("ok"))
        for item in items
    )


def _problem_from_payload(data: dict, default_code: str, default_message: str) -> tuple[str, str]:
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    return (
        _text(error.get("code") or data.get("problemCode") or default_code),
        _text(error.get("message") or data.get("problemReason") or default_message),
    )


def _valid_payment_url(value: str) -> str:
    text = _text(value)
    parsed = urlsplit(text)
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or not (host == "nicepay.co.kr" or host.endswith(".nicepay.co.kr")):
        raise ValueError("供应商返回的不是受支持的 NicePay HTTPS 长链")
    return text


def _find_scan_value(value: Any, wanted: set[str]) -> str:
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalized_key(key) in wanted and _text(child):
                return _text(child)
        for child in value.values():
            found = _find_scan_value(child, wanted)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_scan_value(child, wanted)
            if found:
                return found
    return ""


def _safe_scan_url(value: str) -> str:
    text = _text(value)
    parsed = urlsplit(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _scanner_outcome(data: dict) -> str:
    main_status = _text(data.get("status")).upper()
    subscription = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    subscription_status = _text(subscription.get("status")).upper()
    if main_status in TERMINAL_REMOTE_FAILURES or subscription_status in TERMINAL_REMOTE_FAILURES:
        return "failed"
    if main_status in I7_SCANNER_SUCCESS_STATUSES and subscription_status in I7_SUBSCRIPTION_SUCCESS_STATUSES:
        return "success"
    return "processing"


def _workstation_outcome(status: str) -> str:
    normalized = _text(status).upper()
    if normalized == "COMPLETED":
        return "success"
    if normalized == "UNKNOWN":
        return "missing"
    if normalized in WORKSTATION_FAILURE_STATUSES:
        return "failed"
    if normalized in WORKSTATION_PROCESSING_STATUSES:
        return "processing"
    return "unrecognized"


def _is_duplicate_payment_submission(exc: Exception) -> bool:
    if not isinstance(exc, CustomerApiProblem) or int(exc.status_code or 0) != 409:
        return False
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", f"{exc.code} {exc.message}".lower())
    return (
        any(
            token in compact
            for token in (
                "支付链接已经提交",
                "支付链接已提交",
                "paymentlinkalreadysubmitted",
                "duplicatepaymentlink",
            )
        )
        or ("支付链接" in compact and "重复点击" in compact)
    )


def _is_ambiguous_submit_problem(exc: Exception) -> bool:
    if not isinstance(exc, CustomerApiProblem):
        return isinstance(exc, (ValueError, RuntimeError))
    return exc.code in {"network_error", "invalid_json", "invalid_response"} or int(exc.status_code or 0) >= 500


def _is_transient_poll_problem(exc: Exception) -> bool:
    return isinstance(exc, CustomerApiProblem) and (
        exc.code in {"network_error", "invalid_json", "invalid_response"}
        or int(exc.status_code or 0) >= 500
    )


def _is_missing_submission_problem(exc: Exception) -> bool:
    return isinstance(exc, CustomerApiProblem) and exc.code == "submission_missing"


class KakaoPipelineService:
    def __init__(self):
        self.settings = ProviderSettingsRepository()
        self.accounts = AccountsService()

    def _setting_payload(self, kind: str) -> tuple[ProviderSettingModel | None, dict]:
        if kind not in SETTING_DEFAULTS:
            raise ValueError("未知 Kakao 配置类型")
        defaults = SETTING_DEFAULTS[kind]
        item = self.settings.get_by_key(KAKAO_PROVIDER_TYPE, kind)
        config = item.get_config() if item else {}
        auth = item.get_auth() if item else {}
        base_url = _text(
            config.get("base_url")
            or os.getenv(str(defaults["env_url"]), "")
            or defaults["base_url"]
        )
        if kind == "scanner" and base_url.rstrip("/") in LEGACY_SCANNER_BASE_URLS:
            base_url = SCANNER_BASE_URL
        has_saved_pool = "cdk_keys" in auth
        cdk_keys = _parse_cdk_keys(auth.get("cdk_keys"))
        if not has_saved_pool and not cdk_keys:
            cdk_keys = _parse_cdk_keys(auth.get("cdk_key") or os.getenv(str(defaults["env_key"]), ""))
        return item, {
            "base_url": base_url,
            "cdk_key": cdk_keys[0] if cdk_keys else "",
            "cdk_keys": cdk_keys,
            "display_name": _text(item.display_name if item else defaults["display_name"]),
            "driver_type": _text(defaults.get("driver_type") or "customer_api"),
        }

    def list_settings(self) -> dict:
        result = {}
        for kind in ("supplier", "scanner", "scanner_546789"):
            item, payload = self._setting_payload(kind)
            result[kind] = {
                "id": int(item.id or 0) if item else None,
                "kind": kind,
                "display_name": payload["display_name"],
                "base_url": payload["base_url"],
                "has_cdk": bool(payload["cdk_keys"]),
                "cdk_count": len(payload["cdk_keys"]),
                "cdk_keys": payload["cdk_keys"],
                "cdk_preview": _mask_secret(payload["cdk_key"]),
                "driver_type": payload["driver_type"],
            }
        result["default_scanner_kind"] = self._default_scanner_kind()
        result["auto_upload_after_extract"] = self._auto_upload_after_extract()
        result["account_proxy"] = self._account_proxy_options()
        return result

    def _default_scanner_kind(self) -> str:
        for kind in SCANNER_KINDS:
            item = self.settings.get_by_key(KAKAO_PROVIDER_TYPE, kind)
            if item and item.get_metadata().get("pipeline_default") is True:
                return kind
        return "scanner"

    def set_default_scanner(self, kind: str) -> dict:
        selected = _text(kind)
        if selected not in SCANNER_KINDS:
            raise ValueError("未知扫码供应商")
        for scanner_kind in SCANNER_KINDS:
            item, current = self._setting_payload(scanner_kind)
            if item is None and scanner_kind != selected:
                continue
            metadata = item.get_metadata() if item else {}
            metadata.update({"temporary_feature": True, "pipeline_default": scanner_kind == selected})
            self.settings.save(
                setting_id=int(item.id or 0) if item else None,
                provider_type=KAKAO_PROVIDER_TYPE,
                provider_key=scanner_kind,
                display_name=current["display_name"],
                auth_mode="apikey",
                enabled=True,
                is_default=True,
                config={"base_url": current["base_url"]},
                auth={"cdk_keys": current["cdk_keys"]},
                metadata=metadata,
            )
        return {"ok": True, "default_scanner_kind": selected, "settings": self.list_settings()}

    def _auto_upload_after_extract(self) -> bool:
        for kind in SCANNER_KINDS:
            item = self.settings.get_by_key(KAKAO_PROVIDER_TYPE, kind)
            if item and item.get_metadata().get("auto_upload_after_extract") is True:
                return True
        return False

    def set_auto_upload(self, enabled: bool) -> dict:
        selected = self._default_scanner_kind()
        for kind in SCANNER_KINDS:
            item, current = self._setting_payload(kind)
            if item is None and kind != selected:
                continue
            metadata = item.get_metadata() if item else {}
            metadata.update({"temporary_feature": True, "auto_upload_after_extract": bool(enabled)})
            self.settings.save(
                setting_id=int(item.id or 0) if item else None,
                provider_type=KAKAO_PROVIDER_TYPE,
                provider_key=kind,
                display_name=current["display_name"],
                auth_mode="apikey",
                enabled=True,
                is_default=True,
                config={"base_url": current["base_url"]},
                auth={"cdk_keys": current["cdk_keys"]},
                metadata=metadata,
            )
        return {"ok": True, "auto_upload_after_extract": bool(enabled), "settings": self.list_settings()}

    def _account_proxy_options(self) -> dict[str, str]:
        for kind in SCANNER_KINDS:
            item = self.settings.get_by_key(KAKAO_PROVIDER_TYPE, kind)
            if item is None:
                continue
            metadata = item.get_metadata()
            if "account_proxy_mode" not in metadata:
                continue
            mode = normalize_proxy_mode(
                _text(metadata.get("account_proxy_mode")),
                default=PROXY_MODE_DIRECT,
            )
            if mode not in ACCOUNT_PROXY_MODES:
                mode = PROXY_MODE_DIRECT
            value = _text(metadata.get("account_proxy_value")) if mode == PROXY_MODE_MANUAL else ""
            return {
                "mode": mode,
                "value": value,
                "preview": mask_proxy_url(value),
            }
        return {"mode": PROXY_MODE_DIRECT, "value": "", "preview": ""}

    def set_account_proxy(self, mode: str, value: str = "") -> dict:
        requested_mode = _text(mode).lower()
        if requested_mode not in ACCOUNT_PROXY_MODES:
            raise ValueError("未知账号检查代理模式")
        proxy_value = _text(value)
        if requested_mode == PROXY_MODE_MANUAL:
            if not proxy_value:
                raise ValueError("手动代理模式需要填写代理 URL")
            parsed = urlsplit(proxy_value)
            if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5", "socks5h"} or not parsed.hostname:
                raise ValueError("手动代理 URL 格式无效")
        else:
            proxy_value = ""

        self.settings.definitions.ensure_seeded()
        selected = self._default_scanner_kind()
        for kind in SCANNER_KINDS:
            item, current = self._setting_payload(kind)
            if item is None and kind != selected:
                continue
            metadata = item.get_metadata() if item else {}
            metadata.update(
                {
                    "temporary_feature": True,
                    "account_proxy_mode": requested_mode,
                    "account_proxy_value": proxy_value,
                }
            )
            self.settings.save(
                setting_id=int(item.id or 0) if item else None,
                provider_type=KAKAO_PROVIDER_TYPE,
                provider_key=kind,
                display_name=current["display_name"],
                auth_mode="apikey",
                enabled=True,
                is_default=True,
                config={"base_url": current["base_url"]},
                auth={"cdk_keys": current["cdk_keys"]},
                metadata=metadata,
            )
        return {"ok": True, "account_proxy": self._account_proxy_options(), "settings": self.list_settings()}

    def _maybe_auto_submit_scanner(self, account_id: int, result: dict) -> dict:
        if result.get("state") != "link_ready" or not self._auto_upload_after_extract():
            return result
        try:
            return self.submit_scanner(account_id)
        except Exception as exc:  # noqa: BLE001
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                if pipeline.state == "link_ready":
                    pipeline.last_error_code = "auto_upload_failed"
                    pipeline.last_error_message = f"自动上传扫码失败: {exc}"[:1000]
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, pipeline.last_error_message, level="warning")
                    session.add(pipeline)
                    session.commit()
                return self._serialize_pipeline(pipeline, detail=True)

    def save_setting(self, kind: str, payload: dict) -> dict:
        if kind not in SETTING_DEFAULTS:
            raise ValueError("未知 Kakao 配置类型")
        current, current_payload = self._setting_payload(kind)
        base_url = normalize_base_url(_text(payload.get("base_url") or current_payload["base_url"]))
        cdk_keys = _parse_cdk_keys(payload.get("cdk_keys"))
        if not cdk_keys and _text(payload.get("cdk_key")):
            cdk_keys = _parse_cdk_keys(payload.get("cdk_key"))
        metadata = current.get_metadata() if current else {}
        metadata.update({"temporary_feature": True})
        item = self.settings.save(
            setting_id=int(current.id or 0) if current else None,
            provider_type=KAKAO_PROVIDER_TYPE,
            provider_key=kind,
            display_name=_text(payload.get("display_name") or current_payload["display_name"]),
            auth_mode="apikey",
            enabled=True,
            is_default=True,
            config={"base_url": base_url},
            auth={"cdk_keys": cdk_keys},
            metadata=metadata,
        )
        return {"ok": True, "item": self.list_settings()[kind], "id": int(item.id or 0)}

    def test_setting(self, kind: str, payload: dict | None = None) -> dict:
        _, current = self._setting_payload(kind)
        overrides = dict(payload or {})
        base_url = _text(overrides.get("base_url") or current["base_url"])
        cdk_keys = _parse_cdk_keys(overrides.get("cdk_keys")) or _parse_cdk_keys(overrides.get("cdk_key")) or current["cdk_keys"]
        cdk_key = cdk_keys[0] if cdk_keys else ""
        if kind == "scanner_546789":
            WorkstationScannerClient(base_url, cdk_key).test_connection()
        else:
            CustomerApiClient(base_url, cdk_key).test_connection()
        return {"ok": True, "message": f"{SETTING_DEFAULTS[kind]['display_name']}连接成功"}

    def check_cdks(self, kind: str, payload: dict | None = None) -> dict:
        if kind not in SETTING_DEFAULTS:
            raise ValueError("未知 Kakao 配置类型")
        _, current = self._setting_payload(kind)
        overrides = dict(payload or {})
        base_url = _text(overrides.get("base_url") or current["base_url"])
        cdk_keys = _parse_cdk_keys(overrides.get("cdk_keys")) or _parse_cdk_keys(overrides.get("cdk_key")) or current["cdk_keys"]
        if not cdk_keys:
            raise ValueError("没有可校验的 CDK")
        results = []
        depleted = []
        for key in cdk_keys:
            try:
                client = CustomerApiClient(base_url, key)
                if kind == "scanner":
                    quota = client.check_cdk()
                    total_count = int(quota.get("totalCount") or 0)
                    used_count = int(quota.get("usedCount") or 0)
                    frozen_count = int(quota.get("frozenCount") or 0)
                    available_count = int(quota.get("availableCount") or 0)
                    cdk_status = _text(quota.get("status")).upper() or "UNKNOWN"
                    exhausted = available_count <= 0 or cdk_status in {"EXHAUSTED", "USED", "DEPLETED"}
                    status = "depleted" if exhausted else ("valid" if cdk_status == "ACTIVE" else "invalid")
                    if exhausted:
                        depleted.append(key)
                    results.append(
                        {
                            "cdk_key": key,
                            "status": status,
                            "message": f"可用 {available_count} / 总计 {total_count}，已用 {used_count}，冻结 {frozen_count}",
                            "product_type": _text(quota.get("productType")),
                            "cdk_status": cdk_status,
                            "total_count": total_count,
                            "used_count": used_count,
                            "frozen_count": frozen_count,
                            "available_count": available_count,
                            "expires_at": quota.get("expiresAt"),
                            "remark": _text(quota.get("remark")),
                        }
                    )
                elif kind == "scanner_546789":
                    quota = WorkstationScannerClient(base_url, key).check_cdk()
                    unlimited = quota.get("unlimited") is True
                    remaining = int(quota.get("remaining") or 0)
                    limit = int(quota.get("limit") or 0)
                    exhausted = not unlimited and remaining <= 0
                    status = "depleted" if exhausted else "valid"
                    if exhausted:
                        depleted.append(key)
                    results.append(
                        {
                            "cdk_key": key,
                            "status": status,
                            "message": "无限额度" if unlimited else f"剩余 {remaining} / {limit}",
                            "cdk_status": "UNLIMITED" if unlimited else ("DEPLETED" if exhausted else "ACTIVE"),
                            "total_count": limit,
                            "used_count": max(0, limit - remaining),
                            "frozen_count": 0,
                            "available_count": remaining,
                            "unlimited": unlimited,
                        }
                    )
                else:
                    client.test_connection()
                    results.append({"cdk_key": key, "status": "valid", "message": "鉴权有效；提链接口未提供精确剩余额度"})
            except CustomerApiProblem as exc:
                exhausted = _is_cdk_depleted(exc.code, exc.message)
                results.append({"cdk_key": key, "status": "depleted" if exhausted else "invalid", "message": exc.message})
                if exhausted:
                    depleted.append(key)
            except Exception as exc:  # noqa: BLE001
                results.append({"cdk_key": key, "status": "invalid", "message": str(exc)})
        if depleted:
            self._remove_cdks(kind, depleted)
        return {"ok": True, "items": results, "removed": depleted, "quota_supported": kind in {"scanner", "scanner_546789"}}

    def _remove_cdks(self, kind: str, keys: list[str]) -> list[str]:
        remove = set(_parse_cdk_keys(keys))
        if not remove:
            return []
        with _cdk_pool_lock:
            item, current = self._setting_payload(kind)
            if item is None:
                return []
            remaining = [key for key in current["cdk_keys"] if key not in remove]
            removed = [key for key in current["cdk_keys"] if key in remove]
            if not removed:
                return []
            metadata = item.get_metadata()
            metadata.update({"temporary_feature": True})
            self.settings.save(
                setting_id=int(item.id or 0),
                provider_type=KAKAO_PROVIDER_TYPE,
                provider_key=kind,
                display_name=current["display_name"],
                auth_mode="apikey",
                enabled=True,
                is_default=True,
                config={"base_url": current["base_url"]},
                auth={"cdk_keys": remaining},
                metadata=metadata,
            )
            return removed

    @staticmethod
    def _pipeline_for_account(
        session: Session,
        account_id: int,
        *,
        create: bool = False,
        allow_archived: bool = False,
    ) -> KakaoPipelineModel | None:
        model = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == int(account_id))
        ).first()
        if model is not None and model.archived_at is not None and not allow_archived:
            raise ValueError("Kakao 流水线已归档，请先恢复后再操作")
        if model is None and create:
            model = KakaoPipelineModel(account_id=int(account_id))
            session.add(model)
            session.flush()
        return model

    @staticmethod
    def _ensure_account_pipeline_mutable(account_id: int) -> None:
        with Session(engine) as session:
            pipeline = session.exec(
                select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == int(account_id))
            ).first()
            if pipeline is not None and pipeline.archived_at is not None:
                raise ValueError("Kakao 流水线已归档，请先恢复后再操作")

    @staticmethod
    def _account_credentials(account_id: int) -> tuple[AccountModel, str, str]:
        with Session(engine) as session:
            model = session.get(AccountModel, int(account_id))
            if not model or model.platform != "chatgpt":
                raise ValueError("ChatGPT 账号不存在")
            account = build_platform_account(session, model)
            extra = account.extra or {}
            token = _text(account.token or extra.get("access_token") or extra.get("accessToken"))
            session_cookie = _text(extra.get("session_cookie") or extra.get("session_token") or extra.get("sessionToken"))
            if not session_cookie and extra.get("cookies"):
                cookies = extra.get("cookies")
                session_cookie = cookies if isinstance(cookies, str) else json.dumps(cookies, ensure_ascii=False)
            if not token and not session_cookie:
                raise ValueError("账号缺少 session_cookie 和 access_token")
            session.expunge(model)
            return model, token, session_cookie

    def _account_is_plus(self, account_id: int) -> bool:
        account = self.accounts.get_account(int(account_id)) or {}
        view = account.get("account_view") if isinstance(account.get("account_view"), dict) else {}
        subscription = view.get("subscription") if isinstance(view.get("subscription"), dict) else {}
        plan = _text(subscription.get("plan") or account.get("plan_name")).lower()
        plan_state = _text(subscription.get("state") or account.get("plan_state")).lower()
        return plan_state == "subscribed" or any(
            token in plan for token in ("plus", "pro", "team", "business", "enterprise")
        )

    @staticmethod
    def _append_account_pipeline_event(account_id: int, message: str, *, level: str = "info") -> None:
        with Session(engine) as session:
            pipeline = session.exec(
                select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == int(account_id))
            ).first()
            if pipeline is None or pipeline.archived_at is not None:
                return
            pipeline.updated_at = _utcnow()
            _append_event(pipeline, message, level=level)
            session.add(pipeline)
            session.commit()

    def _query_account_state_with_relogin(
        self,
        account_id: int,
    ) -> tuple[ActionExecutionResult, dict[str, bool]]:
        """Query ChatGPT state and repair an explicitly invalid login once.

        Transport failures and indeterminate checks must not launch a browser.
        A failed login recovery is marked so callers can pause the pipeline
        instead of repeatedly starting browser workers on the short Plus poll
        interval.
        """
        proxy_options = self._account_proxy_options()
        query_params = {
            "platform_proxy_mode": proxy_options["mode"],
            "platform_proxy_value": proxy_options["value"],
        }
        scope_id = f"kakao-plus:{int(account_id)}"

        def pipeline_log(message: str, *, level: str = "info", **_kwargs: Any) -> None:
            self._append_account_pipeline_event(account_id, str(message), level=level)

        def execute(action_id: str, params: dict[str, Any]) -> ActionExecutionResult:
            return execute_runtime_action_with_worker_proxy(
                platform="chatgpt",
                account_id=int(account_id),
                action_id=action_id,
                params=params,
                scope_id=scope_id,
                log_fn=pipeline_log,
                runtime_factory=PlatformRuntime,
            )

        def check_state() -> AccountStateSnapshot:
            return AccountStateSnapshot.from_action(execute("query_state", query_params))

        def relogin_account() -> AccountReloginResult:
            return AccountReloginResult.from_action(
                execute(
                    "relogin",
                    {
                        "browser_mode": "headless",
                        "keep_browser_open": "false",
                        **query_params,
                    },
                )
            )

        try:
            recovery = check_and_recover_account(
                check_state=check_state,
                relogin=relogin_account,
                relogin_invalid=True,
                log_fn=pipeline_log,
                label="Plus ",
            )
        finally:
            from core.worker_proxy import worker_proxy_manager

            worker_proxy_manager.clear_scope(scope_id)

        flags = {
            "attempted": recovery.relogin_attempted,
            "relogin_ok": recovery.relogin_ok,
            "recovery_failed": recovery.recovery_failed,
        }
        if not recovery.relogin_attempted:
            return ActionExecutionResult(
                ok=recovery.initial.ok,
                data=recovery.initial.data,
                error=recovery.initial.error,
            ), flags
        if not recovery.relogin_ok:
            error = recovery.relogin_error or "自动重新登录失败"
            return ActionExecutionResult(
                ok=False,
                data=recovery.relogin_data,
                error=f"账号失效且{error}",
            ), flags
        if recovery.recovery_failed:
            return ActionExecutionResult(
                ok=False,
                data=recovery.final.data,
                error=recovery.relogin_error or "自动重新登录后账号仍为失效状态",
            ), flags
        return ActionExecutionResult(
            ok=recovery.final.ok,
            data=recovery.final.data,
            error=recovery.final.error,
        ), flags

    @staticmethod
    def _load_post_actions_context(
        account_ids: list[int],
        pipelines: dict[int, KakaoPipelineModel],
    ) -> dict[str, Any]:
        normalized_ids = sorted({int(item) for item in account_ids if int(item or 0) > 0})
        task_ids = sorted(
            {
                task_id
                for pipeline in pipelines.values()
                for task_id in (pipeline.codex_task_id, pipeline.codex_push_task_id)
                if task_id
            }
        )
        auth_by_account: dict[int, AccountCodexAuthModel] = {}
        task_by_id: dict[str, TaskModel] = {}
        delivery_by_account: dict[int, AccountPushDeliveryModel] = {}
        if normalized_ids:
            with Session(engine) as session:
                auth_rows = session.exec(
                    select(AccountCodexAuthModel).where(
                        AccountCodexAuthModel.account_id.in_(normalized_ids)
                    )
                ).all()
                auth_by_account = {int(item.account_id): item for item in auth_rows}
                if task_ids:
                    task_rows = session.exec(
                        select(TaskModel).where(TaskModel.id.in_(task_ids))
                    ).all()
                    task_by_id = {str(item.id): item for item in task_rows}
                delivery_rows = session.exec(
                    select(AccountPushDeliveryModel)
                    .where(AccountPushDeliveryModel.account_id.in_(normalized_ids))
                    .where(AccountPushDeliveryModel.target_key == "nvtokens")
                ).all()
                delivery_by_account = {int(item.account_id): item for item in delivery_rows}
        return {
            "push_setting": get_nvtokens_auto_push_state(),
            "auth_by_account": auth_by_account,
            "task_by_id": task_by_id,
            "delivery_by_account": delivery_by_account,
        }

    @staticmethod
    def _serialize_post_actions(
        model: KakaoPipelineModel | None,
        *,
        account_id: int = 0,
        context: dict[str, Any] | None = None,
    ) -> dict:
        push_setting = (
            dict(context.get("push_setting") or {})
            if context is not None
            else get_nvtokens_auto_push_state()
        )
        push_enabled = bool(push_setting.get("enabled"))
        if model is None:
            if context is not None:
                authorized = _has_valid_codex_auth(
                    (context.get("auth_by_account") or {}).get(int(account_id or 0))
                )
            elif int(account_id or 0) > 0:
                with Session(engine) as session:
                    authorized = _has_valid_codex_auth(
                        session.get(AccountCodexAuthModel, int(account_id))
                    )
            else:
                authorized = False
            return {
                "codex": {
                    "status": "waiting",
                    "task_id": None,
                    "authorized": authorized,
                    "error": "",
                },
                "push": {
                    "status": "waiting" if push_enabled else "skipped",
                    "task_id": None,
                    "enabled": push_enabled,
                    "error": "",
                    "target_key": "nvtokens",
                },
            }

        account_id = int(model.account_id)
        if context is not None:
            auth = (context.get("auth_by_account") or {}).get(account_id)
            tasks = context.get("task_by_id") or {}
            codex_task = tasks.get(model.codex_task_id) if model.codex_task_id else None
            push_task = tasks.get(model.codex_push_task_id) if model.codex_push_task_id else None
            delivery = (context.get("delivery_by_account") or {}).get(account_id)
        else:
            with Session(engine) as session:
                auth = session.get(AccountCodexAuthModel, account_id)
                codex_task = session.get(TaskModel, model.codex_task_id) if model.codex_task_id else None
                push_task = session.get(TaskModel, model.codex_push_task_id) if model.codex_push_task_id else None
                delivery = session.exec(
                    select(AccountPushDeliveryModel)
                    .where(AccountPushDeliveryModel.account_id == account_id)
                    .where(AccountPushDeliveryModel.target_key == "nvtokens")
                    .order_by(AccountPushDeliveryModel.updated_at.desc())
                ).first()

        authorized = _has_valid_codex_auth(auth)
        codex_task_status = _text(codex_task.status) if codex_task else ""
        codex_result = _task_account_result(codex_task, account_id)
        codex_error = ""
        if model.codex_skipped_at and authorized:
            codex_status = "skipped"
        elif codex_task_status == "interrupted" and _codex_auth_was_saved_for_task(auth, codex_task):
            codex_status = "skipped"
        elif codex_task_status in TASK_PENDING_STATUSES:
            codex_status = "pending"
        elif codex_task_status in TASK_RUNNING_STATUSES:
            codex_status = "running"
        elif codex_task_status == "succeeded":
            if codex_result.get("ok") and _codex_auth_was_saved_for_task(auth, codex_task):
                codex_status = "success"
            else:
                codex_status = "failed"
                codex_error = _safe_task_error(codex_task, account_id) or "Codex authorization did not save complete credentials"
        elif codex_task_status in {"interrupted", "cancelled"}:
            codex_status = "paused"
            codex_error = _safe_task_error(codex_task, account_id) or "Codex authorization paused"
        elif codex_task_status == "failed":
            codex_status = "failed"
            codex_error = _safe_task_error(codex_task, account_id) or "Codex authorization failed"
        elif model.codex_enqueue_error:
            codex_status = "failed"
            codex_error = _redact_sensitive_error(model.codex_enqueue_error)
        else:
            codex_status = "waiting"

        push_task_status = _text(push_task.status) if push_task else ""
        unlinked_current_delivery = bool(
            push_task is None
            and authorized
            and _delivery_covers_codex_auth(
                delivery,
                auth,
                not_before=model.completed_at,
            )
        )
        push_error = ""
        if push_task_status in TASK_PENDING_STATUSES:
            push_status = "pending"
        elif push_task_status in TASK_RUNNING_STATUSES:
            push_status = "running"
        elif push_task_status == "succeeded":
            if _push_task_account_ok(push_task, account_id):
                push_status = "success"
            else:
                push_status = "failed"
                push_error = _safe_task_error(push_task, account_id) or "NexusVault push result is incomplete"
        elif push_task_status in TASK_FAILED_STATUSES:
            # A later generic manual push is the supported retry path.  It may
            # override a failed linked task, but an older delivery must never
            # complete a new linked attempt.
            task_started_at = push_task.started_at or push_task.created_at
            delivery_updated_at = (
                (delivery.last_attempt_at or delivery.updated_at)
                if delivery
                else None
            )
            manual_retry_succeeded = bool(
                delivery
                and _delivery_covers_codex_auth(delivery, auth)
                and delivery_updated_at
                and task_started_at
                and _as_utc(delivery_updated_at) >= _as_utc(task_started_at)
            )
            if manual_retry_succeeded:
                push_status = "success"
            elif push_task_status in {"interrupted", "cancelled"}:
                push_status = "paused"
                push_error = _safe_task_error(push_task, account_id) or "NexusVault push paused"
            else:
                push_status = "failed"
                push_error = _safe_task_error(push_task, account_id) or _redact_sensitive_error(
                    (delivery.last_error if delivery else "") or "NexusVault push failed"
                )
        elif unlinked_current_delivery:
            # A manual/generic push may finish after automatic enqueue was
            # skipped or failed.  With no real linked task, that delivery is
            # the authoritative fifth-stage result.
            push_status = "success"
        elif model.codex_push_skip_reason == "already_delivered":
            push_status = "success"
        elif model.codex_push_skip_reason:
            push_status = "skipped"
        elif model.codex_push_enqueue_error:
            push_status = "failed"
            push_error = _redact_sensitive_error(model.codex_push_enqueue_error)
        elif not push_enabled:
            push_status = "skipped"
        else:
            push_status = "waiting"

        return {
            "codex": {
                "status": codex_status,
                "task_id": model.codex_task_id or None,
                "authorized": authorized,
                "error": codex_error,
                "attempt_count": int(model.codex_attempt_count or 0),
            },
            "push": {
                "status": push_status,
                "task_id": model.codex_push_task_id or None,
                "enabled": push_enabled,
                "error": push_error,
                "target_key": "nvtokens",
                "attempt_count": int(model.codex_push_attempt_count or 0),
                "pushed_at": delivery.pushed_at.isoformat() if delivery and delivery.pushed_at else None,
            },
        }

    @staticmethod
    def _serialize_pipeline(
        model: KakaoPipelineModel | None,
        *,
        detail: bool = False,
        account_id: int = 0,
        post_actions_context: dict[str, Any] | None = None,
    ) -> dict:
        if model is None:
            return {
                "state": "idle",
                "supplier_status": "",
                "scanner_status": "",
                "plus_status": "",
                "final_result": "",
                "last_error_code": "",
                "last_error_message": "",
                "latest_event_at": None,
                "archived_at": None,
                "archive_reason": "",
                "archive_disposition": "",
                "purged_at": None,
                "post_actions": KakaoPipelineService._serialize_post_actions(
                    None,
                    account_id=account_id,
                    context=post_actions_context,
                ),
            }
        supplier = model.get_supplier_response()
        scanner = model.get_scanner_response()
        events = model.get_events()
        latest_event = events[-1] if events and isinstance(events[-1], dict) else {}
        supplier_data = _data(supplier)
        scanner_data = _data(scanner)
        extraction = supplier_data.get("extraction") if isinstance(supplier_data.get("extraction"), dict) else {}
        subscription = scanner_data.get("subscription") if isinstance(scanner_data.get("subscription"), dict) else {}
        payload = {
            "id": int(model.id or 0),
            "account_id": int(model.account_id),
            "state": model.state,
            "payment_method": model.payment_method,
            "supplier_name": model.supplier_name,
            "supplier_status": model.supplier_status,
            "supplier_order_id": model.supplier_order_id,
            "supplier_stage": int(extraction.get("stage") or 0),
            "supplier_stage_total": int(extraction.get("stageTotal") or 0),
            "supplier_stage_name": _text(extraction.get("stageName")),
            "supplier_processing_started_at": model.supplier_processing_started_at.isoformat() if model.supplier_processing_started_at else None,
            "supplier_deadline_at": model.supplier_deadline_at.isoformat() if model.supplier_deadline_at else None,
            "payment_url": model.payment_url,
            "scanner_driver": model.scanner_driver or "customer_api",
            "scanner_name": model.scanner_name,
            "scanner_status": model.scanner_status,
            "scanner_order_id": model.scanner_order_id,
            "scanner_subscription_status": _text(subscription.get("status")),
            "scan_url": model.scan_url,
            "scan_expires_at": model.scan_expires_at,
            "scanner_submit_attempts": int(model.scanner_submit_attempts or 0),
            "scanner_compensation_attempted": bool(model.scanner_compensation_attempted),
            "scanner_poll_failures": int(model.scanner_poll_failures or 0),
            "scanner_recovery_reason": model.scanner_recovery_reason,
            "scanner_recovery_check_count": int(model.scanner_recovery_check_count or 0),
            "scanner_recovery_started_at": model.scanner_recovery_started_at.isoformat() if model.scanner_recovery_started_at else None,
            "scanner_recovery_next_check_at": model.scanner_recovery_next_check_at.isoformat() if model.scanner_recovery_next_check_at else None,
            "scanner_recovery_deadline_at": model.scanner_recovery_deadline_at.isoformat() if model.scanner_recovery_deadline_at else None,
            "scanner_processing_started_at": model.scanner_processing_started_at.isoformat() if model.scanner_processing_started_at else None,
            "scanner_deadline_at": model.scanner_deadline_at.isoformat() if model.scanner_deadline_at else None,
            "plus_status": model.plus_status,
            "final_result": model.final_result,
            "completion_source": model.completion_source,
            "plus_check_count": int(model.plus_check_count or 0),
            "plus_check_started_at": model.plus_check_started_at.isoformat() if model.plus_check_started_at else None,
            "plus_next_check_at": model.plus_next_check_at.isoformat() if model.plus_next_check_at else None,
            "plus_check_deadline_at": model.plus_check_deadline_at.isoformat() if model.plus_check_deadline_at else None,
            "plus_check_paused_at": model.plus_check_paused_at.isoformat() if model.plus_check_paused_at else None,
            "last_error_code": model.last_error_code,
            "last_error_message": model.last_error_message,
            "created_at": model.created_at.isoformat() if model.created_at else None,
            "updated_at": model.updated_at.isoformat() if model.updated_at else None,
            "latest_event_at": _text(latest_event.get("time")) or None,
            "completed_at": model.completed_at.isoformat() if model.completed_at else None,
            "archived_at": model.archived_at.isoformat() if model.archived_at else None,
            "archive_reason": model.archive_reason,
            "archive_disposition": model.archive_disposition,
            "purged_at": model.purged_at.isoformat() if model.purged_at else None,
            "post_actions": KakaoPipelineService._serialize_post_actions(
                model,
                context=post_actions_context,
            ),
        }
        if detail:
            payload.update(
                {
                    "events": events,
                    "supplier_response": sanitize_remote(supplier),
                    "scanner_response": sanitize_remote(scanner),
                }
            )
        return payload

    def list_accounts(
        self,
        *,
        search: str = "",
        page: int = 1,
        page_size: int = 20,
        view: str = "workspace",
    ) -> dict:
        selected_view = _text(view).lower() or "workspace"
        if selected_view not in KAKAO_ACCOUNT_VIEWS:
            raise ValueError("未知 Kakao 账号视图")

        bounded_page = max(int(page), 1)
        bounded_page_size = min(max(int(page_size), 1), 100)
        conditions = [AccountModel.platform == "chatgpt"]
        search_text = _text(search)
        if search_text:
            escaped = (
                search_text.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            search_condition = AccountModel.email.ilike(f"%{escaped}%", escape="\\")
            if search_text.isdigit():
                search_condition = or_(AccountModel.id == int(search_text), search_condition)
            conditions.append(search_condition)

        completed_condition = and_(
            or_(
                KakaoPipelineModel.state == "completed",
                KakaoPipelineModel.final_result == "plus",
            ),
            or_(
                KakaoPipelineModel.codex_post_action_armed == False,  # noqa: E712
                KakaoPipelineModel.codex_post_action_done_at.is_not(None),
            ),
        )
        if selected_view == "workspace":
            conditions.extend(
                [
                    KakaoPipelineModel.archived_at.is_(None),
                    or_(
                        KakaoPipelineModel.id.is_(None),
                        ~completed_condition,
                    ),
                ]
            )
        elif selected_view == "completed":
            conditions.extend(
                [
                    KakaoPipelineModel.archived_at.is_(None),
                    KakaoPipelineModel.id.is_not(None),
                    completed_condition,
                ]
            )
        elif selected_view == "archived":
            conditions.append(KakaoPipelineModel.archived_at.is_not(None))

        if selected_view == "archived":
            ordering = (KakaoPipelineModel.archived_at.desc(), AccountModel.id.desc())
        elif selected_view == "completed":
            ordering = (
                func.coalesce(
                    KakaoPipelineModel.completed_at,
                    KakaoPipelineModel.updated_at,
                ).desc(),
                AccountModel.id.desc(),
            )
        else:
            ordering = (AccountModel.created_at.desc(), AccountModel.id.desc())

        with Session(engine) as session:
            count_statement = (
                select(func.count(AccountModel.id))
                .select_from(AccountModel)
                .outerjoin(
                    KakaoPipelineModel,
                    KakaoPipelineModel.account_id == AccountModel.id,
                )
                .where(*conditions)
            )
            total = int(session.exec(count_statement).one())
            account_models = session.exec(
                select(AccountModel)
                .outerjoin(
                    KakaoPipelineModel,
                    KakaoPipelineModel.account_id == AccountModel.id,
                )
                .where(*conditions)
                .order_by(*ordering)
                .offset((bounded_page - 1) * bounded_page_size)
                .limit(bounded_page_size)
            ).all()
            records = self.accounts.repository._load_records(session, list(account_models))
            page_accounts = [self.accounts._serialize(record) for record in records]
            account_ids = [int(account.id or 0) for account in account_models]
            pipeline_rows = (
                session.exec(
                    select(KakaoPipelineModel).where(
                        KakaoPipelineModel.account_id.in_(account_ids)
                    )
                ).all()
                if account_ids
                else []
            )
            pipelines = {int(item.account_id): item for item in pipeline_rows}

        post_actions_context = self._load_post_actions_context(account_ids, pipelines)
        items = []
        for account in page_accounts:
            account_id = int(account.get("id") or 0)
            view = account.get("account_view") if isinstance(account.get("account_view"), dict) else {}
            identity = view.get("identity") if isinstance(view.get("identity"), dict) else {}
            subscription = view.get("subscription") if isinstance(view.get("subscription"), dict) else {}
            status = view.get("status") if isinstance(view.get("status"), dict) else {}
            security = view.get("security") if isinstance(view.get("security"), dict) else {}
            items.append(
                {
                    "id": account_id,
                    "email": _text(identity.get("email") or account.get("email")),
                    "plan": _text(subscription.get("plan") or account.get("plan_name") or "unknown"),
                    "plan_state": _text(subscription.get("state") or account.get("plan_state") or "unknown"),
                    "validity": _text(status.get("validity") or account.get("validity_status") or "unknown"),
                    "checked_at": status.get("checked_at"),
                    "account_view": {
                        "status": {"checked_at": status.get("checked_at")},
                        "security": {
                            "phone_bound": bool(security.get("phone_bound")),
                            "phone_number_masked": _text(security.get("phone_number_masked")),
                        },
                    },
                    "pipeline": self._serialize_pipeline(
                        pipelines.get(account_id),
                        account_id=account_id,
                        post_actions_context=post_actions_context,
                    ),
                }
            )
        return {
            "total": total,
            "page": bounded_page,
            "page_size": bounded_page_size,
            "view": selected_view,
            "items": items,
        }

    @staticmethod
    def _normalize_archive_account_ids(account_ids: list[int]) -> list[int]:
        raw_ids = list(account_ids or [])
        if not raw_ids:
            raise ValueError("至少选择一个账号")
        if len(raw_ids) > MAX_ARCHIVE_BATCH_SIZE:
            raise ValueError(f"单次最多处理 {MAX_ARCHIVE_BATCH_SIZE} 个账号")
        normalized: list[int] = []
        seen: set[int] = set()
        for raw_id in raw_ids:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("账号 ID 必须是正整数") from exc
            if account_id <= 0:
                raise ValueError("账号 ID 必须是正整数")
            if account_id not in seen:
                seen.add(account_id)
                normalized.append(account_id)
        return normalized

    @staticmethod
    def _archive_batch_result(action: str, items: list[dict]) -> dict:
        success_count = sum(1 for item in items if item.get("ok"))
        error_count = len(items) - success_count
        return {
            "ok": error_count == 0,
            "action": action,
            "total": len(items),
            "success_count": success_count,
            "error_count": error_count,
            "items": items,
        }

    @staticmethod
    def _load_archive_pipeline(account_id: int) -> KakaoPipelineModel | None:
        with Session(engine) as session:
            pipeline = session.exec(
                select(KakaoPipelineModel).where(
                    KakaoPipelineModel.account_id == int(account_id)
                )
            ).first()
            if pipeline is not None:
                session.expunge(pipeline)
            return pipeline

    def archive_accounts(
        self,
        account_ids: list[int],
        *,
        reason: str = "",
        disposition: str = "auto",
        force: bool = False,
    ) -> dict:
        items: list[dict] = []
        archive_reason = _text(reason)[:1000]
        selected_disposition = _text(disposition).lower() or "auto"
        if selected_disposition not in ARCHIVE_DISPOSITIONS:
            raise ValueError("未知 Kakao 归档处置类型")
        for account_id in self._normalize_archive_account_ids(account_ids):
            try:
                items.append(
                    self._archive_account(
                        account_id,
                        reason=archive_reason,
                        disposition=selected_disposition,
                        force=bool(force),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - batch results are intentionally per account.
                items.append({"account_id": account_id, "ok": False, "error": str(exc)})
        return self._archive_batch_result("archive", items)

    def _archive_account(
        self,
        account_id: int,
        *,
        reason: str,
        disposition: str,
        force: bool,
    ) -> dict:
        warnings: list[str] = []
        active_task_ids: list[str] = []
        changed = False
        with _account_lock(account_id):
            with Session(engine) as session:
                account = session.get(AccountModel, int(account_id))
                if account is None or account.platform != "chatgpt":
                    raise ValueError("ChatGPT 账号不存在")
                pipeline = self._pipeline_for_account(
                    session,
                    account_id,
                    create=True,
                    allow_archived=True,
                )
                assert pipeline is not None
                linked_task_ids = [
                    task_id
                    for task_id in (pipeline.codex_task_id, pipeline.codex_push_task_id)
                    if task_id
                ]
                active_task_ids = [
                    task_id
                    for task_id in linked_task_ids
                    if (
                        (task := session.get(TaskModel, task_id)) is not None
                        and task.status in TASK_ACTIVE_STATUSES
                    )
                ]
                if pipeline.archived_at is not None:
                    if not (force and active_task_ids):
                        return {
                            "account_id": account_id,
                            "ok": True,
                            "changed": False,
                            "pipeline": self._serialize_pipeline(pipeline),
                            "warnings": [],
                        }
                else:
                    pipeline_active = pipeline.state in ACTIVE_STATES
                    pipeline_uncertain = pipeline.state in ARCHIVE_UNCERTAIN_STATES
                    if (pipeline_active or pipeline_uncertain or active_task_ids) and not force:
                        raise ValueError("Kakao 流水线或关联本地任务仍在执行；请使用 force 强制归档")

                    now = _utcnow()
                    pipeline_completed = (
                        pipeline.state == "completed" or pipeline.final_result == "plus"
                    )
                    if disposition == "completed" and not pipeline_completed:
                        raise ValueError("尚未完成的 Kakao 流水线不能归档为 completed")
                    if disposition == "abandoned" and pipeline_completed:
                        raise ValueError("已完成的 Kakao 流水线不能归档为 abandoned")
                    pipeline.archived_at = now
                    pipeline.archive_reason = reason
                    pipeline.archive_disposition = (
                        ("completed" if pipeline_completed else "abandoned")
                        if disposition == "auto"
                        else disposition
                    )
                    pipeline.updated_at = now
                    if (pipeline_active or pipeline_uncertain) and force:
                        warnings.append(
                            "已强制归档；远端供应商/扫码任务或最终结果无法从本地取消或确认，可能仍会继续执行"
                        )
                    _append_event(
                        pipeline,
                        "Kakao 流水线已归档",
                        level="warning" if warnings else "info",
                        detail={
                            "reason": reason,
                            "disposition": pipeline.archive_disposition,
                            "forced": bool(force),
                        },
                    )
                    session.add(pipeline)
                    session.commit()
                    changed = True

            if force and active_task_ids:
                from application.tasks import request_cancel

                for task_id in active_task_ids:
                    try:
                        request_cancel(task_id)
                    except Exception as exc:  # noqa: BLE001 - archive already blocks further advancement.
                        warnings.append(f"关联本地任务 {task_id} 取消失败: {exc}")

            pipeline = self._load_archive_pipeline(account_id)
            assert pipeline is not None
            return {
                "account_id": account_id,
                "ok": True,
                "changed": changed,
                "pipeline": self._serialize_pipeline(pipeline),
                "warnings": warnings,
            }

    def restore_accounts(self, account_ids: list[int]) -> dict:
        items: list[dict] = []
        for account_id in self._normalize_archive_account_ids(account_ids):
            try:
                items.append(self._restore_account(account_id))
            except Exception as exc:  # noqa: BLE001 - batch results are intentionally per account.
                items.append({"account_id": account_id, "ok": False, "error": str(exc)})
        return self._archive_batch_result("restore", items)

    def _restore_account(self, account_id: int) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(
                    session,
                    account_id,
                    allow_archived=True,
                )
                if pipeline is None or pipeline.archived_at is None:
                    raise ValueError("Kakao 流水线未归档")
                if pipeline.purged_at is not None:
                    raise ValueError("Kakao 归档已清除详情，不能恢复")
                disposition = pipeline.archive_disposition
                pipeline.archived_at = None
                pipeline.archive_reason = ""
                pipeline.archive_disposition = ""
                pipeline.updated_at = _utcnow()
                _append_event(
                    pipeline,
                    "Kakao 流水线已从归档恢复",
                    detail={"previous_disposition": disposition},
                )
                session.add(pipeline)
                session.commit()
                return {
                    "account_id": account_id,
                    "ok": True,
                    "changed": True,
                    "pipeline": self._serialize_pipeline(pipeline),
                    "warnings": [],
                }

    def purge_archived_accounts(self, account_ids: list[int]) -> dict:
        items: list[dict] = []
        for account_id in self._normalize_archive_account_ids(account_ids):
            try:
                items.append(self._purge_archived_account(account_id))
            except Exception as exc:  # noqa: BLE001 - batch results are intentionally per account.
                items.append({"account_id": account_id, "ok": False, "error": str(exc)})
        return self._archive_batch_result("purge", items)

    def _purge_archived_account(self, account_id: int) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(
                    session,
                    account_id,
                    allow_archived=True,
                )
                if pipeline is None or pipeline.archived_at is None:
                    raise ValueError("只能清除已归档的 Kakao 流水线")
                if pipeline.purged_at is not None:
                    return {
                        "account_id": account_id,
                        "ok": True,
                        "changed": False,
                        "pipeline": self._serialize_pipeline(pipeline),
                        "warnings": [],
                    }

                active_task_ids = [
                    task_id
                    for task_id in (pipeline.codex_task_id, pipeline.codex_push_task_id)
                    if task_id
                    and (
                        (task := session.get(TaskModel, task_id)) is not None
                        and task.status in TASK_ACTIVE_STATUSES
                    )
                ]
                if active_task_ids:
                    raise ValueError("关联本地任务仍在取消或执行，请稍后再清除")

                self._clear_pipeline_for_purge(pipeline)
                session.add(pipeline)
                session.commit()
                return {
                    "account_id": account_id,
                    "ok": True,
                    "changed": True,
                    "pipeline": self._serialize_pipeline(pipeline),
                    "warnings": [],
                }

    @staticmethod
    def _clear_pipeline_for_purge(pipeline: KakaoPipelineModel) -> None:
        now = _utcnow()
        pipeline.state = "idle"
        pipeline.payment_method = "kakao_pay"
        pipeline.supplier_setting_id = None
        pipeline.supplier_name = ""
        pipeline.supplier_base_url = ""
        pipeline.supplier_cdk_key = ""
        pipeline.supplier_order_id = ""
        pipeline.supplier_customer_token = ""
        pipeline.supplier_poll_url = ""
        pipeline.supplier_status = ""
        pipeline.set_supplier_response({})
        pipeline.supplier_processing_started_at = None
        pipeline.supplier_deadline_at = None
        pipeline.payment_url = ""
        pipeline.scanner_setting_id = None
        pipeline.scanner_driver = "customer_api"
        pipeline.scanner_name = ""
        pipeline.scanner_base_url = ""
        pipeline.scanner_cdk_key = ""
        pipeline.scanner_order_id = ""
        pipeline.scanner_customer_token = ""
        pipeline.scanner_poll_url = ""
        pipeline.scanner_status = ""
        pipeline.set_scanner_response({})
        pipeline.scan_url = ""
        pipeline.scan_expires_at = ""
        pipeline.scanner_submit_attempts = 0
        pipeline.scanner_compensation_attempted = False
        pipeline.scanner_poll_failures = 0
        pipeline.scanner_recovery_reason = ""
        pipeline.scanner_recovery_check_count = 0
        pipeline.scanner_recovery_started_at = None
        pipeline.scanner_recovery_next_check_at = None
        pipeline.scanner_recovery_deadline_at = None
        pipeline.scanner_processing_started_at = None
        pipeline.scanner_deadline_at = None
        pipeline.plus_status = ""
        pipeline.final_result = ""
        pipeline.completion_source = ""
        pipeline.plus_check_count = 0
        pipeline.plus_check_started_at = None
        pipeline.plus_next_check_at = None
        pipeline.plus_check_deadline_at = None
        pipeline.plus_check_paused_at = None
        pipeline.codex_post_action_armed = False
        pipeline.codex_task_id = ""
        pipeline.codex_attempt_count = 0
        pipeline.codex_interrupted_retry_count = 0
        pipeline.codex_skipped_at = None
        pipeline.codex_enqueue_error = ""
        pipeline.codex_push_task_id = ""
        pipeline.codex_push_attempt_count = 0
        pipeline.codex_push_skip_reason = ""
        pipeline.codex_push_enqueue_error = ""
        pipeline.codex_post_action_done_at = None
        pipeline.last_error_code = ""
        pipeline.last_error_message = ""
        pipeline.set_events([])
        pipeline.completed_at = None
        pipeline.purged_at = now
        pipeline.updated_at = now

    @staticmethod
    def _post_action_run_key(pipeline: KakaoPipelineModel) -> str:
        anchor = pipeline.completed_at or pipeline.created_at or _utcnow()
        stamp = int(_as_utc(anchor).timestamp() * 1_000_000)
        return f"{int(pipeline.account_id)}_{int(pipeline.id or 0)}_{stamp}"

    @classmethod
    def _codex_post_action_task_id(cls, pipeline: KakaoPipelineModel, attempt: int) -> str:
        return f"kakao_codex_{cls._post_action_run_key(pipeline)}_{max(int(attempt), 1)}"

    @classmethod
    def _push_post_action_task_id(cls, pipeline: KakaoPipelineModel, attempt: int) -> str:
        return f"kakao_push_{cls._post_action_run_key(pipeline)}_{max(int(attempt), 1)}"

    @staticmethod
    def _post_actions_need_background(session: Session, pipeline: KakaoPipelineModel) -> bool:
        if (
            pipeline.archived_at is not None
            or pipeline.purged_at is not None
            or pipeline.state != "completed"
            or pipeline.final_result != "plus"
            or not bool(pipeline.codex_post_action_armed)
        ):
            return False

        auth = session.get(AccountCodexAuthModel, int(pipeline.account_id))
        authorized = _has_valid_codex_auth(auth)
        codex_task = session.get(TaskModel, pipeline.codex_task_id) if pipeline.codex_task_id else None
        if pipeline.codex_skipped_at and authorized:
            codex_ready = True
        elif not pipeline.codex_task_id or codex_task is None:
            return True
        elif codex_task.status in TASK_ACTIVE_STATUSES:
            return True
        elif codex_task.status == "interrupted":
            if _codex_auth_was_saved_for_task(auth, codex_task):
                return True
            return bool(
                codex_task.started_at is None
                and int(pipeline.codex_interrupted_retry_count or 0) < MAX_CODEX_INTERRUPTED_RETRIES
            )
        elif codex_task.status == "succeeded":
            codex_ready = bool(
                _task_account_result(codex_task, int(pipeline.account_id)).get("ok")
                and _codex_auth_was_saved_for_task(auth, codex_task)
            )
        else:
            codex_ready = False
        if not codex_ready:
            return False

        if pipeline.codex_push_skip_reason:
            return False
        if not pipeline.codex_push_task_id:
            return True
        push_task = session.get(TaskModel, pipeline.codex_push_task_id)
        return push_task is None or push_task.status in TASK_ACTIVE_STATUSES

    def _ensure_kakao_push_if_ready(self, account_id: int) -> dict:
        """Create the linked fifth-stage push only after verified Codex auth."""
        with _account_lock(account_id):
            candidate_task_id = ""
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if (
                    pipeline is None
                    or pipeline.state != "completed"
                    or pipeline.final_result != "plus"
                    or not pipeline.codex_post_action_armed
                ):
                    return self._serialize_pipeline(pipeline, detail=True)

                auth = session.get(AccountCodexAuthModel, int(account_id))
                if not _has_valid_codex_auth(auth):
                    return self._serialize_pipeline(pipeline, detail=True)

                codex_task = session.get(TaskModel, pipeline.codex_task_id) if pipeline.codex_task_id else None
                if pipeline.codex_skipped_at:
                    codex_ready = True
                elif codex_task and codex_task.status == "succeeded":
                    codex_ready = bool(
                        _task_account_result(codex_task, account_id).get("ok")
                        and _codex_auth_was_saved_for_task(auth, codex_task)
                    )
                elif (
                    codex_task
                    and codex_task.status == "interrupted"
                    and _codex_auth_was_saved_for_task(auth, codex_task)
                ):
                    # OAuth credentials may have been committed immediately
                    # before process shutdown.  Valid paired tokens make a
                    # browser retry both unnecessary and potentially harmful.
                    pipeline.codex_skipped_at = _utcnow()
                    pipeline.codex_enqueue_error = ""
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, "Codex credentials survived the interrupted task; browser retry skipped")
                    session.add(pipeline)
                    session.commit()
                    codex_ready = True
                else:
                    codex_ready = False
                if not codex_ready:
                    return self._serialize_pipeline(pipeline, detail=True)

                linked = (
                    session.get(TaskModel, pipeline.codex_push_task_id)
                    if pipeline.codex_push_task_id
                    else None
                )
                if linked is not None:
                    return self._serialize_pipeline(pipeline, detail=True)

                # A manual/generic delivery can win after automatic enqueue
                # was disabled, failed, or crashed before its task row was
                # committed.  Adopt it before considering another enqueue.
                delivery = session.exec(
                    select(AccountPushDeliveryModel)
                    .where(AccountPushDeliveryModel.account_id == int(account_id))
                    .where(AccountPushDeliveryModel.target_key == "nvtokens")
                    .where(AccountPushDeliveryModel.status == "success")
                    .order_by(AccountPushDeliveryModel.pushed_at.desc())
                ).first()
                if _delivery_covers_codex_auth(
                    delivery,
                    auth,
                    not_before=pipeline.completed_at,
                ):
                    pipeline.codex_push_task_id = ""
                    pipeline.codex_push_skip_reason = "already_delivered"
                    pipeline.codex_push_enqueue_error = ""
                    pipeline.codex_post_action_done_at = _utcnow()
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, "NexusVault already received the current Codex credentials")
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)

                if pipeline.codex_push_skip_reason:
                    return self._serialize_pipeline(pipeline, detail=True)
                if pipeline.codex_push_task_id:
                    candidate_task_id = pipeline.codex_push_task_id
                else:
                    push_setting = get_nvtokens_auto_push_state()
                    if not push_setting.get("enabled"):
                        pipeline.codex_push_skip_reason = str(
                            push_setting.get("reason") or "auto_push_disabled"
                        )[:120]
                        pipeline.codex_push_enqueue_error = ""
                        pipeline.codex_post_action_done_at = _utcnow()
                        pipeline.updated_at = _utcnow()
                        _append_event(pipeline, "NexusVault automatic push is disabled; push stage skipped")
                        session.add(pipeline)
                        session.commit()
                        return self._serialize_pipeline(pipeline, detail=True)

                    pipeline.codex_push_attempt_count = int(pipeline.codex_push_attempt_count or 0) + 1
                    candidate_task_id = self._push_post_action_task_id(
                        pipeline,
                        pipeline.codex_push_attempt_count,
                    )
                    # Persist the link before task creation.  A process crash
                    # between these commits is repaired with the same ID.
                    pipeline.codex_push_task_id = candidate_task_id
                    pipeline.codex_push_enqueue_error = ""
                    pipeline.codex_post_action_done_at = None
                    pipeline.updated_at = _utcnow()
                    session.add(pipeline)
                    session.commit()

            outcome = enqueue_nvtokens_push_after_codex_oauth(
                account_id,
                platform="chatgpt",
                task_id=candidate_task_id,
                source="kakao_pipeline",
            )
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                if outcome.get("enqueued"):
                    pipeline.codex_push_task_id = str(outcome.get("task_id") or candidate_task_id)
                    pipeline.codex_push_enqueue_error = ""
                    pipeline.codex_post_action_done_at = None
                    _append_event(pipeline, "NexusVault background push task created")
                elif outcome.get("reason") != "enqueue_failed":
                    pipeline.codex_push_task_id = ""
                    pipeline.codex_push_skip_reason = str(
                        outcome.get("reason") or "auto_push_disabled"
                    )[:120]
                    pipeline.codex_push_enqueue_error = ""
                    pipeline.codex_post_action_done_at = _utcnow()
                    _append_event(pipeline, "NexusVault automatic push is unavailable; push stage skipped")
                else:
                    pipeline.codex_push_enqueue_error = _redact_sensitive_error(
                        outcome.get("error") or "Failed to create NexusVault push task"
                    )
                    _append_event(pipeline, pipeline.codex_push_enqueue_error, level="warning")
                pipeline.updated_at = _utcnow()
                session.add(pipeline)
                session.commit()
            if outcome.get("enqueued"):
                from services.task_runtime import task_runtime

                task_runtime.wake_up()
            return self.get_account_pipeline(account_id)

    def start_codex(self, account_id: int, *, automatic: bool = False, repair: bool = False) -> dict:
        """Start/retry the Kakao page's single-account Codex stage."""
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                pipeline_plus = bool(
                    pipeline and pipeline.state == "completed" and pipeline.final_result == "plus"
                )
            if not pipeline_plus and not self._account_is_plus(account_id):
                raise ValueError("Codex authorization can only start after the account is Plus")

            candidate_task_id = ""
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id, create=True)
                assert pipeline is not None
                if not pipeline_plus:
                    pipeline.state = "completed"
                    pipeline.plus_status = "plus"
                    pipeline.final_result = "plus"
                    pipeline.completion_source = pipeline.completion_source or "existing_account_plus"
                    pipeline.completed_at = pipeline.completed_at or _utcnow()
                pipeline.codex_post_action_armed = True

                auth = session.get(AccountCodexAuthModel, int(account_id))
                linked = session.get(TaskModel, pipeline.codex_task_id) if pipeline.codex_task_id else None
                existing_auth_can_skip = bool(
                    _has_valid_codex_auth(auth)
                    and (
                        linked is None
                        or (
                            linked.status == "interrupted"
                            and _codex_auth_was_saved_for_task(auth, linked)
                        )
                    )
                )
                if existing_auth_can_skip:
                    pipeline.codex_skipped_at = pipeline.codex_skipped_at or _utcnow()
                    pipeline.codex_enqueue_error = ""
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, "Existing valid Codex authorization found; browser authorization skipped")
                    session.add(pipeline)
                    session.commit()
                    return self._ensure_kakao_push_if_ready(account_id)

                if linked and linked.status in TASK_ACTIVE_STATUSES:
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
                if linked and linked.status == "succeeded":
                    linked_ok = bool(
                        _task_account_result(linked, account_id).get("ok")
                        and _codex_auth_was_saved_for_task(auth, linked)
                    )
                    session.add(pipeline)
                    session.commit()
                    if linked_ok:
                        return self._ensure_kakao_push_if_ready(account_id)
                    if automatic:
                        return self._serialize_pipeline(pipeline, detail=True)

                missing_linked_task = bool(pipeline.codex_task_id and linked is None)
                interrupted_retry = bool(linked and linked.status == "interrupted")
                if linked and linked.status in {"failed", "cancelled"} and automatic:
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
                if interrupted_retry and automatic:
                    if (
                        not repair
                        or linked.started_at is not None
                        or int(pipeline.codex_interrupted_retry_count or 0) >= MAX_CODEX_INTERRUPTED_RETRIES
                    ):
                        session.add(pipeline)
                        session.commit()
                        return self._serialize_pipeline(pipeline, detail=True)
                    pipeline.codex_interrupted_retry_count = int(
                        pipeline.codex_interrupted_retry_count or 0
                    ) + 1

                if missing_linked_task:
                    # Crash repair uses the link that was committed before the
                    # missing task row; no attempt/ID change is needed.
                    candidate_task_id = pipeline.codex_task_id
                else:
                    pipeline.codex_attempt_count = int(pipeline.codex_attempt_count or 0) + 1
                    candidate_task_id = self._codex_post_action_task_id(
                        pipeline,
                        pipeline.codex_attempt_count,
                    )
                    pipeline.codex_task_id = candidate_task_id
                pipeline.codex_skipped_at = None
                pipeline.codex_enqueue_error = ""
                pipeline.codex_push_task_id = ""
                pipeline.codex_push_attempt_count = 0
                pipeline.codex_push_skip_reason = ""
                pipeline.codex_push_enqueue_error = ""
                pipeline.codex_post_action_done_at = None
                pipeline.updated_at = _utcnow()
                session.add(pipeline)
                session.commit()

            try:
                task = create_codex_oauth_batch_task(
                    platform="chatgpt",
                    account_ids=[int(account_id)],
                    concurrency=1,
                    auto_push_after_oauth=False,
                    task_id=candidate_task_id,
                    source="kakao_pipeline",
                )
            except Exception as exc:  # noqa: BLE001
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.codex_enqueue_error = _redact_sensitive_error(exc)
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, pipeline.codex_enqueue_error, level="warning")
                    session.add(pipeline)
                    session.commit()
                return self.get_account_pipeline(account_id)

            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                pipeline.codex_task_id = str(task.get("id") or candidate_task_id)
                pipeline.codex_enqueue_error = ""
                pipeline.updated_at = _utcnow()
                _append_event(pipeline, "Codex authorization task created")
                session.add(pipeline)
                session.commit()
            from services.task_runtime import task_runtime

            task_runtime.wake_up()
            return self.get_account_pipeline(account_id)

    def reconcile_codex_post_actions(self, account_id: int) -> dict:
        """Repair and advance an armed Kakao Codex/push post-action chain."""
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None:
                    return self._serialize_pipeline(pipeline, detail=True)
                if not self._post_actions_need_background(session, pipeline):
                    if (
                        pipeline.codex_post_action_armed
                        and pipeline.codex_post_action_done_at is None
                    ):
                        pipeline.codex_post_action_done_at = _utcnow()
                        pipeline.updated_at = _utcnow()
                        session.add(pipeline)
                        session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
                task = session.get(TaskModel, pipeline.codex_task_id) if pipeline.codex_task_id else None
                auth = session.get(AccountCodexAuthModel, int(account_id))
                codex_ready = bool(
                    (pipeline.codex_skipped_at and _has_valid_codex_auth(auth))
                    or (
                        task
                        and task.status == "succeeded"
                        and _task_account_result(task, account_id).get("ok")
                        and _codex_auth_was_saved_for_task(auth, task)
                    )
                    or (
                        task
                        and task.status == "interrupted"
                        and _codex_auth_was_saved_for_task(auth, task)
                    )
                )

            if codex_ready:
                result = self._ensure_kakao_push_if_ready(account_id)
            else:
                result = self.start_codex(account_id, automatic=True, repair=True)

            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is not None and self._post_actions_need_background(session, pipeline):
                    result = {**result, "_background_state": CODEX_POST_ACTION_STATE}
                elif pipeline is not None and pipeline.codex_post_action_done_at is None:
                    pipeline.codex_post_action_done_at = _utcnow()
                    pipeline.updated_at = _utcnow()
                    session.add(pipeline)
                    session.commit()
            return result

    def list_background_work(self, *, limit: int = 100) -> list[dict]:
        """Return persisted work that can safely resume without a browser page."""
        with Session(engine) as session:
            now = _utcnow()
            bounded_limit = min(max(int(limit), 1), 500)
            remote_rows = session.exec(
                select(KakaoPipelineModel)
                .where(KakaoPipelineModel.archived_at.is_(None))
                .where(KakaoPipelineModel.state.in_(REMOTE_BACKGROUND_POLL_STATES))
                .where(
                    or_(
                        KakaoPipelineModel.state != "plus_pending",
                        KakaoPipelineModel.plus_next_check_at.is_(None),
                        KakaoPipelineModel.plus_next_check_at <= now,
                    )
                )
                .where(
                    or_(
                        KakaoPipelineModel.state != "scanner_accepted_untracked",
                        KakaoPipelineModel.scanner_recovery_next_check_at.is_(None),
                        KakaoPipelineModel.scanner_recovery_next_check_at <= now,
                    )
                )
                .order_by(KakaoPipelineModel.updated_at, KakaoPipelineModel.id)
                .limit(bounded_limit)
            ).all()
            work = [
                {
                    "account_id": int(row.account_id),
                    "state": _text(row.state),
                }
                for row in remote_rows
            ]
            remaining = bounded_limit - len(work)
            if remaining > 0:
                post_rows = session.exec(
                    select(KakaoPipelineModel)
                    .where(KakaoPipelineModel.archived_at.is_(None))
                    .where(KakaoPipelineModel.state == "completed")
                    .where(KakaoPipelineModel.final_result == "plus")
                    .where(KakaoPipelineModel.codex_post_action_armed == True)  # noqa: E712
                    .where(KakaoPipelineModel.codex_post_action_done_at.is_(None))
                    .order_by(KakaoPipelineModel.updated_at, KakaoPipelineModel.id)
                    .limit(remaining)
                ).all()
                work.extend(
                    {
                        "account_id": int(row.account_id),
                        "state": CODEX_POST_ACTION_STATE,
                    }
                    for row in post_rows
                )
            return work

    def advance_background(self, account_id: int, *, expected_state: str = "") -> dict:
        """Advance one resumable state while sharing the manual-action account lock."""
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None:
                    return self._serialize_pipeline(None)
                state = _text(pipeline.state)
                if _text(expected_state) == CODEX_POST_ACTION_STATE:
                    if (
                        state != "completed"
                        or pipeline.final_result != "plus"
                        or not pipeline.codex_post_action_armed
                    ):
                        return self._serialize_pipeline(pipeline)
                    return self.reconcile_codex_post_actions(account_id)
                if expected_state and state != _text(expected_state):
                    return self._serialize_pipeline(pipeline)
                if state not in REMOTE_BACKGROUND_POLL_STATES:
                    return self._serialize_pipeline(pipeline)

            if state == "supplier_processing":
                with Session(engine) as session:
                    current = self._pipeline_for_account(session, account_id)
                    assert current is not None
                    self._ensure_supplier_processing_window(current)
                    if self._supplier_processing_expired(current):
                        _set_error(
                            current,
                            "supplier_poll_failed",
                            "supplier_processing_timeout",
                            "提链供应商处理超过 15 分钟，已停止自动查询",
                        )
                        session.add(current)
                        session.commit()
                        return self._serialize_pipeline(current, detail=True)
                    session.add(current)
                    session.commit()
                return self.poll_supplier(account_id)
            if state == "supplier_submitting":
                with Session(engine) as session:
                    current = self._pipeline_for_account(session, account_id)
                    if current is not None and _age_seconds(current.updated_at) < SUBMIT_RECOVERY_GRACE_SECONDS:
                        return self._serialize_pipeline(current)
                return self._mark_interrupted_supplier_submit(account_id)
            if state == "scanner_submitting":
                with Session(engine) as session:
                    current = self._pipeline_for_account(session, account_id)
                    if current is not None and _age_seconds(current.updated_at) < SUBMIT_RECOVERY_GRACE_SECONDS:
                        return self._serialize_pipeline(current)
                return self._resume_scanner_submit(account_id)
            if state == "scanner_processing":
                with Session(engine) as session:
                    current = self._pipeline_for_account(session, account_id)
                    assert current is not None
                    self._ensure_scanner_processing_window(current)
                    if self._scanner_processing_expired(current):
                        _set_error(
                            current,
                            "scanner_poll_failed",
                            "scanner_processing_timeout",
                            "扫码订单处理超过 30 分钟，已停止自动查询，可手动继续查询原订单",
                        )
                        session.add(current)
                        session.commit()
                        return self._serialize_pipeline(current, detail=True)
                    session.add(current)
                    session.commit()
                result = self.poll_scanner(account_id)
                if result.get("state") == "scanner_succeeded":
                    return self.check_plus(account_id, advance_pipeline=True)
                return result
            if state == "scanner_accepted_untracked":
                with Session(engine) as session:
                    current = self._pipeline_for_account(session, account_id)
                    assert current is not None
                    self._ensure_untracked_plus_window(current)
                    if self._untracked_plus_window_expired(current):
                        self._pause_untracked_plus_confirmation(current)
                        session.add(current)
                        session.commit()
                        return self._serialize_pipeline(current, detail=True)
                    if current.scanner_recovery_next_check_at and (
                        _as_utc(current.scanner_recovery_next_check_at) > _utcnow()
                    ):
                        session.add(current)
                        session.commit()
                        return self._serialize_pipeline(current)
                    session.add(current)
                    session.commit()
                return self.check_untracked_plus(account_id)
            if state in {"plus_pending", "plus_checking"}:
                with Session(engine) as session:
                    current = self._pipeline_for_account(session, account_id)
                    assert current is not None
                    self._ensure_plus_window(current)
                    if self._plus_window_expired(current):
                        self._pause_plus_confirmation(current)
                        session.add(current)
                        session.commit()
                        return self._serialize_pipeline(current, detail=True)
                    if current.plus_next_check_at:
                        next_at = current.plus_next_check_at
                        if next_at.tzinfo is None:
                            next_at = next_at.replace(tzinfo=timezone.utc)
                        if next_at > _utcnow():
                            return self._serialize_pipeline(current)
                return self.check_plus(account_id, advance_pipeline=True)
            return self.check_plus(account_id, advance_pipeline=True)

    def get_account_pipeline(self, account_id: int) -> dict:
        with Session(engine) as session:
            model = self._pipeline_for_account(session, account_id, allow_archived=True)
            if model is None:
                raise ValueError("账号还没有 Kakao 操作记录")
            return self._serialize_pipeline(model, detail=True)

    def set_codex_post_actions_enabled(self, account_id: int, enabled: bool) -> dict:
        """Persist which surface owns the optional Codex/push tail."""
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None:
                    raise ValueError("Kakao pipeline does not exist")
                if pipeline.state != "completed":
                    pipeline.codex_post_action_armed = bool(enabled)
                    pipeline.codex_post_action_done_at = None
                    pipeline.updated_at = _utcnow()
                    session.add(pipeline)
                    session.commit()
                return self._serialize_pipeline(pipeline, detail=True)

    def start_extraction(
        self,
        account_id: int,
        supplier_setting_id: int | None = None,
        payment_method: str = "kakao_pay",
        *,
        enable_post_actions: bool = False,
    ) -> dict:
        with _account_lock(account_id):
            self._ensure_account_pipeline_mutable(account_id)
            if self._account_is_plus(account_id):
                raise ValueError("当前账号已经是 Plus，无需再次提链扫码")
            _, access_token, _ = self._account_credentials(account_id)
            if not access_token:
                raise ValueError("提链供应商仍需要账号 access_token")
            setting, config = self._setting_payload("supplier")
            if supplier_setting_id and (not setting or int(setting.id or 0) != int(supplier_setting_id)):
                raise ValueError("选择的供应商配置不存在")
            method = _text(payment_method).lower() or "kakao_pay"
            if method not in {"kakao_pay", "naver_pay"}:
                raise ValueError("不支持的支付方式")
            client = CustomerApiClient(config["base_url"], config["cdk_key"])
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id, create=True)
                assert pipeline is not None
                if pipeline.state not in {"idle", "supplier_failed"}:
                    raise ValueError("当前账号已有 Kakao 流程记录，请先完成或重置后再提链")
                pipeline.state = "supplier_submitting"
                pipeline.payment_method = method
                pipeline.supplier_setting_id = int(setting.id or 0) if setting else None
                pipeline.supplier_name = config["display_name"]
                pipeline.supplier_base_url = config["base_url"]
                pipeline.supplier_cdk_key = config["cdk_key"]
                pipeline.supplier_order_id = ""
                pipeline.supplier_customer_token = ""
                pipeline.supplier_poll_url = ""
                pipeline.supplier_status = "SUBMITTING"
                pipeline.set_supplier_response({})
                pipeline.supplier_processing_started_at = None
                pipeline.supplier_deadline_at = None
                pipeline.payment_url = ""
                pipeline.scanner_setting_id = None
                pipeline.scanner_driver = "customer_api"
                pipeline.scanner_name = ""
                pipeline.scanner_base_url = ""
                pipeline.scanner_cdk_key = ""
                pipeline.scanner_order_id = ""
                pipeline.scanner_customer_token = ""
                pipeline.scanner_poll_url = ""
                pipeline.scanner_status = ""
                pipeline.set_scanner_response({})
                pipeline.scan_url = ""
                pipeline.scan_expires_at = ""
                pipeline.scanner_submit_attempts = 0
                pipeline.scanner_compensation_attempted = False
                pipeline.scanner_poll_failures = 0
                pipeline.scanner_recovery_reason = ""
                pipeline.scanner_recovery_check_count = 0
                pipeline.scanner_recovery_started_at = None
                pipeline.scanner_recovery_next_check_at = None
                pipeline.scanner_recovery_deadline_at = None
                pipeline.scanner_processing_started_at = None
                pipeline.scanner_deadline_at = None
                pipeline.plus_status = ""
                pipeline.final_result = ""
                pipeline.completion_source = ""
                pipeline.plus_check_count = 0
                pipeline.plus_check_started_at = None
                pipeline.plus_next_check_at = None
                pipeline.plus_check_deadline_at = None
                pipeline.plus_check_paused_at = None
                pipeline.codex_post_action_armed = bool(enable_post_actions)
                pipeline.codex_task_id = ""
                pipeline.codex_attempt_count = 0
                pipeline.codex_interrupted_retry_count = 0
                pipeline.codex_skipped_at = None
                pipeline.codex_enqueue_error = ""
                pipeline.codex_push_task_id = ""
                pipeline.codex_push_attempt_count = 0
                pipeline.codex_push_skip_reason = ""
                pipeline.codex_push_enqueue_error = ""
                pipeline.codex_post_action_done_at = None
                pipeline.last_error_code = ""
                pipeline.last_error_message = ""
                pipeline.completed_at = None
                pipeline.updated_at = _utcnow()
                _append_event(pipeline, f"已向 {config['display_name']} 提交提链请求")
                session.add(pipeline)
                session.commit()
            try:
                payload = client.create_extraction(access_token, payment_method=method)
                data = _data(payload)
                order = data.get("order") if isinstance(data.get("order"), dict) else {}
                order_id = _text(order.get("id"))
                customer_token = _text(data.get("customerToken"))
                poll_url = _text(data.get("pollUrl"))
                if not order_id or not customer_token or not poll_url:
                    raise ValueError("供应商创建订单响应缺少 order/customerToken/pollUrl")
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.supplier_order_id = order_id
                    pipeline.supplier_customer_token = customer_token
                    pipeline.supplier_poll_url = poll_url
                    pipeline.supplier_status = _text(order.get("status") or "PENDING").upper()
                    pipeline.state = "supplier_processing"
                    pipeline.set_supplier_response(payload)
                    pipeline.supplier_processing_started_at = _utcnow()
                    default_deadline = (
                        _as_utc(pipeline.supplier_processing_started_at)
                        + timedelta(seconds=SUPPLIER_PROCESSING_WINDOW_SECONDS)
                    )
                    pipeline.supplier_deadline_at = _earlier_deadline(
                        default_deadline,
                        _find_scan_value(data, {"expires_at", "expire_at", "expired_at"}),
                    )
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"供应商订单已创建: {order_id}")
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
            except Exception as exc:
                code = exc.code if isinstance(exc, CustomerApiProblem) else "supplier_submit_failed"
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    if _is_ambiguous_submit_problem(exc):
                        _set_error(
                            pipeline,
                            "supplier_submit_unconfirmed",
                            code,
                            f"提链提交结果无法确认，为避免重复订单已停止自动重试: {exc}",
                        )
                        pipeline.supplier_status = "UNCONFIRMED"
                    else:
                        _set_error(pipeline, "supplier_failed", code, str(exc))
                        pipeline.supplier_status = "FAILED"
                    session.add(pipeline)
                    session.commit()
                raise

    def poll_supplier(self, account_id: int) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None or not pipeline.supplier_order_id:
                    raise ValueError("当前账号没有供应商提链订单")
                manual_recovery = pipeline.state == "supplier_poll_failed"
                if pipeline.state not in {"supplier_processing", "supplier_poll_failed"}:
                    return self._serialize_pipeline(pipeline, detail=True)
                self._ensure_supplier_processing_window(pipeline)
                if not manual_recovery and self._supplier_processing_expired(pipeline):
                    _set_error(
                        pipeline,
                        "supplier_poll_failed",
                        "supplier_processing_timeout",
                        "提链供应商处理超过 15 分钟，已停止自动查询",
                    )
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
                client = CustomerApiClient(pipeline.supplier_base_url, pipeline.supplier_cdk_key)
                poll_url = pipeline.supplier_poll_url
                customer_token = pipeline.supplier_customer_token
            try:
                payload = client.get_order(poll_url, customer_token)
            except Exception as exc:
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.last_error_code = exc.code if isinstance(exc, CustomerApiProblem) else "supplier_poll_failed"
                    pipeline.last_error_message = str(exc)[:1000]
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"刷新供应商状态失败: {exc}", level="warning")
                    session.add(pipeline)
                    session.commit()
                raise

            data = _data(payload)
            status = _text(data.get("status") or "PENDING").upper()
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                if pipeline.state not in {"supplier_processing", "supplier_poll_failed"}:
                    return self._serialize_pipeline(pipeline, detail=True)
                previous = pipeline.supplier_status
                pipeline.supplier_status = status
                pipeline.set_supplier_response(payload)
                remote_deadline = _find_scan_value(data, {"expires_at", "expire_at", "expired_at"})
                if pipeline.supplier_deadline_at and remote_deadline:
                    pipeline.supplier_deadline_at = _earlier_deadline(pipeline.supplier_deadline_at, remote_deadline)
                pipeline.updated_at = _utcnow()
                if previous != status:
                    _append_event(pipeline, f"供应商状态: {status}")
                if status == "READY":
                    qualification = data.get("qualification") if isinstance(data.get("qualification"), dict) else {}
                    extraction = data.get("extraction") if isinstance(data.get("extraction"), dict) else {}
                    zero_verified = qualification.get("zeroVerified") is True
                    promo = qualification.get("postPromoAmountKrw")
                    tax = qualification.get("postTaxAmountKrw")
                    amounts_ok = (promo in (None, 0)) and (tax in (None, 0))
                    try:
                        payment_url = _valid_payment_url(_text(extraction.get("paymentUrl")))
                    except ValueError as exc:
                        _set_error(pipeline, "supplier_failed", "invalid_payment_url", str(exc))
                    else:
                        if not zero_verified or not amounts_ok:
                            _set_error(pipeline, "supplier_failed", "zero_amount_not_verified", "供应商未完成 0 KRW 双重校验")
                        else:
                            pipeline.payment_url = payment_url
                            pipeline.state = "link_ready"
                            pipeline.last_error_code = ""
                            pipeline.last_error_message = ""
                            _append_event(
                                pipeline,
                                "Kakao 长链提取成功，准备自动上传扫码"
                                if self._auto_upload_after_extract()
                                else "Kakao 长链提取成功，等待人工上传扫码",
                            )
                elif status in TERMINAL_REMOTE_FAILURES:
                    code, message = _problem_from_payload(data, "supplier_failed", "供应商提链失败")
                    _set_error(pipeline, "supplier_failed", code, message)
                else:
                    if self._supplier_processing_expired(pipeline):
                        _set_error(
                            pipeline,
                            "supplier_poll_failed",
                            "supplier_processing_timeout",
                            "提链订单仍在处理中且已超过 15 分钟，可稍后手动继续查询原订单",
                        )
                    else:
                        pipeline.state = "supplier_processing"
                session.add(pipeline)
                session.commit()
                result = self._serialize_pipeline(pipeline, detail=True)
                return self._maybe_auto_submit_scanner(account_id, result)

    def submit_scanner(
        self,
        account_id: int,
        scanner_setting_id: int | None = None,
        scanner_kind: str = "",
    ) -> dict:
        with _account_lock(account_id):
            kind = _text(scanner_kind) or self._default_scanner_kind()
            if kind not in {"scanner", "scanner_546789"}:
                raise ValueError("未知扫码供应商")
            setting, config = self._setting_payload(kind)
            if scanner_setting_id and (not setting or int(setting.id or 0) != int(scanner_setting_id)):
                raise ValueError("选择的扫码配置不存在")
            if not config["cdk_key"]:
                raise ValueError(f"{config['display_name']} 没有可用 CDK")
            access_token = ""
            session_cookie = ""
            if config["driver_type"] == "customer_api":
                _, access_token, session_cookie = self._account_credentials(account_id)
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None or pipeline.state not in {"link_ready", "scanner_failed"} or not pipeline.payment_url:
                    raise ValueError("必须先成功提取 Kakao 长链")
                payment_url = pipeline.payment_url
                payment_method = pipeline.payment_method
                pipeline.state = "scanner_submitting"
                pipeline.scanner_setting_id = int(setting.id or 0) if setting else None
                pipeline.scanner_driver = config["driver_type"]
                pipeline.scanner_name = config["display_name"]
                pipeline.scanner_base_url = config["base_url"]
                pipeline.scanner_cdk_key = config["cdk_key"]
                pipeline.scanner_status = "SUBMITTING"
                pipeline.scanner_order_id = ""
                pipeline.scanner_customer_token = ""
                pipeline.scanner_poll_url = ""
                pipeline.set_scanner_response({})
                pipeline.scan_url = ""
                pipeline.scan_expires_at = ""
                pipeline.scanner_submit_attempts = 0
                pipeline.scanner_compensation_attempted = False
                pipeline.scanner_poll_failures = 0
                pipeline.scanner_recovery_reason = ""
                pipeline.scanner_recovery_check_count = 0
                pipeline.scanner_recovery_started_at = None
                pipeline.scanner_recovery_next_check_at = None
                pipeline.scanner_recovery_deadline_at = None
                pipeline.scanner_processing_started_at = None
                pipeline.scanner_deadline_at = None
                pipeline.plus_status = ""
                pipeline.final_result = ""
                pipeline.completion_source = ""
                pipeline.plus_check_count = 0
                pipeline.plus_check_started_at = None
                pipeline.plus_next_check_at = None
                pipeline.plus_check_deadline_at = None
                pipeline.plus_check_paused_at = None
                pipeline.last_error_code = ""
                pipeline.last_error_message = ""
                pipeline.updated_at = _utcnow()
                session.add(pipeline)
                session.commit()
            if config["driver_type"] == "payment_submission":
                return self._submit_workstation_scanner(account_id, compensation=False)

            try:
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.scanner_submit_attempts = 1
                    _append_event(pipeline, f"已向 {config['display_name']} 上传扫码任务")
                    session.add(pipeline)
                    session.commit()
                payload = CustomerApiClient(config["base_url"], config["cdk_key"]).create_scanner(
                    access_token,
                    payment_url,
                    payment_method=payment_method,
                    session_cookie=session_cookie,
                )
                data = _data(payload)
                order = data.get("order") if isinstance(data.get("order"), dict) else {}
                order_id = _text(order.get("id"))
                customer_token = _text(data.get("customerToken"))
                poll_url = _text(data.get("pollUrl"))
                if not order_id or not customer_token or not poll_url:
                    raise ValueError("扫码接口响应缺少 order/customerToken/pollUrl")
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.scanner_order_id = order_id
                    pipeline.scanner_customer_token = customer_token
                    pipeline.scanner_poll_url = poll_url
                    pipeline.scanner_status = _text(order.get("status") or "PENDING").upper()
                    pipeline.state = "scanner_processing"
                    pipeline.set_scanner_response(payload)
                    pipeline.scanner_processing_started_at = _utcnow()
                    default_deadline = (
                        _as_utc(pipeline.scanner_processing_started_at)
                        + timedelta(seconds=SCANNER_PROCESSING_WINDOW_SECONDS)
                    )
                    pipeline.scanner_deadline_at = _earlier_deadline(
                        default_deadline,
                        _find_scan_value(data, {"expires_at", "expire_at", "expired_at"}),
                    )
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"扫码订单已创建: {order_id}")
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
            except Exception as exc:
                code = exc.code if isinstance(exc, CustomerApiProblem) else "scanner_submit_failed"
                depleted = _is_cdk_depleted(code, str(exc))
                if depleted:
                    self._remove_cdks(kind, [config["cdk_key"]])
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    message = str(exc)
                    if depleted:
                        message = f"{message}；已删除用完的 CDK，可使用池中下一条重新上传"
                    if _is_ambiguous_submit_problem(exc):
                        self._start_untracked_plus_confirmation(
                            pipeline,
                            scanner_status="SUBMIT_UNCONFIRMED",
                            reason=code,
                            event_message="扫码提交结果不确定，停止重复上传并转为 30 分钟 Plus 观察",
                        )
                    else:
                        _set_error(pipeline, "scanner_failed", code, message)
                        pipeline.scanner_status = "FAILED"
                    session.add(pipeline)
                    session.commit()
                    result = self._serialize_pipeline(pipeline, detail=True)
                if _is_ambiguous_submit_problem(exc):
                    return result
                raise

    def _submit_workstation_scanner(self, account_id: int, *, compensation: bool, reason: str = "") -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None or pipeline.scanner_driver != "payment_submission" or not pipeline.payment_url:
                    raise ValueError("当前账号没有可补偿的 546789 扫码任务")
                if compensation and pipeline.scanner_compensation_attempted:
                    self._start_untracked_plus_confirmation(
                        pipeline,
                        scanner_status="SUBMIT_UNCONFIRMED",
                        reason="compensation_already_used",
                        event_message="扫码订单无法追踪且补偿已使用，停止提交并转为 30 分钟 Plus 观察",
                    )
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
                if compensation:
                    pipeline.scanner_compensation_attempted = True
                    pipeline.scanner_recovery_reason = _text(reason) or "submission_untracked"
                    pipeline.scanner_status = "COMPENSATING"
                    _append_event(pipeline, "扫码任务无法追踪，立即执行唯一一次补偿提交", level="warning")
                else:
                    pipeline.scanner_status = "SUBMITTING"
                    _append_event(pipeline, f"已向 {pipeline.scanner_name or '546789 扫码平台'} 上传扫码任务")
                pipeline.scanner_submit_attempts = int(pipeline.scanner_submit_attempts or 0) + 1
                pipeline.state = "scanner_submitting"
                pipeline.updated_at = _utcnow()
                base_url = pipeline.scanner_base_url
                cdk_key = pipeline.scanner_cdk_key
                payment_url = pipeline.payment_url
                session.add(pipeline)
                session.commit()

            try:
                client = WorkstationScannerClient(base_url, cdk_key)
                payload = client.submit_payment(payment_url)
                submissions = payload.get("submissions") if isinstance(payload.get("submissions"), list) else []
                order = submissions[0] if submissions and isinstance(submissions[0], dict) else {}
                order_id = _text(order.get("id"))
                if not order_id:
                    raise CustomerApiProblem(502, "missing_submission_id", "546789 扫码接口响应缺少 submission ID")
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.scanner_order_id = order_id
                    pipeline.scanner_customer_token = ""
                    pipeline.scanner_poll_url = ""
                    pipeline.scanner_status = _text(order.get("state") or "PENDING").upper()
                    pipeline.state = "scanner_processing"
                    pipeline.set_scanner_response(payload)
                    pipeline.scan_url = client.qr_url(order_id)
                    pipeline.scan_expires_at = ""
                    pipeline.scanner_poll_failures = 0
                    pipeline.scanner_processing_started_at = _utcnow()
                    default_deadline = (
                        _as_utc(pipeline.scanner_processing_started_at)
                        + timedelta(seconds=SCANNER_PROCESSING_WINDOW_SECONDS)
                    )
                    pipeline.scanner_deadline_at = _earlier_deadline(
                        default_deadline,
                        _find_scan_value(order, {"expires_at", "expire_at", "expired_at"}),
                    )
                    pipeline.last_error_code = ""
                    pipeline.last_error_message = ""
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"扫码订单已创建: {order_id}")
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
            except Exception as exc:
                if compensation and _is_duplicate_payment_submission(exc):
                    with Session(engine) as session:
                        pipeline = self._pipeline_for_account(session, account_id)
                        assert pipeline is not None
                        self._start_untracked_plus_confirmation(
                            pipeline,
                            scanner_status="DUPLICATE_ACCEPTED",
                            reason="duplicate_submission",
                            event_message="补偿提交确认上游已接收支付链接，转为无单号 Plus 确认",
                        )
                        session.add(pipeline)
                        session.commit()
                        return self._serialize_pipeline(pipeline, detail=True)

                code = exc.code if isinstance(exc, CustomerApiProblem) else "scanner_submit_failed"
                if _is_workstation_cdk_depleted(code, str(exc)):
                    self._remove_cdks("scanner_546789", [cdk_key])
                    with Session(engine) as session:
                        pipeline = self._pipeline_for_account(session, account_id)
                        assert pipeline is not None
                        _set_error(pipeline, "scanner_failed", code, f"{exc}；已删除用完的 CDK，可使用池中下一条重新上传")
                        pipeline.scanner_status = "FAILED"
                        session.add(pipeline)
                        session.commit()
                    raise

                if not compensation and _is_ambiguous_submit_problem(exc):
                    with Session(engine) as session:
                        pipeline = self._pipeline_for_account(session, account_id)
                        assert pipeline is not None
                        pipeline.last_error_code = code
                        pipeline.last_error_message = str(exc)[:1000]
                        _append_event(pipeline, f"首次提交结果不确定: {exc}", level="warning")
                        session.add(pipeline)
                        session.commit()
                    return self._submit_workstation_scanner(account_id, compensation=True, reason=code)

                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    if compensation:
                        if _is_ambiguous_submit_problem(exc):
                            self._start_untracked_plus_confirmation(
                                pipeline,
                                scanner_status="SUBMIT_UNCONFIRMED",
                                reason=code,
                                event_message="两次扫码提交结果均无法确认，停止提交并转为 30 分钟 Plus 观察",
                            )
                        else:
                            _set_error(pipeline, "scanner_submit_unconfirmed", code, f"唯一一次补偿提交仍未确认: {exc}")
                            pipeline.scanner_status = "UNCONFIRMED"
                            pipeline.scan_url = ""
                            pipeline.scan_expires_at = ""
                    else:
                        _set_error(pipeline, "scanner_failed", code, str(exc))
                        pipeline.scanner_status = "FAILED"
                    session.add(pipeline)
                    session.commit()
                    result = self._serialize_pipeline(pipeline, detail=True)
                if not compensation:
                    raise
                return result

    def _resume_scanner_submit(self, account_id: int) -> dict:
        with Session(engine) as session:
            pipeline = self._pipeline_for_account(session, account_id)
            if pipeline is None:
                return self._serialize_pipeline(None)
            if pipeline.scanner_driver != "payment_submission":
                self._start_untracked_plus_confirmation(
                    pipeline,
                    scanner_status="SUBMIT_UNCONFIRMED",
                    reason="scanner_submit_interrupted",
                    event_message="扫码提交被服务重启中断，停止重复上传并转为 30 分钟 Plus 观察",
                )
                session.add(pipeline)
                session.commit()
                return self._serialize_pipeline(pipeline, detail=True)
            already_compensated = bool(pipeline.scanner_compensation_attempted)
        if not already_compensated:
            return self._submit_workstation_scanner(account_id, compensation=True, reason="process_interrupted")
        with Session(engine) as session:
            pipeline = self._pipeline_for_account(session, account_id)
            assert pipeline is not None
            self._start_untracked_plus_confirmation(
                pipeline,
                scanner_status="SUBMIT_UNCONFIRMED",
                reason="compensation_interrupted",
                event_message="补偿提交被服务重启中断，停止再次提交并转为 30 分钟 Plus 观察",
            )
            session.add(pipeline)
            session.commit()
            return self._serialize_pipeline(pipeline, detail=True)

    @staticmethod
    def _ensure_supplier_processing_window(pipeline: KakaoPipelineModel) -> None:
        if pipeline.supplier_processing_started_at is None:
            pipeline.supplier_processing_started_at = pipeline.updated_at or _utcnow()
        expected_deadline = _as_utc(pipeline.supplier_processing_started_at) + timedelta(
            seconds=SUPPLIER_PROCESSING_WINDOW_SECONDS
        )
        if pipeline.supplier_deadline_at is None or _as_utc(pipeline.supplier_deadline_at) > expected_deadline:
            pipeline.supplier_deadline_at = expected_deadline

    @staticmethod
    def _supplier_processing_expired(pipeline: KakaoPipelineModel) -> bool:
        return bool(pipeline.supplier_deadline_at and _as_utc(pipeline.supplier_deadline_at) <= _utcnow())

    @staticmethod
    def _ensure_scanner_processing_window(pipeline: KakaoPipelineModel) -> None:
        if pipeline.scanner_processing_started_at is None:
            pipeline.scanner_processing_started_at = pipeline.updated_at or _utcnow()
        expected_deadline = _as_utc(pipeline.scanner_processing_started_at) + timedelta(
            seconds=SCANNER_PROCESSING_WINDOW_SECONDS
        )
        if pipeline.scanner_deadline_at is None or _as_utc(pipeline.scanner_deadline_at) > expected_deadline:
            pipeline.scanner_deadline_at = expected_deadline

    @staticmethod
    def _scanner_processing_expired(pipeline: KakaoPipelineModel) -> bool:
        return bool(pipeline.scanner_deadline_at and _as_utc(pipeline.scanner_deadline_at) <= _utcnow())

    @staticmethod
    def _start_untracked_plus_confirmation(
        pipeline: KakaoPipelineModel,
        *,
        scanner_status: str,
        reason: str,
        event_message: str,
    ) -> None:
        started_at = _utcnow()
        deadline_at = started_at + timedelta(seconds=UNTRACKED_PLUS_WINDOW_SECONDS)
        pipeline.state = "scanner_accepted_untracked"
        pipeline.scanner_status = scanner_status
        pipeline.scanner_order_id = ""
        pipeline.scanner_customer_token = ""
        pipeline.scanner_poll_url = ""
        pipeline.scan_url = ""
        pipeline.scan_expires_at = ""
        pipeline.scanner_recovery_reason = _text(reason) or "submission_untracked"
        pipeline.scanner_recovery_started_at = started_at
        pipeline.scanner_recovery_check_count = 0
        pipeline.scanner_recovery_next_check_at = min(
            started_at + timedelta(seconds=UNTRACKED_PLUS_INITIAL_DELAY_SECONDS),
            deadline_at,
        )
        pipeline.scanner_recovery_deadline_at = deadline_at
        pipeline.scanner_processing_started_at = None
        pipeline.scanner_deadline_at = None
        pipeline.plus_status = "waiting"
        pipeline.last_error_code = ""
        pipeline.last_error_message = ""
        pipeline.updated_at = started_at
        _append_event(pipeline, event_message, level="warning")

    def _mark_interrupted_supplier_submit(self, account_id: int) -> dict:
        with Session(engine) as session:
            pipeline = self._pipeline_for_account(session, account_id)
            if pipeline is None:
                return self._serialize_pipeline(None)
            if pipeline.state == "supplier_submitting":
                _set_error(
                    pipeline,
                    "supplier_submit_unconfirmed",
                    "supplier_submit_interrupted",
                    "提链提交被服务重启中断，结果无法确认；为避免重复订单已停止自动重试",
                )
                pipeline.supplier_status = "UNCONFIRMED"
                session.add(pipeline)
                session.commit()
            return self._serialize_pipeline(pipeline, detail=True)

    def poll_scanner(self, account_id: int) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None or not pipeline.scanner_order_id:
                    raise ValueError("当前账号没有扫码订单")
                manual_recovery = pipeline.state == "scanner_poll_failed"
                if pipeline.state not in {"scanner_processing", "scanner_poll_failed"}:
                    return self._serialize_pipeline(pipeline, detail=True)
                self._ensure_scanner_processing_window(pipeline)
                if not manual_recovery and self._scanner_processing_expired(pipeline):
                    _set_error(
                        pipeline,
                        "scanner_poll_failed",
                        "scanner_processing_timeout",
                        "扫码订单处理超过 30 分钟，已停止自动查询，可手动继续查询原订单",
                    )
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
                scanner_driver = pipeline.scanner_driver or "customer_api"
                poll_url = pipeline.scanner_poll_url
                customer_token = pipeline.scanner_customer_token
            try:
                if scanner_driver == "payment_submission":
                    payload = WorkstationScannerClient(pipeline.scanner_base_url).get_submission(pipeline.scanner_order_id)
                else:
                    payload = CustomerApiClient(pipeline.scanner_base_url, pipeline.scanner_cdk_key).get_order(poll_url, customer_token)
            except Exception as exc:
                if scanner_driver == "payment_submission" and _is_missing_submission_problem(exc):
                    return self._submit_workstation_scanner(account_id, compensation=True, reason="submission_missing")
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    if pipeline.state not in {"scanner_processing", "scanner_poll_failed"}:
                        return self._serialize_pipeline(pipeline, detail=True)
                    pipeline.scanner_poll_failures = int(pipeline.scanner_poll_failures or 0) + 1
                    pipeline.last_error_code = exc.code if isinstance(exc, CustomerApiProblem) else "scanner_poll_failed"
                    pipeline.last_error_message = str(exc)[:1000]
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, f"刷新扫码状态失败: {exc}", level="warning")
                    transient = _is_transient_poll_problem(exc)
                    if not transient or pipeline.scanner_poll_failures >= MAX_SCANNER_POLL_FAILURES:
                        _set_error(
                            pipeline,
                            "scanner_poll_failed",
                            pipeline.last_error_code,
                            f"扫码状态连续查询失败，已停止自动轮询: {exc}" if transient else str(exc),
                        )
                    session.add(pipeline)
                    session.commit()
                    if pipeline.state == "scanner_poll_failed":
                        return self._serialize_pipeline(pipeline, detail=True)
                raise

            data = _data(payload)
            if scanner_driver == "payment_submission":
                status = _text(data.get("state") or "PENDING").upper()
                outcome = _workstation_outcome(status)
            else:
                status = _text(data.get("status") or "PENDING").upper()
                outcome = _scanner_outcome(data)
            scan_url = _safe_scan_url(
                _find_scan_value(data.get("payment") or data, {"qr_url", "qrcode_url", "qr_code", "scan_url"})
            )
            expires_at = _find_scan_value(data, {"expires_at", "expire_at", "expired_at"})
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                if pipeline.state not in {"scanner_processing", "scanner_poll_failed"}:
                    return self._serialize_pipeline(pipeline, detail=True)
                previous = pipeline.scanner_status
                pipeline.scanner_status = status
                pipeline.set_scanner_response(payload)
                pipeline.scan_url = scan_url or pipeline.scan_url
                pipeline.scan_expires_at = expires_at or pipeline.scan_expires_at
                if pipeline.scanner_deadline_at and expires_at:
                    pipeline.scanner_deadline_at = _earlier_deadline(pipeline.scanner_deadline_at, expires_at)
                pipeline.updated_at = _utcnow()
                if previous != status:
                    _append_event(pipeline, f"扫码状态: {status}")
                if outcome == "success":
                    pipeline.scanner_poll_failures = 0
                    pipeline.state = "scanner_succeeded"
                    pipeline.last_error_code = ""
                    pipeline.last_error_message = ""
                    _append_event(pipeline, "扫码平台返回成功，等待本地 Plus 复检")
                elif outcome == "missing":
                    pipeline.scanner_poll_failures = 0
                    session.add(pipeline)
                    session.commit()
                    return self._submit_workstation_scanner(account_id, compensation=True, reason="unknown_submission")
                elif outcome == "failed":
                    pipeline.scanner_poll_failures = 0
                    if scanner_driver == "payment_submission":
                        code = "payment_link_expired" if status == "EXPIRED" else "scanner_failed"
                        message = "支付链接已失效" if status == "EXPIRED" else f"546789 扫码任务失败: {status}"
                    else:
                        code, message = _problem_from_payload(data, "scanner_failed", f"扫码任务失败: {status}")
                    if scanner_driver == "customer_api" and _is_cdk_depleted(code, message):
                        self._remove_cdks("scanner", [pipeline.scanner_cdk_key])
                        message = f"{message}；已删除用完的 CDK，可使用池中下一条重新上传"
                    _set_error(pipeline, "scanner_failed", code, message)
                elif outcome == "processing":
                    pipeline.scanner_poll_failures = 0
                    if self._scanner_processing_expired(pipeline):
                        _set_error(
                            pipeline,
                            "scanner_poll_failed",
                            "scanner_processing_timeout",
                            "扫码订单仍在处理中且已超过 30 分钟，可稍后手动继续查询原订单",
                        )
                    else:
                        pipeline.state = "scanner_processing"
                else:
                    pipeline.scanner_poll_failures = int(pipeline.scanner_poll_failures or 0) + 1
                    pipeline.last_error_code = "scanner_unknown_status"
                    pipeline.last_error_message = f"扫码平台返回未识别状态: {status}"[:1000]
                    _append_event(pipeline, pipeline.last_error_message, level="warning")
                    if pipeline.scanner_poll_failures >= MAX_SCANNER_POLL_FAILURES:
                        _set_error(pipeline, "scanner_poll_failed", "scanner_unknown_status", pipeline.last_error_message)
                    else:
                        pipeline.state = "scanner_processing"
                session.add(pipeline)
                session.commit()
                return self._serialize_pipeline(pipeline, detail=True)

    @staticmethod
    def _ensure_plus_window(pipeline: KakaoPipelineModel) -> None:
        if pipeline.plus_check_started_at is None:
            pipeline.plus_check_started_at = pipeline.updated_at or _utcnow()
        expected_deadline = _as_utc(pipeline.plus_check_started_at) + timedelta(
            seconds=PLUS_CONFIRM_WINDOW_SECONDS
        )
        if (
            pipeline.plus_check_deadline_at is None
            or _as_utc(pipeline.plus_check_deadline_at) > expected_deadline
        ):
            pipeline.plus_check_deadline_at = expected_deadline

    @staticmethod
    def _plus_window_expired(pipeline: KakaoPipelineModel) -> bool:
        if pipeline.plus_check_deadline_at is None:
            return False
        return _as_utc(pipeline.plus_check_deadline_at) <= _utcnow()

    @staticmethod
    def _pause_plus_confirmation(pipeline: KakaoPipelineModel, message: str = "") -> None:
        pipeline.state = "plus_unconfirmed"
        pipeline.plus_status = pipeline.plus_status or "unconfirmed"
        pipeline.final_result = "not_plus"
        pipeline.plus_next_check_at = None
        pipeline.plus_check_paused_at = _utcnow()
        pipeline.last_error_code = "plus_unconfirmed"
        pipeline.last_error_message = message or "扫码平台已完成，但 10 分钟内尚未确认 Plus"
        pipeline.updated_at = _utcnow()
        _append_event(pipeline, pipeline.last_error_message, level="warning")

    @staticmethod
    def _ensure_untracked_plus_window(pipeline: KakaoPipelineModel) -> None:
        if pipeline.scanner_recovery_started_at is None:
            pipeline.scanner_recovery_started_at = pipeline.updated_at or _utcnow()
        expected_deadline = _as_utc(pipeline.scanner_recovery_started_at) + timedelta(
            seconds=UNTRACKED_PLUS_WINDOW_SECONDS
        )
        if (
            pipeline.scanner_recovery_deadline_at is None
            or _as_utc(pipeline.scanner_recovery_deadline_at) > expected_deadline
        ):
            pipeline.scanner_recovery_deadline_at = expected_deadline
        if pipeline.scanner_recovery_next_check_at is None:
            if int(pipeline.scanner_recovery_check_count or 0) == 0:
                pipeline.scanner_recovery_next_check_at = min(
                    _as_utc(pipeline.scanner_recovery_started_at)
                    + timedelta(seconds=UNTRACKED_PLUS_INITIAL_DELAY_SECONDS),
                    _as_utc(pipeline.scanner_recovery_deadline_at),
                )
            else:
                pipeline.scanner_recovery_next_check_at = _utcnow()

    @staticmethod
    def _untracked_plus_window_expired(pipeline: KakaoPipelineModel) -> bool:
        if pipeline.scanner_recovery_deadline_at is None:
            return False
        return _as_utc(pipeline.scanner_recovery_deadline_at) <= _utcnow()

    @staticmethod
    def _pause_untracked_plus_confirmation(pipeline: KakaoPipelineModel, message: str = "") -> None:
        pipeline.state = "scanner_recovery_unconfirmed"
        pipeline.plus_status = pipeline.plus_status or "unconfirmed"
        pipeline.final_result = "not_plus"
        pipeline.scanner_recovery_next_check_at = None
        pipeline.last_error_code = "untracked_plus_unconfirmed"
        pipeline.last_error_message = message or "上游已接收链接，但 30 分钟内尚未确认 Plus"
        pipeline.updated_at = _utcnow()
        _append_event(pipeline, pipeline.last_error_message, level="warning")

    def check_plus(
        self,
        account_id: int,
        *,
        advance_pipeline: bool = False,
        enable_post_actions: bool | None = None,
    ) -> dict:
        with _account_lock(account_id):
            check_started_at = _utcnow()
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id, create=True)
                assert pipeline is not None
                preserve_completed = pipeline.state == "completed"
                if preserve_completed:
                    advance_pipeline = False
                elif enable_post_actions is not None:
                    pipeline.codex_post_action_armed = bool(enable_post_actions)
                    pipeline.codex_post_action_done_at = None
                if advance_pipeline and pipeline.state in {
                    "scanner_accepted_untracked",
                    "scanner_recovery_unconfirmed",
                    "scanner_submit_unconfirmed",
                }:
                    session.add(pipeline)
                    session.commit()
                    return self.check_untracked_plus(account_id)
                if advance_pipeline and pipeline.state not in {
                    "scanner_succeeded",
                    "plus_checking",
                    "plus_pending",
                    "plus_check_failed",
                    "plus_unconfirmed",
                }:
                    raise ValueError("当前流水线状态不能推进 Plus 复检")
                if not preserve_completed:
                    pipeline.plus_status = "checking"
                if advance_pipeline:
                    manual_paused_check = pipeline.state == "plus_unconfirmed"
                    self._ensure_plus_window(pipeline)
                    pipeline.state = "plus_unconfirmed" if manual_paused_check else "plus_checking"
                    pipeline.last_error_code = ""
                    pipeline.last_error_message = ""
                else:
                    manual_paused_check = False
                pipeline.updated_at = _utcnow()
                _append_event(
                    pipeline,
                    (
                        "开始人工检测暂停任务的 Plus 状态"
                        if manual_paused_check
                        else ("开始流水线 Plus 复检" if advance_pipeline else "开始人工检测账号 Plus 状态")
                    ),
                )
                session.add(pipeline)
                session.commit()
            result, auth_recovery = self._query_account_state_with_relogin(account_id)
            if not result.ok:
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    if preserve_completed:
                        pipeline.updated_at = _utcnow()
                        _append_event(pipeline, result.error or "Plus 检测失败", level="warning")
                        session.add(pipeline)
                        session.commit()
                        return self._serialize_pipeline(pipeline, detail=True)
                    pipeline.plus_status = "error"
                    if advance_pipeline:
                        if auth_recovery["recovery_failed"]:
                            self._pause_plus_confirmation(
                                pipeline,
                                result.error or "账号失效且自动重新登录失败，Plus 确认已暂停",
                            )
                            pipeline.last_error_code = "plus_relogin_failed"
                        elif manual_paused_check:
                            pipeline.state = "plus_unconfirmed"
                            pipeline.last_error_code = "plus_check_failed"
                            pipeline.last_error_message = (result.error or "Plus 检测失败")[:1000]
                            pipeline.updated_at = _utcnow()
                            _append_event(pipeline, pipeline.last_error_message, level="warning")
                        else:
                            self._ensure_plus_window(pipeline)
                            pipeline.plus_check_count = int(pipeline.plus_check_count or 0) + 1
                            if self._plus_window_expired(pipeline):
                                self._pause_plus_confirmation(pipeline, "Plus 查询持续失败，10 分钟确认窗口已结束")
                            else:
                                pipeline.state = "plus_pending"
                                pipeline.plus_next_check_at = min(
                                    _utcnow() + timedelta(
                                        seconds=_next_plus_delay_seconds(pipeline.plus_check_count)
                                    ),
                                    _as_utc(pipeline.plus_check_deadline_at),
                                )
                                pipeline.last_error_code = "plus_check_failed"
                                pipeline.last_error_message = (result.error or "Plus 检测失败")[:1000]
                                pipeline.updated_at = _utcnow()
                                _append_event(pipeline, pipeline.last_error_message, level="warning")
                    else:
                        pipeline.updated_at = _utcnow()
                        _append_event(pipeline, result.error or "Plus 检测失败", level="error")
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)

            account = self.accounts.get_account(int(account_id)) or {}
            view = account.get("account_view") if isinstance(account.get("account_view"), dict) else {}
            subscription = view.get("subscription") if isinstance(view.get("subscription"), dict) else {}
            status_view = view.get("status") if isinstance(view.get("status"), dict) else {}
            checked_at = _parse_datetime(status_view.get("checked_at") or account.get("checked_at"))
            fresh_check = bool(checked_at and checked_at.astimezone(timezone.utc) >= check_started_at)
            plan = _text(subscription.get("plan") or account.get("plan_name")).lower()
            plan_state = _text(subscription.get("state") or account.get("plan_state")).lower()
            is_plus = fresh_check and (
                plan_state == "subscribed"
                or any(token in plan for token in ("plus", "pro", "team", "business", "enterprise"))
            )
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                if preserve_completed:
                    pipeline.updated_at = _utcnow()
                    _append_event(
                        pipeline,
                        "已完成账号仍为 Plus" if is_plus else "账号状态已刷新，已完成流水线保持不变",
                        level="info" if is_plus else "warning",
                    )
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)
                pipeline.plus_status = "plus" if is_plus else (plan or plan_state or "free")
                pipeline.final_result = "plus" if is_plus else "not_plus"
                if advance_pipeline:
                    if is_plus:
                        pipeline.state = "completed"
                        pipeline.completed_at = _utcnow()
                        pipeline.completion_source = "normal_scanner"
                        if pipeline.codex_post_action_armed:
                            pipeline.codex_post_action_done_at = None
                        pipeline.plus_next_check_at = None
                        pipeline.plus_check_paused_at = None
                        pipeline.last_error_code = ""
                        pipeline.last_error_message = ""
                    elif manual_paused_check:
                        pipeline.state = "plus_unconfirmed"
                        pipeline.plus_next_check_at = None
                        pipeline.last_error_code = "plus_unconfirmed"
                        pipeline.last_error_message = (
                            "本次检测结果缺少刷新时间，任务继续保持暂停"
                            if not fresh_check
                            else "本次人工检测仍未确认 Plus"
                        )
                    else:
                        self._ensure_plus_window(pipeline)
                        pipeline.plus_check_count = int(pipeline.plus_check_count or 0) + 1
                        if self._plus_window_expired(pipeline):
                            self._pause_plus_confirmation(pipeline)
                        else:
                            pipeline.state = "plus_pending"
                            pipeline.plus_next_check_at = min(
                                _utcnow() + timedelta(
                                    seconds=_next_plus_delay_seconds(pipeline.plus_check_count)
                                ),
                                _as_utc(pipeline.plus_check_deadline_at),
                            )
                pipeline.updated_at = _utcnow()
                _append_event(
                    pipeline,
                    (
                        "账号已确认升级为 Plus"
                        if is_plus
                        else (
                            "Plus 检测结果缺少本次刷新时间，暂不采信"
                            if not fresh_check
                            else f"本地复检尚未发现 Plus（{plan or plan_state or 'unknown'}）"
                        )
                    ),
                    level="info" if is_plus else "warning",
                )
                session.add(pipeline)
                session.commit()
                if advance_pipeline and is_plus and pipeline.codex_post_action_armed:
                    return self.start_codex(account_id, automatic=True)
                return self._serialize_pipeline(pipeline, detail=True)

    def check_untracked_plus(self, account_id: int) -> dict:
        """Confirm Plus after 546789 accepted a duplicate link without returning an order ID."""
        with _account_lock(account_id):
            check_started_at = _utcnow()
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None or pipeline.state not in {
                    "scanner_accepted_untracked",
                    "scanner_recovery_unconfirmed",
                    "scanner_submit_unconfirmed",
                }:
                    raise ValueError("当前流水线不需要无单号 Plus 确认")
                was_unconfirmed = pipeline.state in {
                    "scanner_recovery_unconfirmed",
                    "scanner_submit_unconfirmed",
                    "scanner_poll_failed",
                }
                pipeline.plus_status = "checking_untracked"
                pipeline.scanner_recovery_check_count = int(pipeline.scanner_recovery_check_count or 0) + 1
                attempt = pipeline.scanner_recovery_check_count
                pipeline.updated_at = _utcnow()
                _append_event(pipeline, f"开始第 {attempt} 次无单号 Plus 确认")
                session.add(pipeline)
                session.commit()

            result, auth_recovery = self._query_account_state_with_relogin(account_id)
            if not result.ok:
                with Session(engine) as session:
                    pipeline = self._pipeline_for_account(session, account_id)
                    assert pipeline is not None
                    pipeline.plus_status = "check_error"
                    pipeline.last_error_code = "untracked_plus_check_failed"
                    pipeline.last_error_message = (result.error or "无单号 Plus 确认失败")[:1000]
                    pipeline.updated_at = _utcnow()
                    _append_event(pipeline, pipeline.last_error_message, level="warning")
                    if auth_recovery["recovery_failed"]:
                        self._pause_untracked_plus_confirmation(
                            pipeline,
                            result.error or "账号失效且自动重新登录失败，Plus 确认已暂停",
                        )
                        pipeline.last_error_code = "untracked_plus_relogin_failed"
                    elif was_unconfirmed:
                        self._pause_untracked_plus_confirmation(
                            pipeline,
                            "本次人工检测失败，任务继续保持暂停",
                        )
                    else:
                        self._ensure_untracked_plus_window(pipeline)
                        if self._untracked_plus_window_expired(pipeline):
                            self._pause_untracked_plus_confirmation(
                                pipeline,
                                "无单号 Plus 查询持续失败，30 分钟观察窗口已结束",
                            )
                        else:
                            pipeline.state = "scanner_accepted_untracked"
                            pipeline.scanner_recovery_next_check_at = min(
                                _utcnow() + timedelta(
                                    seconds=_next_untracked_plus_delay_seconds(
                                        pipeline.scanner_recovery_check_count
                                    )
                                ),
                                _as_utc(pipeline.scanner_recovery_deadline_at),
                            )
                    session.add(pipeline)
                    session.commit()
                    return self._serialize_pipeline(pipeline, detail=True)

            account = self.accounts.get_account(int(account_id)) or {}
            view = account.get("account_view") if isinstance(account.get("account_view"), dict) else {}
            subscription = view.get("subscription") if isinstance(view.get("subscription"), dict) else {}
            status_view = view.get("status") if isinstance(view.get("status"), dict) else {}
            checked_at = _parse_datetime(status_view.get("checked_at") or account.get("checked_at"))
            fresh_check = bool(checked_at and checked_at.astimezone(timezone.utc) >= check_started_at)
            plan = _text(subscription.get("plan") or account.get("plan_name")).lower()
            plan_state = _text(subscription.get("state") or account.get("plan_state")).lower()
            is_plus = fresh_check and (
                plan_state == "subscribed"
                or any(token in plan for token in ("plus", "pro", "team", "business", "enterprise"))
            )
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                assert pipeline is not None
                pipeline.plus_status = "plus" if is_plus else (plan or plan_state or "free")
                pipeline.final_result = "plus" if is_plus else "not_plus"
                pipeline.last_error_code = ""
                pipeline.last_error_message = ""
                if is_plus:
                    pipeline.state = "completed"
                    pipeline.completed_at = _utcnow()
                    if pipeline.codex_post_action_armed:
                        pipeline.codex_post_action_done_at = None
                    pipeline.scanner_recovery_next_check_at = None
                    duplicate_confirmed = pipeline.scanner_status == "DUPLICATE_ACCEPTED"
                    pipeline.completion_source = (
                        "duplicate_submission_untracked"
                        if duplicate_confirmed
                        else "untracked_scanner_confirmation"
                    )
                    _append_event(
                        pipeline,
                        "重复提交后的无单号 Plus 确认成功，账号已升级为 Plus"
                        if duplicate_confirmed
                        else "扫码状态不可追踪，但本地已确认账号升级为 Plus",
                    )
                elif was_unconfirmed:
                    self._pause_untracked_plus_confirmation(
                        pipeline,
                        "本次人工检测仍未确认 Plus，任务继续保持暂停",
                    )
                else:
                    self._ensure_untracked_plus_window(pipeline)
                    if self._untracked_plus_window_expired(pipeline):
                        self._pause_untracked_plus_confirmation(pipeline)
                    else:
                        pipeline.state = "scanner_accepted_untracked"
                        pipeline.scanner_recovery_next_check_at = min(
                            _utcnow() + timedelta(
                                seconds=_next_untracked_plus_delay_seconds(
                                    pipeline.scanner_recovery_check_count
                                )
                            ),
                            _as_utc(pipeline.scanner_recovery_deadline_at),
                        )
                        pipeline.updated_at = _utcnow()
                        _append_event(
                            pipeline,
                            (
                                f"无单号确认结果缺少本次刷新时间，暂不采信 Plus 状态"
                                if not fresh_check
                                else f"无单号确认尚未发现 Plus（{plan or plan_state or 'unknown'}）"
                            ),
                            level="warning",
                        )
                session.add(pipeline)
                session.commit()
                if is_plus and pipeline.codex_post_action_armed:
                    return self.start_codex(account_id, automatic=True)
                return self._serialize_pipeline(pipeline, detail=True)

    def reset(self, account_id: int, *, force: bool = False) -> dict:
        with _account_lock(account_id):
            with Session(engine) as session:
                pipeline = self._pipeline_for_account(session, account_id)
                if pipeline is None:
                    return self._serialize_pipeline(None)
                linked_task_ids = [
                    task_id
                    for task_id in (pipeline.codex_task_id, pipeline.codex_push_task_id)
                    if task_id
                ]
                linked_active = any(
                    task is not None and task.status in TASK_ACTIVE_STATUSES
                    for task in (session.get(TaskModel, task_id) for task_id in linked_task_ids)
                )
                if linked_active and not force:
                    raise ValueError("Codex authorization or NexusVault push is still running")
                if pipeline.state in ACTIVE_STATES and not force:
                    raise ValueError("任务进行中，不能重置")
                if force:
                    from application.tasks import request_cancel

                    for task_id in linked_task_ids:
                        request_cancel(task_id)
                session.delete(pipeline)
                session.commit()
                return self._serialize_pipeline(None)
