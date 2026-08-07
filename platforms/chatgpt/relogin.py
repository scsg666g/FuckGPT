"""Reusable ChatGPT existing-account login orchestration.

The browser implementation is shared with registration, but the runner turns
on ``existing_account_only`` so a stale or incorrect local account can never
silently create a new remote account during a re-login action.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from platforms._browser_backend import BrowserBackendConfig


RELOGIN_CREDENTIAL_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "cookies",
    "workspace_id",
    "account_id",
}


@dataclass(frozen=True, slots=True)
class ReloginFailure:
    code: str
    reason: str

    @property
    def message(self) -> str:
        return f"重新登录失败 [{self.code}]: {self.reason}"


class ChatGPTReloginError(RuntimeError):
    def __init__(self, failure: ReloginFailure):
        self.failure = failure
        super().__init__(failure.message)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_relogin_failure(exc: Exception) -> ReloginFailure:
    if isinstance(exc, ChatGPTReloginError):
        return exc.failure
    reason = str(exc or "").strip() or "未知错误"
    lowered = reason.casefold()
    if reason == "任务已取消" or "cancel" in lowered or "取消" in reason:
        code = "cancelled"
    elif (
        "account_deactivated" in lowered
        or "deactivated" in lowered
        or "deleted or disabled" in lowered
        or "削除または無効" in reason
        or "账号已停用" in reason
        or "账号已禁用" in reason
    ):
        code = "account_deactivated"
    elif "密码" in reason or "password" in lowered or "credential" in lowered:
        code = "credentials_invalid"
    elif "验证码" in reason or "otp" in lowered or "邮箱" in reason or "mailbox" in lowered:
        code = "otp_unavailable" if "未" in reason or "不可用" in reason else "otp_failed"
    elif "代理" in reason or "proxy" in lowered or "network" in lowered or "网络" in reason:
        code = "network_or_proxy"
    elif "超时" in reason or "timeout" in lowered or "timed out" in lowered:
        code = "timeout"
    elif "账号不一致" in reason or "identity" in lowered:
        code = "identity_mismatch"
    elif "session" in lowered or "token" in lowered or "会话" in reason:
        code = "session_missing"
    else:
        code = "unexpected"
    return ReloginFailure(code=code, reason=reason)


def _normalized_identity(value: object) -> str:
    return str(value or "").strip().casefold()


def validate_relogin_result(
    result: dict,
    *,
    expected_email: str,
    expected_account_id: str = "",
) -> dict:
    """Validate and normalize browser output before any local data is changed."""
    payload = dict(result or {})
    access_token = str(payload.get("access_token") or "").strip()
    session_token = str(payload.get("session_token") or "").strip()
    if not access_token and not session_token:
        raise ChatGPTReloginError(
            ReloginFailure("session_missing", "登录完成但未获取到可保存的会话令牌")
        )

    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    remote_email = str(profile.get("email") or payload.get("email") or "").strip()
    if remote_email and expected_email and _normalized_identity(remote_email) != _normalized_identity(expected_email):
        raise ChatGPTReloginError(
            ReloginFailure(
                "identity_mismatch",
                f"远端邮箱与本地账号不一致（远端 {remote_email}，本地 {expected_email}）",
            )
        )

    remote_account_id = str(payload.get("account_id") or "").strip()
    if remote_account_id and expected_account_id and remote_account_id != str(expected_account_id).strip():
        raise ChatGPTReloginError(
            ReloginFailure("identity_mismatch", "远端账号 ID 与本地账号不一致")
        )

    auth_mode = str(payload.get("registration_auth_mode") or "").strip() or "email_otp"
    checked_at = utcnow_iso()
    payload.update(
        {
            "message": "重新登录成功，本地登录数据已刷新",
            "valid": True,
            "checked_at": checked_at,
            "remote_email": remote_email or expected_email,
            "registration_auth_mode": auth_mode,
            "last_login_at": checked_at,
            "last_login_status": "succeeded",
        }
    )
    return payload


def perform_chatgpt_relogin(
    *,
    email: str,
    password: str = "",
    expected_account_id: str = "",
    proxy: str | None = None,
    headless: bool = True,
    otp_callback: Callable[[], str] | None = None,
    keep_browser_open: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    log_fn: Callable[[str], None] = print,
    backend_config: BrowserBackendConfig | None = None,
) -> dict:
    """Log into one existing account and return validated replacement data."""
    if not str(email or "").strip():
        raise ChatGPTReloginError(ReloginFailure("email_missing", "本地账号缺少邮箱"))
    if not str(password or "").strip() and not callable(otp_callback):
        raise ChatGPTReloginError(
            ReloginFailure("credentials_missing", "既没有可用密码，也没有绑定可收验证码的邮箱")
        )

    from platforms.chatgpt.browser_register import ChatGPTBrowserRegister

    worker = ChatGPTBrowserRegister(
        headless=headless,
        proxy=proxy,
        otp_callback=otp_callback,
        keep_browser_open=keep_browser_open,
        existing_account_only=True,
        cancel_check=cancel_check,
        log_fn=log_fn,
        backend_config=backend_config,
    )
    try:
        result = worker.run_isolated(
            str(email).strip(),
            str(password or ""),
            password_provided=bool(str(password or "").strip()),
        )
        return validate_relogin_result(
            result,
            expected_email=str(email).strip(),
            expected_account_id=str(expected_account_id or "").strip(),
        )
    except Exception as exc:
        raise ChatGPTReloginError(classify_relogin_failure(exc)) from exc
