"""ChatGPT / Codex CLI 平台插件"""
import json
import secrets
import time
from datetime import datetime, timezone

from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.network_retry import is_retryable_network_error
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registry import register
from core.proxy_pool import proxy_pool


def _generate_chatgpt_registration_password(length: int = 16) -> str:
    """生成更稳定通过 OpenAI 注册页校验的密码。

    旧协议流已经验证过：至少带小写、数字、符号时，成功率明显更稳。
    这里再补一个大写字符，避免浏览器流随机生成出“看起来够长但组合不够强”的密码。
    """
    specials = ",._!@#"
    minimum_length = 12
    size = max(int(length or minimum_length), minimum_length)
    required = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        secrets.choice("0123456789"),
        secrets.choice(specials),
    ]
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + specials
    required.extend(secrets.choice(pool) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "是", "开启", "启用"}


def _resolve_registration_auth_mode(extra: dict | None) -> str:
    payload = extra if isinstance(extra, dict) else {}
    direct = str(payload.get("registration_auth_mode") or "").strip().lower()
    if direct:
        return direct
    overview = payload.get("account_overview") if isinstance(payload.get("account_overview"), dict) else {}
    structured = str(overview.get("registration_auth_mode") or "").strip().lower()
    if structured:
        return structured
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else overview.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    amr = [
        str(item or "").strip().lower()
        for item in (profile.get("amr") or overview.get("amr") or [])
    ]
    if any("otp_email" in item for item in amr):
        return "email_otp"
    return ""


def _mask_phone_number(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}****{text[-4:]}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_plan(value) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if any(token in raw for token in ("team", "enterprise", "business")):
        return "team"
    if any(token in raw for token in ("plus", "pro", "premium", "paid")):
        return "plus"
    if raw in {"free", "basic", "starter", "hobby"}:
        return "free"
    return raw


def _normalize_usage_plan(value) -> str:
    """Mirror subscription._plan for an explicit wham plan_type."""
    normalized = _normalize_plan(value)
    return normalized if normalized in {"plus", "team", "free"} else "free"


def _plan_state(plan: str) -> str:
    normalized = _normalize_plan(plan)
    if normalized in {"plus", "team"}:
        return "subscribed"
    if normalized == "free":
        return "free"
    if "trial" in normalized:
        return "trial"
    if normalized in {"expired", "invalid", "banned", "deactivated"}:
        return "expired"
    return "unknown"


def _string_list(value) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _optional_bool(value):
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


def _security_from_profile(profile: dict, base: dict | None = None) -> dict:
    base = base if isinstance(base, dict) else {}
    profile = profile if isinstance(profile, dict) else {}

    phone_keys = {"phone_bound", "phone_number_masked", "phone_number", "phoneNumber"}
    phone_observed = any(key in base for key in phone_keys) or any(key in profile for key in phone_keys)
    phone_value = (
        base.get("phone_number_masked")
        or base.get("phone_number")
        or base.get("phoneNumber")
        or profile.get("phone_number_masked")
        or profile.get("phone_number")
        or profile.get("phoneNumber")
        or ""
    )
    phone_number_masked = _mask_phone_number(phone_value)
    explicit_phone_bound = None
    for payload in (base, profile):
        if "phone_bound" in payload:
            explicit_phone_bound = _optional_bool(payload.get("phone_bound"))
            break
    phone_bound = bool(phone_number_masked) if explicit_phone_bound is None else explicit_phone_bound

    amr_observed = "amr" in base or "amr" in profile
    amr = _string_list(base.get("amr")) or _string_list(profile.get("amr"))
    explicit_mfa = None
    mfa_observed = False
    for payload in (base, profile):
        for key in ("mfa_enabled", "has_mfa", "mfa"):
            if key not in payload:
                continue
            mfa_observed = True
            explicit_mfa = _optional_bool(payload.get(key))
            if explicit_mfa is not None:
                break
        if explicit_mfa is not None:
            break
    mfa_enabled = explicit_mfa if explicit_mfa is not None else any("mfa" in item.lower() for item in amr)
    security: dict = {}
    if phone_observed:
        security.update(
            {
                "phone_bound": bool(phone_bound),
                "phone_number_masked": phone_number_masked,
            }
        )
    if mfa_observed or amr_observed:
        security.update({"mfa_enabled": bool(mfa_enabled), "amr": amr})
    return security


