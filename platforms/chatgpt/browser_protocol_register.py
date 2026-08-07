"""ChatGPT registration through in-page Fetch inside one Camoufox context.

The state machine remains protocol-oriented, but every API request is emitted
by the browser page.  Cross-origin authorization and callback transitions stay
as document navigations so browser cookie, CORS, and redirect rules remain in
force.
"""
from __future__ import annotations

import json
import random
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urljoin

from .constants import (
    CHATGPT_APP,
    OPENAI_API_ENDPOINTS,
    OPENAI_AUTH,
    SENTINEL_SDK_URL,
)


FIRST_NAMES = (
    "James", "John", "Robert", "Michael", "David", "William", "Richard",
    "Joseph", "Thomas", "Daniel", "Matthew", "Anthony", "Mary", "Linda",
    "Jennifer", "Sarah", "Jessica", "Elizabeth",
)
LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Martin",
    "Lee", "White",
)


def _random_profile() -> tuple[str, str]:
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    age = random.randint(24, 36)
    birthdate = (datetime.now() - timedelta(days=age * 365)).strftime("%Y-%m-%d")
    return name, birthdate


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if callable(cancel_check) and cancel_check():
        raise RuntimeError("任务已取消")


def _response_error(result: dict) -> str:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        if code and message and code not in message:
            return f"{code}: {message}"
        if message or code:
            return message or code
    if isinstance(error, str) and error:
        return error
    text = str(result.get("text") or "").strip()
    return text[:300] or f"HTTP {int(result.get('status') or 0)}"


def _require_success(result: dict, label: str) -> dict:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if not result.get("ok") or int(result.get("status") or 0) >= 400 or data.get("error"):
        raise RuntimeError(f"{label}: {_response_error(result)}")
    return data


def _wait_for_public_sentinel_sdk(page, log, timeout: float = 20) -> None:
    predicate = "() => typeof window.SentinelSDK?.token === 'function'"
    try:
        page.wait_for_function(predicate, timeout=max(int(timeout * 1000), 1000))
        return
    except Exception:
        pass

    log("Browser Protocol 页面未发现 Sentinel SDK，加载官方 SDK...")
    try:
        page.add_script_tag(url=SENTINEL_SDK_URL)
        page.wait_for_function(predicate, timeout=max(int(timeout * 1000), 1000))
    except Exception as exc:
        raise RuntimeError(f"Browser Protocol Sentinel SDK 初始化失败: {exc}") from exc


def _browser_sentinel_headers(page, flow: str, log) -> dict[str, str]:
    _wait_for_public_sentinel_sdk(page, log)
    result = None
    token = None
    missing: list[str] = []
    for attempt in range(1, 4):
        result = page.evaluate(
            """
            async (flow) => {
              const sdk = window.SentinelSDK;
              const parse = (value) => {
                if (value === null || value === undefined || value === '') return null;
                if (typeof value !== 'string') return value;
                try { return JSON.parse(value); } catch { return value; }
              };
              const token = parse(await sdk.token(flow));
              let so = null;
              if (typeof sdk.sessionObserverToken === 'function') {
                try { so = parse(await sdk.sessionObserverToken(flow)); } catch {}
              }
              return { token, so };
            }
            """,
            flow,
        )
        token = result.get("token") if isinstance(result, dict) else None
        if isinstance(token, dict):
            missing = [key for key in ("p", "c", "id", "flow") if not str(token.get(key) or "")]
            if not missing:
                break
        else:
            missing = ["token"]
        if attempt < 3:
            log(
                f"Browser Protocol Sentinel token 不完整，重新生成 "
                f"({attempt}/3): {', '.join(missing)}"
            )
            time.sleep(1)
    if not isinstance(token, dict):
        raise RuntimeError("Browser Protocol Sentinel SDK 未返回 token 对象")
    if missing:
        raise RuntimeError("Browser Protocol Sentinel token 缺少字段: " + ", ".join(missing))

    headers = {
        "openai-sentinel-token": json.dumps(token, separators=(",", ":")),
    }
    so = result.get("so") if isinstance(result, dict) else None
    if isinstance(so, dict) and so:
        headers["openai-sentinel-so-token"] = json.dumps(so, separators=(",", ":"))
    elif isinstance(so, str) and so:
        headers["openai-sentinel-so-token"] = so
    return headers


def _auth_json_fetch(
    page,
    url: str,
    *,
    method: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout_ms: int = 45_000,
) -> dict:
    from .browser_register import _browser_fetch

    request_headers = {
        "accept": "application/json",
        "content-type": "application/json",
        **dict(headers or {}),
    }
    return _browser_fetch(
        page,
        url,
        method=method,
        headers=request_headers,
        body=json.dumps(payload, separators=(",", ":")),
        redirect="follow",
        timeout_ms=timeout_ms,
    )


def _navigate(page, target: str, log) -> str:
    from .browser_register import _goto_with_retry

    url = urljoin(OPENAI_AUTH, str(target or "").strip())
    if not url:
        raise RuntimeError("Browser Protocol 缺少导航地址")
    _goto_with_retry(
        page,
        url,
        wait_until="domcontentloaded",
        timeout=45_000,
        log=log,
    )
    return str(getattr(page, "url", "") or url)


