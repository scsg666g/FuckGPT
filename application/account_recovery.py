from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from core.network_retry import is_retryable_network_error
from core.proxy_resolution import (
    PROXY_MODE_DIRECT,
    PROXY_MODE_MANUAL,
    PROXY_MODE_PROXY_SERVICE,
    normalize_proxy_mode,
)
from domain.actions import ActionExecutionCommand, ActionExecutionResult
from infrastructure.platform_runtime import PlatformRuntime


LogFn = Callable[..., None]
CancelCheck = Callable[[], bool]


def _emit(log_fn: LogFn | None, message: str, *, level: str = "info") -> None:
    if not callable(log_fn):
        return
    try:
        log_fn(str(message), level=level)
    except TypeError:
        log_fn(str(message))


@dataclass(slots=True)
class AccountStateSnapshot:
    ok: bool
    valid: bool | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def from_action(cls, result: ActionExecutionResult) -> "AccountStateSnapshot":
        raw_data = getattr(result, "data", None)
        data = raw_data if isinstance(raw_data, dict) else {}
        valid = data.get("valid") if isinstance(data.get("valid"), bool) else None
        return cls(
            ok=bool(getattr(result, "ok", False)),
            valid=valid,
            data=dict(data),
            error=str(getattr(result, "error", "") or ""),
        )


@dataclass(slots=True)
class AccountReloginResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    refreshed: AccountStateSnapshot | None = None

    @classmethod
    def from_action(cls, result: ActionExecutionResult) -> "AccountReloginResult":
        raw_data = getattr(result, "data", None)
        data = raw_data if isinstance(raw_data, dict) else {}
        return cls(
            ok=bool(getattr(result, "ok", False)),
            data=dict(data),
            error=str(getattr(result, "error", "") or ""),
        )


@dataclass(slots=True)
class AccountRecoveryOutcome:
    initial: AccountStateSnapshot
    final: AccountStateSnapshot
    relogin_attempted: bool = False
    relogin_ok: bool = False
    recovery_failed: bool = False
    relogin_error: str = ""
    relogin_data: dict[str, Any] = field(default_factory=dict)


def check_and_recover_account(
    *,
    check_state: Callable[[], AccountStateSnapshot],
    relogin: Callable[[], AccountReloginResult],
    relogin_invalid: bool,
    log_fn: LogFn | None = None,
    label: str = "账号",
) -> AccountRecoveryOutcome:
    """Refresh one account and repair an explicitly invalid login once.

    An unavailable or indeterminate check never starts a browser.  Callers can
    provide their own state persistence and log sink while sharing the exact
    invalid-login recovery decision and result contract.
    """
    initial = check_state()
    outcome = AccountRecoveryOutcome(initial=initial, final=initial)
    if not relogin_invalid or not initial.ok or initial.valid is not False:
        return outcome

    outcome.relogin_attempted = True
    _emit(log_fn, f"{label}检测发现账号登录已失效，开始自动重新登录", level="warning")
    try:
        relogin_result = relogin()
    except Exception as exc:  # noqa: BLE001 - normalize reusable action failures.
        relogin_result = AccountReloginResult(ok=False, error=str(exc))

    outcome.relogin_ok = bool(relogin_result.ok)
    outcome.relogin_error = str(relogin_result.error or "")
    outcome.relogin_data = dict(relogin_result.data or {})
    if not relogin_result.ok:
        outcome.recovery_failed = True
        _emit(
            log_fn,
            f"{label}自动重新登录失败: {relogin_result.error or '未知错误'}",
            level="error",
        )
        return outcome

    refresh_already_completed = relogin_result.refreshed is not None
    if not refresh_already_completed:
        _emit(log_fn, f"{label}自动重新登录成功，重新刷新账号状态与额度")
    try:
        refreshed = relogin_result.refreshed or check_state()
    except Exception as exc:  # noqa: BLE001 - a post-login network error is not an auth failure.
        refreshed = AccountStateSnapshot(ok=False, error=str(exc))
    outcome.final = refreshed

    if refreshed.ok and refreshed.valid is False:
        outcome.recovery_failed = True
        outcome.relogin_error = "自动重新登录后账号仍为失效状态"
        _emit(log_fn, f"{label}{outcome.relogin_error}", level="error")
    elif refreshed.ok and not refresh_already_completed:
        state_label = "有效" if refreshed.valid is True else "状态未确定"
        _emit(log_fn, f"{label}重新登录后刷新完成: {state_label}")
    elif not refreshed.ok and not refresh_already_completed:
        _emit(
            log_fn,
            f"{label}重新登录成功，但刷新账号状态失败: {refreshed.error or '未知错误'}",
            level="warning",
        )
    return outcome


def execute_runtime_action_with_worker_proxy(
    *,
    platform: str,
    account_id: int,
    action_id: str,
    params: dict[str, Any],
    scope_id: str,
    log_fn: LogFn | None = None,
    cancel_check: CancelCheck | None = None,
    runtime_factory: Callable[[], PlatformRuntime] | None = None,
) -> ActionExecutionResult:
    """Execute one platform action with the shared replaceable-proxy policy."""
    from core.worker_proxy import WorkerProxyPolicy, worker_proxy_manager

    proxy_mode = normalize_proxy_mode(
        str(params.get("platform_proxy_mode") or "").strip(),
        default=PROXY_MODE_DIRECT,
    )
    policy = WorkerProxyPolicy.load()
    attempts = policy.replace_max_attempts if proxy_mode == PROXY_MODE_PROXY_SERVICE else 1
    result: ActionExecutionResult | None = None
    for proxy_attempt in range(1, attempts + 1):
        lease = None
        runtime_params = dict(params)
        try:
            if proxy_mode == PROXY_MODE_PROXY_SERVICE:
                lease = worker_proxy_manager.acquire(
                    scope_id=scope_id,
                    log_fn=lambda message: _emit(log_fn, message),
                    cancel_check=cancel_check,
                    policy=policy,
                )
                runtime_params["platform_proxy_mode"] = PROXY_MODE_MANUAL
                runtime_params["platform_proxy_value"] = lease.url
                runtime_params["_proxy_log_mode"] = PROXY_MODE_PROXY_SERVICE
            runtime = (runtime_factory or PlatformRuntime)()
            result = runtime.execute_action(
                ActionExecutionCommand(
                    platform=platform,
                    account_id=int(account_id),
                    action_id=action_id,
                    params=runtime_params,
                ),
                log_fn=log_fn,
                cancel_check=cancel_check,
            )
            network_error = RuntimeError(str(result.error or ""))
            retry_proxy = (
                not result.ok
                and proxy_mode == PROXY_MODE_PROXY_SERVICE
                and is_retryable_network_error(network_error)
            )
            if lease is not None:
                if retry_proxy:
                    lease.report_failure()
                else:
                    lease.report_success()
            if retry_proxy and proxy_attempt < attempts:
                _emit(
                    log_fn,
                    f"代理网络异常，换 IP 重试 ({proxy_attempt + 1}/{attempts}): {result.error}",
                    level="warning",
                )
                continue
            return result
        finally:
            if lease is not None:
                lease.release()
    return result or ActionExecutionResult(ok=False, error="平台动作未执行")