def _build_account_state_summary(
    *,
    valid: bool | None,
    status,
    source,
    profile: dict | None,
    usage: dict | None,
    base: dict | None = None,
) -> dict:
    """Normalize check_valid and query_state into one persistence shape."""
    base = base if isinstance(base, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    usage = usage if isinstance(usage, dict) else {}

    # wham's explicit plan_type is authoritative, even when it is ``free``.
    usage_plan = usage.get("plan_type")
    plan = (
        _normalize_usage_plan(usage_plan)
        if str(usage_plan or "").strip()
        else _normalize_plan(status)
    )
    plan_state = _plan_state(plan)
    chips = []
    if plan == "plus":
        chips.append("Plus")
    elif plan == "team":
        chips.append("Team")
    elif plan == "free":
        chips.append("Free")

    security = _security_from_profile(profile, base)
    if security.get("phone_bound"):
        chips.append("已绑手机")
    check_source = (
        "backend-api/wham/usage"
        if str(usage_plan or "").strip()
        else str(
            source
            or base.get("check_source")
            or base.get("subscription_source")
            or ""
        ).strip()
    )
    summary = {
        "checked_at": str(base.get("checked_at") or _utcnow_iso()),
        "check_source": check_source,
        "subscription_source": check_source,
        "plan": plan,
        "plan_name": plan,
        "plan_state": plan_state,
        "chips": chips,
        **security,
        "chatgpt_usage": usage,
    }
    if str(status or "").strip().casefold() == "deactivated":
        summary["validity_status"] = "deactivated"
    if valid is not None:
        summary["valid"] = bool(valid)
    else:
        error = base.get("last_error") or base.get("subscription_error") or base.get("profile_error")
        if error not in (None, "", {}):
            summary["last_error"] = str(error)
    if "remote_email" in base or "email" in profile:
        summary["remote_email"] = str(base.get("remote_email") or profile.get("email") or "").strip()
    return summary


def _int_setting(value, default: int, *, minimum: int = 1) -> int:
    try:
        result = int(float(value if value not in (None, "") else default))
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


def _float_setting(value, default: float, *, minimum: float = 0.0) -> float:
    try:
        result = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


class _CodexSmsPhoneCallback:
    def __init__(
        self,
        provider,
        *,
        service: str = "",
        country: str = "",
        log_fn=None,
        buy_max_attempts: int = 20,
        buy_retry_interval: float = 3,
        otp_timeout_seconds: int = 120,
        phone_max_attempts: int = 3,
        cancel_check=None,
    ):
        self.provider = provider
        self.service = service
        self.country = country
        self.log_fn = log_fn if callable(log_fn) else (lambda _message: None)
        self.buy_max_attempts = max(int(buy_max_attempts or 1), 1)
        self.buy_retry_interval = max(float(buy_retry_interval or 0), 0)
        self.otp_timeout_seconds = max(int(otp_timeout_seconds or 1), 1)
        self.phone_max_attempts = max(int(phone_max_attempts or 1), 1)
        self.activation = None
        self.completed = False
        self.sent = False
        self._has_cancel_check = callable(cancel_check)
        self.cancel_check = cancel_check if callable(cancel_check) else (lambda: False)

    def _log(self, message: str) -> None:
        try:
            self.log_fn(message)
        except Exception:
            pass

    def _raise_if_cancelled(self) -> None:
        try:
            cancelled = bool(self.cancel_check())
        except Exception:
            cancelled = False
        if cancelled:
            self.cleanup()
            raise RuntimeError("任务已取消")

    def __call__(self) -> str:
        self._raise_if_cancelled()
        if self.activation is None:
            self.activation = self._buy_number_with_retry()
            return self.activation.phone_number
        return self._wait_for_code()

    @staticmethod
    def _is_retryable_buy_error(exc: Exception) -> bool:
        message = str(exc or "")
        lowered = message.lower()
        return (
            "NO_NUMBERS" in message
            or "暂无号码" in message
            or "无号码" in message
            or "no numbers" in lowered
            or "no_number" in lowered
            or "no number" in lowered
            or "out of stock" in lowered
            or "no stock" in lowered
            or "暂无库存" in message
            or "无库存" in message
            or is_retryable_network_error(exc)
        )

    def _buy_number_with_retry(self):
        last_error: Exception | None = None
        for attempt in range(1, self.buy_max_attempts + 1):
            self._raise_if_cancelled()
            try:
                activation = self.provider.get_number(service=self.service, country=self.country)
                self._log(f"Codex OAuth 接码买号成功: activation={activation.activation_id}")
                return activation
            except Exception as exc:
                last_error = exc
                if not self._is_retryable_buy_error(exc):
                    raise
                if attempt >= self.buy_max_attempts:
                    break
                if attempt <= 3 or attempt % 25 == 0:
                    self._log(
                        f"Codex OAuth 接码暂无号码，{self.buy_retry_interval:g}s 后重试 "
                        f"({attempt}/{self.buy_max_attempts}): {exc}"
                    )
                if self.buy_retry_interval > 0 and not self._has_cancel_check:
                    time.sleep(self.buy_retry_interval)
                elif self.buy_retry_interval > 0:
                    deadline = time.monotonic() + self.buy_retry_interval
                    while time.monotonic() < deadline:
                        self._raise_if_cancelled()
                        time.sleep(min(0.5, max(deadline - time.monotonic(), 0)))
        raise RuntimeError(f"Codex OAuth 接码买号重试耗尽: {last_error}") from last_error

    def _wait_for_code(self) -> str:
        deadline = time.monotonic() + self.otp_timeout_seconds
        interval = max(float(getattr(self.provider, "poll_interval", 3) or 0), 0)
        last_status = ""
        network_failures = 0
        while time.monotonic() < deadline:
            self._raise_if_cancelled()
            try:
                status = self.provider.get_status(self.activation.activation_id)
                network_failures = 0
            except Exception as exc:
                if not is_retryable_network_error(exc):
                    raise
                network_failures += 1
                last_status = f"瞬时网络/代理异常: {exc}"
                remaining = max(deadline - time.monotonic(), 0)
                if remaining <= 0:
                    break
                retry_delay = min(max(interval, 1.0), remaining)
                if network_failures <= 3 or network_failures % 10 == 0:
                    self._log(
                        f"Codex OAuth 查码遇到瞬时网络/代理异常，{retry_delay:g}s 后继续重试 "
                        f"(连续 {network_failures} 次): {exc}"
                    )
                self._sleep_interruptibly(retry_delay)
                continue
            last_status = status.raw or status.status
            if status.code:
                return status.code
            if status.status == "cancelled":
                raise RuntimeError(f"短信激活已取消: {self.activation.activation_id}")
            self._sleep_interruptibly(interval)
        raise TimeoutError(f"等待短信验证码超时 ({self.otp_timeout_seconds}s)，最后状态: {last_status or 'none'}")

    def _sleep_interruptibly(self, seconds: float) -> None:
        interval = max(float(seconds or 0), 0)
        if interval <= 0:
            return
        if not self._has_cancel_check:
            time.sleep(interval)
            return
        sleep_deadline = time.monotonic() + interval
        while time.monotonic() < sleep_deadline:
            self._raise_if_cancelled()
            time.sleep(min(0.5, max(sleep_deadline - time.monotonic(), 0)))

    def mark_send_succeeded(self) -> None:
        if self.activation is None or self.sent:
            return
        try:
            self.provider.mark_sms_sent(self.activation.activation_id)
        except Exception as exc:
            message = str(exc or "")
            if (
                "BAD_STATUS" not in message
                and "状态码无效" not in message
                and not is_retryable_network_error(exc)
            ):
                raise
            self._log(f"Codex OAuth 接码状态回写未完成，继续轮询验证码: {message}")
        self.sent = True

    def mark_send_failed(self, reason: str = "") -> None:
        self._log(f"Codex OAuth 手机号提交失败: {reason}")
        self.cleanup()

    def mark_code_failed(self, reason: str = "") -> None:
        self._log(f"Codex OAuth 短信验证码失败: {reason}")
        if self.activation is not None:
            try:
                self.provider.request_retry(self.activation.activation_id)
            except Exception:
                pass

    def report_success(self) -> None:
        if self.activation is None:
            return
        try:
            self.provider.finish(self.activation.activation_id)
        except Exception as exc:
            # OAuth and phone verification have already succeeded.  A failed
            # best-effort provider cleanup must not turn that success into a
            # failed account task.
            self._log(f"Codex OAuth 已成功，接码完成状态回写失败（忽略）: {exc}")
        finally:
            self.completed = True

    def cleanup(self) -> None:
        if self.activation is None or self.completed:
            return
        try:
            self.provider.cancel(self.activation.activation_id)
        except Exception:
            pass
        finally:
            self.activation = None
            self.sent = False

    def reset(self) -> None:
        self.activation = None
        self.sent = False
        self.completed = False


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "browser_protocol", "browser"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers = []

    # Declarative capabilities
    capabilities = [
        "query_state",      # Query account state/quota
        "switch_desktop",   # Switch to Codex desktop
        "relogin",          # Refresh ChatGPT browser login credentials
        "codex_oauth_authorize",  # Create Codex OAuth credentials through browser login
        "upload_cpa",       # Upload to CPA system
        "upload_tm",        # Upload to Team Manager
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        self._last_check_overview = {}
        try:
            from platforms.chatgpt.subscription import fetch_subscription_status_details
            from core.proxy_pool import proxy_pool
            class _A: pass
            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.id_token = extra.get("id_token", "")
            a.cookies = extra.get("cookies", "")
            a.extra = extra
            a.account_id = (
                extra.get("account_id")
                or extra.get("chatgpt_account_id")
                or getattr(account, "user_id", "")
                or ""
            )
            a.chatgpt_account_id = a.account_id

            region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
            configured_proxy = self.config.proxy if self.config else None
            disable_proxy_pool = _truthy((self.config.extra or {}).get("disable_proxy_pool")) if self.config else False
            strict_proxy = _truthy((self.config.extra or {}).get("strict_proxy")) if self.config else False
            raise_check_errors = _truthy((self.config.extra or {}).get("raise_check_errors")) if self.config else False
            request_timeout = max(int((self.config.extra or {}).get("request_timeout_seconds") or 20), 5) if self.config else 20
            proxy_candidates: list[tuple[str | None, bool]] = []
            if configured_proxy:
                proxy_candidates.append((configured_proxy, False))
            elif not disable_proxy_pool:
                pooled_proxy = proxy_pool.get_next(region=region)
                if pooled_proxy:
                    proxy_candidates.append((pooled_proxy, True))
            if not strict_proxy:
                proxy_candidates.append((None, False))

            last_error: Exception | None = None
            for proxy, should_report in proxy_candidates:
                try:
                    try:
                        details = fetch_subscription_status_details(a, proxy=proxy, timeout=request_timeout)
                    except TypeError as exc:
                        # Keep compatibility with lightweight provider stubs
                        # used by plugins/tests that still expose the old
                        # two-argument call shape.
                        if "timeout" not in str(exc):
                            raise
                        details = fetch_subscription_status_details(a, proxy=proxy)
                    if should_report and proxy:
                        proxy_pool.report_success(proxy)
                    status = details.get("status")
                    valid = status not in ("expired", "invalid", "banned", "deactivated", None)
                    overview = _build_account_state_summary(
                        valid=valid,
                        status=status,
                        source=details.get("source"),
                        profile=details.get("me"),
                        usage=details.get("usage"),
                    )
                    self._last_check_overview = overview
                    return valid
                except Exception as exc:
                    last_error = exc
                    if should_report and proxy:
                        proxy_pool.report_fail(proxy)
                    response = getattr(exc, "response", None)
                    status_code = int(getattr(response, "status_code", 0) or 0)
                    if status_code in {401, 403}:
                        self._last_check_overview = {
                            "valid": False,
                            "check_error": f"HTTP {status_code}",
                            "check_source": "authentication",
                        }
                        return False
                    continue
            if last_error is not None and (strict_proxy or raise_check_errors):
                raise last_error
        except Exception:
            if self.config and (
                _truthy((self.config.extra or {}).get("strict_proxy"))
                or _truthy((self.config.extra or {}).get("raise_check_errors"))
            ):
                raise
            return False
        return False

    def get_last_check_overview(self) -> dict:
        return dict(getattr(self, "_last_check_overview", {}) or {})

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return _generate_chatgpt_registration_password()

    def _map_chatgpt_result(
        self,
        result: dict,
        *,
        password: str = "",
        user_id: str = "",
    ) -> RegistrationResult:
        extra = {
            "account_id": result.get("account_id", ""),
            "access_token": result.get("access_token", ""),
            "refresh_token": result.get("refresh_token", ""),
            "id_token": result.get("id_token", ""),
            "session_token": result.get("session_token", ""),
            "workspace_id": result.get("workspace_id", ""),
            "cookies": (
                json.dumps(result.get("cookies"), ensure_ascii=False, separators=(",", ":"))
                if isinstance(result.get("cookies"), (dict, list))
                else result.get("cookies", "")
            ),
            "profile": result.get("profile", {}),
            "expires_at": result.get("expires_at", ""),
            "registration_auth_mode": result.get("registration_auth_mode", ""),
            "existing_account": bool(result.get("existing_account") or (result.get("registration_state") or {}).get("existing_account")),
            "account_status": str(result.get("account_status") or (result.get("registration_state") or {}).get("account_status") or ""),
        }
        for key in (
            "codex_auth_path",
            "codex_email",
            "codex_account_id",
            "codex_plan_type",
            "codex_access_token",
            "codex_refresh_token",
            "codex_id_token",
            "codex_expires_at",
            "codex_last_refresh",
        ):
            if result.get(key) not in (None, ""):
                extra[key] = result[key]
        if isinstance(result.get("post_codex_oauth"), dict):
            extra["post_codex_oauth"] = result["post_codex_oauth"]
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id or result.get("account_id", ""),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra=extra,
        )

    def build_browser_registration_adapter(self):
        def _build_browser_worker(ctx, artifacts):
            from platforms.chatgpt.browser_register import ChatGPTBrowserRegister
            from core.mailbox_lifecycle import MailboxAllocationLifecycle

            post_codex_oauth = _truthy(ctx.extra.get("auto_codex_oauth_after_register"))
            codex_phone_callback = self._build_codex_phone_callback(ctx.proxy) if post_codex_oauth else None
            keep_browser_open = _truthy(ctx.extra.get("codex_oauth_keep_browser_open"))
            browser_visible = _truthy(ctx.extra.get("browser_visible"))
            browser_protocol_headed = (
                ctx.executor_type == "browser_protocol"
                and (
                    browser_visible
                    or _truthy(ctx.extra.get("browser_protocol_headed"))
                )
            )
            browser_headed = (
                ctx.executor_type == "browser"
                and (
                    browser_visible
                    or _truthy(ctx.extra.get("browser_headed"))
                )
            )
            identity_metadata = dict(getattr(ctx.identity, "metadata", {}) or {})
            allocation_id = str(identity_metadata.get("mailbox_allocation_id") or "")

            def _worker_timeout(key: str, default: int, minimum: int, maximum: int) -> int:
                try:
                    value = int(float(ctx.extra.get(key, default) or default))
                except (TypeError, ValueError):
                    value = default
                return min(max(value, minimum), maximum)

            worker_idle_timeout = _worker_timeout(
                "browser_worker_idle_timeout",
                120,
                30,
                600,
            )
            try:
                configured_hard_timeout = int(
                    float(ctx.extra.get("browser_worker_hard_timeout", 0) or 0)
                )
            except (TypeError, ValueError):
                configured_hard_timeout = 0
            worker_hard_timeout = (
                min(max(configured_hard_timeout, worker_idle_timeout), 3600)
                if configured_hard_timeout > 0
                else 0
            )

            def _mark_existing_account(reason: str = "OpenAI 认证流程进入 login_password"):
                if allocation_id:
                    MailboxAllocationLifecycle().flag_existing_account(
                        allocation_id,
                        reason=reason,
                    )

            if browser_protocol_headed:
                ctx.log("Browser Protocol 已启用可视浏览器窗口")
            elif browser_headed:
                ctx.log("浏览器模式已启用可视浏览器窗口")

            return ChatGPTBrowserRegister(
                headless=(
                    ctx.executor_type == "headless"
                    or (
                        ctx.executor_type == "browser_protocol"
                        and not browser_protocol_headed
                    )
                    or (
                        ctx.executor_type == "browser"
                        and not browser_headed
                    )
                ),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                post_codex_oauth=post_codex_oauth,
                codex_phone_callback=codex_phone_callback,
                codex_oauth_timeout=int(ctx.extra.get("codex_oauth_timeout") or 300),
                keep_browser_open=keep_browser_open,
                prefer_password_registration=_truthy(ctx.extra.get("prefer_password_registration")),
                existing_account_callback=_mark_existing_account,
                cancel_check=ctx.platform.is_cancel_requested,
                worker_idle_timeout=worker_idle_timeout,
                worker_hard_timeout=worker_hard_timeout,
                flow_mode=(
                    "browser_protocol"
                    if ctx.executor_type == "browser_protocol"
                    else "dom"
                ),
                log_fn=ctx.log,
                backend_config=(ctx.extra or {}).get("_reuse_backend_config"),
            )

        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(result),
            browser_worker_builder=_build_browser_worker,
            browser_register_runner=lambda worker, ctx, artifacts: worker.run_isolated(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                password_provided=ctx.password_provided,
            ),
            otp_spec=OtpSpec(wait_message="等待验证码...", timeout=600),
        )

    def build_protocol_mailbox_adapter(self):
        def _build_protocol_worker(ctx, artifacts):
            from platforms.chatgpt.protocol_register import ChatGPTProtocolRegister

            return ChatGPTProtocolRegister(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                cancel_check=ctx.platform.is_cancel_requested,
            )

        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                password=ctx.password or "",
            ),
            worker_builder=_build_protocol_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                # ChatGPT's current OTP emails use subjects such as
                # "Your temporary ChatGPT login code" and do not always
                # contain the literal "OpenAI".  The mailbox provider already
                # filters stale messages and extracts a six-digit code, so a
                # sender/brand keyword here only causes valid messages to be
                # discarded.
                keyword="",
                wait_message="等待 Outlook 验证码...",
                timeout=180,
            ),
        )

    def get_platform_actions(self) -> list:
        proxy_params = [
            {"key": "platform_proxy_mode", "label": "ChatGPT/Codex 代理", "type": "select", "options": ["direct", "manual", "proxy_service"]},
            {"key": "platform_proxy_value", "label": "手动代理 URL", "type": "text"},
        ]
        return [
            {"id": "switch_account", "label": "切换到 Codex 桌面端", "params": []},
            {"id": "relogin", "label": "重新登录",
             "params": [
                 {"key": "browser_mode", "label": "浏览器模式", "type": "select", "options": ["headless", "headed"]},
                 {"key": "keep_browser_open", "label": "完成后保留浏览器窗口", "type": "select", "options": ["false", "true"]},
                 *proxy_params,
             ]},
            {"id": "codex_oauth_authorize", "label": "Codex OAuth 授权",
             "params": [
                 {"key": "oauth_mode", "label": "授权模式", "type": "select", "options": ["browser", "browser_protocol", "protocol"]},
                 {"key": "browser_mode", "label": "浏览器模式", "type": "select", "options": ["headless", "headed"]},
                 {"key": "keep_browser_open", "label": "完成后保留浏览器窗口", "type": "select", "options": ["false", "true"]},
                 *proxy_params,
             ]},
            {"id": "get_account_state", "label": "查询账号状态/订阅", "params": proxy_params},
            {"id": "upload_cpa", "label": "上传 CPA",
             "params": [
                 {"key": "api_url", "label": "CPA API URL", "type": "text"},
                 {"key": "api_key", "label": "CPA API Key", "type": "text"},
             ]},
            {"id": "upload_tm", "label": "上传 Team Manager",
             "params": [
                 {"key": "api_url", "label": "TM API URL", "type": "text"},
                 {"key": "api_key", "label": "TM API Key", "type": "text"},
             ]},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        self.raise_if_cancelled()
        aliases = {
            "switch_account": "switch_desktop",
            "get_account_state": "query_state",
        }
        resolved_action = aliases.get(action_id, action_id)
        if resolved_action in self.capabilities:
            result = self._handle_capability(resolved_action, account, params or {})
        else:
            result = self._execute_platform_action(resolved_action, account, params or {})
        self.raise_if_cancelled()
        return result

    def get_desktop_state(self) -> dict:
        from platforms.chatgpt.switch import get_codex_desktop_state

        return get_codex_desktop_state()

    def _execute_platform_action(self, action_id: str, account: Account, params: dict) -> dict:
        """Handle ChatGPT-specific actions."""
        proxy = self.config.proxy if self.config else None
        mailbox_proxy = str((self.config.extra or {}).get("mailbox_proxy") or "").strip() if self.config else ""
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        from .constants import OAUTH_CLIENT_ID
        a.client_id = extra.get("client_id", OAUTH_CLIENT_ID)
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id or ""
        a.account_id = extra.get("account_id") or extra.get("chatgpt_account_id") or account.user_id or ""

        if action_id == "relogin":
            from platforms.chatgpt.relogin import (
                ChatGPTReloginError,
                classify_relogin_failure,
                perform_chatgpt_relogin,
                utcnow_iso,
            )

            if not account.email:
                failure = classify_relogin_failure(RuntimeError("本地账号缺少邮箱"))
                return {
                    "ok": False,
                    "error": failure.message,
                    "data": {"failure_code": failure.code, "failed_at": utcnow_iso()},
                }

            otp_callback = None
            try:
                from core.mailbox_store import MailboxStore

                mailbox, mailbox_account, _mailbox_context = MailboxStore().resolve_mailbox_for_account(
                    platform="chatgpt",
                    account_id=int(getattr(account, "id", 0) or 0),
                    proxy=mailbox_proxy or None,
                    extra=self.config.extra if self.config else {},
                )
                if hasattr(mailbox, "set_cancel_checker"):
                    mailbox.set_cancel_checker(self.is_cancel_requested)
                before_ids = mailbox.get_current_ids(mailbox_account)

                def _relogin_otp_callback():
                    self.raise_if_cancelled()
                    self.log("等待重新登录邮箱验证码...")
                    code = mailbox.wait_for_code(
                        mailbox_account,
                        keyword="",
                        timeout=180,
                        before_ids=before_ids,
                    )
                    self.raise_if_cancelled()
                    if code:
                        self.log("重新登录验证码已获取")
                    return code

                otp_callback = _relogin_otp_callback
            except Exception as exc:
                self.log(f"未绑定可用验证邮箱，重新登录将仅尝试密码: {exc}")

            browser_mode = str(params.get("browser_mode") or "headless").strip().lower()
            headless = browser_mode not in {"headed", "false", "0", "no", "前台", "可见浏览器"}
            keep_browser_open = _truthy(params.get("keep_browser_open"))
            try:
                result = perform_chatgpt_relogin(
                    email=account.email,
                    password=account.password,
                    expected_account_id=a.account_id,
                    proxy=proxy,
                    headless=headless,
                    otp_callback=otp_callback,
                    keep_browser_open=keep_browser_open,
                    cancel_check=self.is_cancel_requested,
                    log_fn=self.log,
                )
                self.raise_if_cancelled()
                return {"ok": True, "data": result}
            except ChatGPTReloginError as exc:
                failure = exc.failure
                self.log(failure.message)
                return {
                    "ok": False,
                    "error": failure.message,
                    "data": {"failure_code": failure.code, "failed_at": utcnow_iso()},
                }

        if action_id == "codex_oauth_authorize":
            from platforms.chatgpt import codex_oauth

            self.raise_if_cancelled()
            if not account.email:
                return {"ok": False, "error": "Codex OAuth 授权需要账号邮箱"}
            registration_auth_mode = _resolve_registration_auth_mode(extra)
            oauth_mode = codex_oauth.normalize_codex_oauth_mode(
                params.get("oauth_mode")
                or params.get("executor_type")
                or (self.config.extra or {}).get("codex_oauth_mode")
                or "browser"
            )
            reusable_session = codex_oauth.has_codex_oauth_reusable_session(
                session_token=a.session_token,
                cookies=a.cookies,
            )
            if (
                not account.password
                and registration_auth_mode != "email_otp"
                and not (
                    oauth_mode in {"protocol", "browser_protocol"}
                    and reusable_session
                )
            ):
                return {"ok": False, "error": "Codex OAuth 授权需要账号密码或可复用会话"}
            browser_mode = str(params.get("browser_mode") or "").strip().lower()
            if not browser_mode:
                browser_mode = str((self.config.extra or {}).get("codex_oauth_browser_mode") or "headed").strip().lower()
            headless = browser_mode in {"headless", "true", "1", "yes", "后台", "后台浏览器"}
            keep_browser_open = _truthy(params.get("keep_browser_open") or (self.config.extra or {}).get("codex_oauth_keep_browser_open"))
            otp_callback = None
            try:
                from core.mailbox_store import MailboxStore

                mailbox, mailbox_account, _mailbox_context = MailboxStore().resolve_mailbox_for_account(
                    platform="chatgpt",
                    account_id=int(getattr(account, "id", 0) or 0) if getattr(account, "id", None) else 0,
                    proxy=mailbox_proxy or None,
                    extra=self.config.extra if self.config else {},
                )
                if hasattr(mailbox, "set_cancel_checker"):
                    mailbox.set_cancel_checker(self.is_cancel_requested)
                before_ids = mailbox.get_current_ids(mailbox_account)

                def _otp_callback():
                    self.raise_if_cancelled()
                    self.log("等待 Codex OAuth 邮箱验证码...")
                    code = mailbox.wait_for_code(
                        mailbox_account,
                        keyword="",
                        timeout=180,
                        before_ids=before_ids,
                    )
                    self.raise_if_cancelled()
                    if code:
                        self.log("Codex OAuth 验证码已获取")
                    return code

                otp_callback = _otp_callback
            except Exception as exc:
                self.log(f"未绑定可用验证邮箱，Codex OAuth 如触发验证码将失败: {exc}")
            phone_callback = self._build_codex_phone_callback(proxy)
            self.log(f"Codex OAuth 授权模式: {oauth_mode}")
            result = codex_oauth.perform_codex_oauth_login_with_mode(
                oauth_mode=oauth_mode,
                email=account.email,
                password=account.password,
                registration_auth_mode=registration_auth_mode,
                proxy=proxy,
                headless=headless,
                log_fn=self.log,
                otp_callback=otp_callback,
                phone_callback=phone_callback,
                keep_browser_open=keep_browser_open,
                session_token=a.session_token,
                cookies=a.cookies,
            )
            self.raise_if_cancelled()
            return {"ok": True, "data": result}

        if action_id == "switch_desktop":
            from platforms.chatgpt.switch import (
                close_codex_app,
                extract_session_token,
                fetch_chatgpt_account_state,
                get_codex_desktop_state,
                read_current_codex_account,
                restart_codex_app,
                switch_codex_account,
            )

            session_token = extract_session_token(a.session_token, a.cookies)
            if not session_token:
                return {"ok": False, "error": "Switch to Codex desktop requires session_token"}

            close_ok, close_msg = close_codex_app()
            switch_ok, switch_data = switch_codex_account(session_token=session_token, cookies=a.cookies)
            if not switch_ok:
                return {"ok": False, "error": switch_data.get("error", "Switch failed")}

            remote_state = fetch_chatgpt_account_state(
                access_token=a.access_token,
                session_token=session_token,
                cookies=a.cookies,
                chatgpt_account_id=a.account_id,
                id_token=a.id_token,
                proxy=proxy,
            )
            local_state = read_current_codex_account()
            restart_ok, restart_msg = restart_codex_app()
            message_parts = [switch_data.get("message", "Codex credentials written")]
            if close_msg:
                message_parts.append(close_msg)
            if restart_msg:
                message_parts.append(restart_msg)
            data = {
                "message": ".".join(part for part in message_parts if part),
                "close": {"ok": close_ok, "message": close_msg},
                "restart": {"ok": restart_ok, "message": restart_msg},
                "local_app_account": local_state,
                "desktop_app_state": get_codex_desktop_state(),
                "remote_state": remote_state,
                "switch_details": switch_data,
            }
            if remote_state.get("access_token"):
                data["access_token"] = remote_state["access_token"]
            if remote_state.get("refresh_token"):
                data["refresh_token"] = remote_state["refresh_token"]
            return {"ok": True, "data": data}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import upload_to_cpa, generate_token_json
            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(token_data, api_url=params.get("api_url"),
                                    api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager
            ok, msg = upload_to_team_manager(a, api_url=params.get("api_url"),
                                             api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"Unknown action: {action_id}")

    def _build_codex_phone_callback(self, proxy: str | None):
        try:
            from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
            from infrastructure.provider_settings_repository import ProviderSettingsRepository
            from providers.registry import create_provider, load_all

            repo = ProviderSettingsRepository()
            provider_key = repo.get_default_provider_key("sms")
            if not provider_key:
                self.log("Codex OAuth 未配置默认接码服务，add_phone 将尝试跳过")
                return None
            definition = ProviderDefinitionsRepository().get_by_key("sms", provider_key)
            extra = repo.resolve_runtime_settings("sms", provider_key, self.config.extra if self.config else {})
            # SMS provider APIs must use the local direct connection.  The
            # browser proxy is only for ChatGPT/Codex page traffic and must
            # never leak into number purchase or OTP polling requests.
            extra.pop("proxy", None)
            extra.pop("sms_proxy", None)
            extra["_log_fn"] = self.log
            load_all()
            client = create_provider("sms", (definition.driver_type if definition else "") or provider_key, extra)
            configuration_error = getattr(client, "configuration_error", lambda: "")()
            service = str(getattr(client, "default_service", "") or "").strip()
            country = str(getattr(client, "default_country", "") or "").strip()
            if configuration_error or not service or not country:
                reason = configuration_error or "service/country 未配置"
                self.log(f"Codex OAuth 默认接码服务配置不完整（{reason}），add_phone 将尝试跳过")
                return None
            return _CodexSmsPhoneCallback(
                client,
                service=service,
                country=country,
                log_fn=self.log,
                buy_max_attempts=_int_setting(
                    getattr(client, "buy_max_attempts", extra.get(f"{provider_key}_buy_max_attempts", 20)), 20
                ),
                buy_retry_interval=_float_setting(
                    getattr(client, "buy_retry_interval", extra.get(f"{provider_key}_buy_retry_interval", 3)), 3
                ),
                otp_timeout_seconds=_int_setting(
                    getattr(client, "otp_timeout_seconds", extra.get(f"{provider_key}_otp_timeout_seconds", 120)), 120
                ),
                phone_max_attempts=_int_setting(extra.get(f"{provider_key}_phone_max_attempts", 3), 3),
                cancel_check=self.is_cancel_requested,
            )
        except Exception as exc:
            self.log(f"Codex OAuth 初始化接码服务失败，add_phone 将尝试跳过: {exc}")
            return None

    # Override specific capability handlers
    def _handle_query_state(self, account: Account, params: dict) -> dict:
        """Handle query_state capability for ChatGPT."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")
        a.account_id = extra.get("account_id") or extra.get("chatgpt_account_id") or account.user_id or ""
        a.id_token = extra.get("id_token", "")

        from platforms.chatgpt.switch import fetch_chatgpt_account_state, get_codex_desktop_state, read_current_codex_account

        data = fetch_chatgpt_account_state(
            access_token=a.access_token,
            session_token=a.session_token,
            cookies=a.cookies,
            chatgpt_account_id=a.account_id,
            id_token=a.id_token,
            proxy=proxy,
        )
        summary = _build_account_state_summary(
            valid=data.get("valid") if isinstance(data.get("valid"), bool) else None,
            status=data.get("subscription_status") or data.get("plan"),
            source=data.get("subscription_source") or data.get("check_source"),
            profile=data.get("profile"),
            usage=data.get("chatgpt_usage"),
            base=data,
        )
        data.update(summary)
        # Keep the established action response fields while making them agree
        # with the canonical summary used by account checks and persistence.
        if summary.get("plan"):
            data["subscription_status"] = summary["plan"]
        if summary.get("check_source"):
            data["subscription_source"] = summary["check_source"]
        data["local_app_account"] = read_current_codex_account()
        data["desktop_app_state"] = get_codex_desktop_state()
        return {"ok": True, "data": data}