def browser_protocol_registration_flow(
    page,
    email: str,
    password: str,
    otp_callback: Callable[[], str] | None,
    log,
    *,
    existing_account_callback: Callable[..., None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """Run the browser-backed API state machine in a single browser context."""
    from .browser_register import (
        _derive_registration_state_from_page,
        _handle_post_signup_onboarding,
        _is_registration_complete,
        _seed_browser_device_id,
        _start_browser_signup_via_authorize,
    )

    if not email:
        raise RuntimeError("Browser Protocol 缺少邮箱")
    if not password:
        raise RuntimeError("Browser Protocol 缺少注册密码")
    if not callable(otp_callback):
        raise RuntimeError("Browser Protocol 缺少邮箱验证码回调")

    device_id = str(uuid.uuid4())
    _seed_browser_device_id(page, device_id)
    _check_cancelled(cancel_check)
    log("Browser Protocol: 使用页面内 Fetch 初始化授权会话")
    _start_browser_signup_via_authorize(page, email, device_id, log)

    _check_cancelled(cancel_check)
    password_url = f"{OPENAI_AUTH}/create-account/password"
    log("Browser Protocol: 打开密码注册 Origin")
    _navigate(page, password_url, log)
    if "auth.openai.com" not in str(getattr(page, "url", "") or "").lower():
        raise RuntimeError("Browser Protocol 未进入 OpenAI Auth Origin")

    log("Browser Protocol: 页面内 Fetch 提交注册密码")
    password_headers = _browser_sentinel_headers(page, "username_password_create", log)
    password_result = _auth_json_fetch(
        page,
        OPENAI_API_ENDPOINTS["register"],
        method="POST",
        payload={"username": email, "password": password},
        headers=password_headers,
    )
    if not password_result.get("ok"):
        error_text = _response_error(password_result)
        lowered = error_text.lower()
        if callable(existing_account_callback) and any(
            marker in lowered for marker in ("already", "exists", "login", "user_exists")
        ):
            existing_account_callback(error_text)
        raise RuntimeError(f"Browser Protocol 设置密码失败: {error_text}")
    password_data = _require_success(password_result, "Browser Protocol 设置密码失败")
    password_continue = str(password_data.get("continue_url") or "").strip()
    if not password_continue:
        raise RuntimeError("Browser Protocol 设置密码成功但缺少 OTP 跳转地址")

    _check_cancelled(cancel_check)
    log("Browser Protocol: 跟随 OTP 发送导航")
    _navigate(page, password_continue, log)
    log("Browser Protocol: 等待邮箱验证码")
    code = str(otp_callback() or "").strip()
    if not code:
        raise RuntimeError("Browser Protocol 未收到邮箱验证码")

    _check_cancelled(cancel_check)
    log("Browser Protocol: 页面内 Fetch 校验邮箱验证码")
    otp_result = _auth_json_fetch(
        page,
        OPENAI_API_ENDPOINTS["validate_otp"],
        method="POST",
        payload={"code": code},
    )
    otp_data = _require_success(otp_result, "Browser Protocol OTP 校验失败")
    otp_continue = str(otp_data.get("continue_url") or "/about-you").strip()
    _navigate(page, otp_continue, log)

    name, birthdate = _random_profile()
    created_data: dict | None = None
    last_error = ""
    for attempt in range(1, 4):
        _check_cancelled(cancel_check)
        log(f"Browser Protocol: 页面内 Fetch 创建账号资料 ({attempt}/3)")
        create_headers = _browser_sentinel_headers(page, "oauth_create_account", log)
        create_result = _auth_json_fetch(
            page,
            OPENAI_API_ENDPOINTS["create_account"],
            method="POST",
            payload={"name": name, "birthdate": birthdate},
            headers=create_headers,
        )
        if create_result.get("ok") and not (
            isinstance(create_result.get("data"), dict)
            and create_result["data"].get("error")
        ):
            created_data = create_result.get("data") or {}
            break
        last_error = _response_error(create_result)
        if "registration_disallowed" not in last_error or attempt >= 3:
            break
        time.sleep(2)
    if created_data is None:
        raise RuntimeError(f"Browser Protocol 创建账号资料失败: {last_error}")

    callback_url = str(created_data.get("continue_url") or "").strip()
    if not callback_url:
        raise RuntimeError("Browser Protocol 创建账号成功但缺少 OAuth callback")
    _check_cancelled(cancel_check)
    log("Browser Protocol: 跟随 OAuth callback")
    _navigate(page, callback_url, log)

    deadline = time.time() + 30
    while time.time() < deadline:
        current_url = str(getattr(page, "url", "") or "")
        state = _derive_registration_state_from_page(page)
        if _is_registration_complete(state):
            _handle_post_signup_onboarding(page, log)
            state["registration_auth_mode"] = "password"
            state["browser_protocol"] = True
            return state
        if "chatgpt.com" in current_url.lower():
            state.update(
                {
                    "page_type": state.get("page_type") or "chatgpt_home",
                    "current_url": current_url,
                    "registration_auth_mode": "password",
                    "browser_protocol": True,
                }
            )
            _handle_post_signup_onboarding(page, log)
            return state
        time.sleep(0.25)
    raise RuntimeError(f"Browser Protocol OAuth callback 后未进入 ChatGPT: {getattr(page, 'url', '')}")
