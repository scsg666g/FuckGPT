"""ChatGPT 浏览器注册流程（Camoufox）。"""
import base64
import json
import random
import re
import secrets
import sys
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from camoufox.sync_api import Camoufox

from .._browser_backend import BrowserBackendConfig, keep_browser_context_open, open_browser_backend
from .constants import (
    OPENAI_AUTH,
    CHATGPT_APP,
    PLATFORM_LOGIN_ENTRY,
    SENTINEL_SDK_URL,
    SENTINEL_REQ_URL,
    SENTINEL_FRAME_URL,
    SENTINEL_BASE,
    OAUTH_CONSENT_FORM_SELECTOR,
)


CAMOUFOX_VISIBLE_WINDOW_SIZE = (1280, 720)


class ExistingAccountAuthenticationError(RuntimeError):
    """An existing remote account could not be authenticated safely.

    The mailbox must not return to the registration pool, otherwise a later
    worker would repeat the same new-account attempt with another generated
    password.
    """

    preserve_mailbox = True


def _apply_camoufox_visible_window_limit(
    launch_opts: dict,
    backend_config: BrowserBackendConfig,
) -> None:
    if not backend_config.is_camoufox:
        return
    if backend_config.is_headless or bool(launch_opts.get("headless")):
        return
    launch_opts.setdefault("window", CAMOUFOX_VISIBLE_WINDOW_SIZE)


def _is_transient_nav_error(exc: BaseException) -> bool:
    """page.goto / page.reload 抛错是否属于可重试的瞬时网络断连。

    覆盖 Chromium/Firefox 常见的瞬时网络错误码。业务/页面错误（4xx、选择器
    超时等）不在此列，不会被误判重试。
    """
    msg = str(exc or "").lower()
    return any(
        token in msg
        for token in (
            "err_connection_closed",
            "err_connection_reset",
            "err_connection_refused",
            "err_connection_aborted",
            "err_connection_failed",
            "err_timed_out",
            "err_network_changed",
            "err_empty_response",
            "err_socks_connection_failed",
            "err_proxy_connection_failed",
            "err_tunnel_connection_failed",
            "err_name_not_resolved",
            "err_address_unreachable",
            "ns_error_net",            # Firefox/Camoufox 网络错误前缀
            "neterror",
            "navigating to",           # Playwright 包装的导航失败常带这句
        )
    )


def _goto_with_retry(
    page,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
):
    """``page.goto`` 带瞬时网络错误重试（默认 3 次，指数退避）。

    全局统一：注册流程里所有打开页面都该走这个，避免一次网络波动
    （ERR_CONNECTION_CLOSED / RESET / TIMED_OUT 等）就直接判失败。
    瞬时错误重试；业务错误（页面 4xx、选择器问题）原样抛出不重试。
    """
    _log = log or (lambda *_a, **_k: None)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 按错误内容判定是否重试
            last_exc = exc
            if attempt >= attempts or not _is_transient_nav_error(exc):
                raise
            backoff = 1.5 * attempt
            _log(
                f"打开页面瞬时网络失败（第 {attempt}/{attempts} 次，{backoff:.1f}s 后重试）："
                f"{str(exc)[:120]}"
            )
            time.sleep(backoff)
    if last_exc is not None:
        raise last_exc


def _reload_with_retry(
    page,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
):
    """``page.reload`` 带瞬时网络错误重试。"""
    _log = log or (lambda *_a, **_k: None)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return page.reload(wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= attempts or not _is_transient_nav_error(exc):
                raise
            time.sleep(1.5 * attempt)
    if last_exc is not None:
        raise last_exc

EMAIL_INPUT_SELECTORS = [
    'input#login-email',
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[autocomplete*="username"]',
    'input[inputmode="email"]',
    'input[id*="email"]',
]

PASSWORD_INPUT_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="new-password"]',
]

EMAIL_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Next")',
    'button:has-text("next")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
]

PASSWORD_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Sign up")',
    'button:has-text("sign up")',
    'button:has-text("Create account")',
    'button:has-text("create account")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("登録")',
    'button:has-text("新規登録")',
    'button:has-text("アカウントを作成")',
    'button:has-text("サインアップ")',
]

OTP_INPUT_SELECTORS = [
    "input[inputmode='numeric']",
    "input[autocomplete='one-time-code']",
    "input[type='tel']",
    "input[type='number']",
    "input[name*='code' i]",
    "input[id*='code' i]",
]

OTP_SUBMIT_SELECTORS = [
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Verify")',
    'button:has-text("verify")',
    'button:has-text("Next")',
    'button:has-text("next")',
    'button:text-is("続ける")',
    'button:text-is("続行")',
    'button:text-is("確認")',
    'button:text-is("認証")',
    'button:text-is("次へ")',
    'button[type="submit"]',
]

ABOUT_YOU_SUBMIT_SELECTORS = [
    'button:has-text("Finish creating account")',
    'button:has-text("finish creating account")',
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Next")',
    'button:has-text("next")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
    'button:has-text("完了")',
    'button:has-text("アカウントを作成")',
]

ABOUT_YOU_SUBMIT_TEXTS = [
    "Finish creating account",
    "finish creating account",
    "Continue",
    "continue",
    "Next",
    "next",
    "続ける",
    "続行",
    "次へ",
    "完了",
    "アカウントを作成",
]

SIGNUP_RECOVERY_SELECTORS = [
    'a:has-text("Sign up")',
    'button:has-text("Sign up")',
    'a:has-text("sign up")',
    'button:has-text("sign up")',
    'a:has-text("Register")',
    'button:has-text("Register")',
    'a:has-text("Create account")',
    'button:has-text("Create account")',
    'a:has-text("创建账号")',
    'button:has-text("创建账号")',
    'a:has-text("注册")',
    'button:has-text("注册")',
    'a:has-text("登録")',
    'button:has-text("登録")',
    'a:has-text("新規登録")',
    'button:has-text("新規登録")',
    'a:has-text("アカウントを作成")',
    'button:has-text("アカウントを作成")',
    'a:has-text("サインアップ")',
    'button:has-text("サインアップ")',
]

PASSWORDLESS_LOGIN_SELECTORS = [
    'button[name="intent"][value="passwordless_login_send_otp"]',
    'button[value="passwordless_login_send_otp"]',
    'button:has-text("Continue with email code")',
    'button:has-text("Use email code")',
    'button:has-text("Email code")',
    'button:has-text("one-time code")',
    'button:has-text("one time code")',
    'button:has-text("passwordless")',
    'button:has-text("一次性验证码")',
    'button:has-text("驗證碼")',
    'button:has-text("验证码")',
    'button:has-text("código único")',
    'button:has-text("code unique")',
    'button:has-text("Einmalcode")',
    'button:has-text("código de uso único")',
    'button:has-text("ワンタイムコード")',
    'button:has-text("一回限りのコード")',
    'button:has-text("認証コード")',
]

PASSWORD_CONTINUE_LINK_SELECTORS = [
    'a[href="/create-account/password"]',
    'a[href^="/create-account/password?"]',
    'a[href$="/create-account/password"]',
    'a[href="/log-in/password"]',
    'a[href^="/log-in/password?"]',
    'a[href$="/log-in/password"]',
]

# add-phone 页面国际拨号码 -> 国家名映射（用于 UI 下拉选择）
AUTH_TIMEOUT_TITLE_RE = re.compile(r"oops,\s*an\s*error\s*occurred|出错|發生錯誤|エラーが発生|問題が発生", re.I)
AUTH_TIMEOUT_DETAIL_RE = re.compile(
    r"operation\s+timed\s+out|route\s+error|405\s+method\s+not\s+allowed|failed\s+to\s+fetch|network\s+error|fetch\s+failed|タイムアウト|ネットワークエラー|取得に失敗",
    re.I,
)
AUTH_RETRY_TEXT_RE = re.compile(r"try\s+again|重试|重試|再試行|もう一度|やり直す", re.I)


def _is_auth_timeout_retry_text(text: str) -> bool:
    value = str(text or "")
    return bool(
        AUTH_RETRY_TEXT_RE.search(value)
        and (AUTH_TIMEOUT_TITLE_RE.search(value) or AUTH_TIMEOUT_DETAIL_RE.search(value))
    )


def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        # OAuth callbacks are served by the local process and must never be
        # sent through the remote registration proxy.
        "bypass": "localhost,127.0.0.1",
    }
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _wait_for_url(page, substring: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if substring in page.url:
            return True
        time.sleep(1)
    return False


def _find_first_selector(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            node = page.query_selector(sel)
        except Exception:
            node = None
        if node:
            return sel
    return None


def _find_first_visible_selector(page, selectors: list[str]) -> str | None:
    """Return the first rendered matching element, ignoring hidden form fields."""
    for sel in selectors:
        try:
            node = page.query_selector(sel)
        except Exception:
            node = None
        if not node:
            continue
        try:
            if node.is_visible():
                return sel
        except Exception:
            # Non-Playwright test doubles and older backends may not expose
            # visibility. Preserve their previous existence-based behavior.
            return sel
    return None


def _locator_is_visible(locator, *, timeout: int = 300) -> bool:
    """Probe a locator without using ``count()``, which can stall on auth DOM swaps."""
    try:
        return bool(locator.first.is_visible(timeout=timeout))
    except Exception:
        return False


def _wait_for_any_selector(page, selectors: list[str], timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _find_first_selector(page, selectors)
        if found:
            return found
        time.sleep(0.5)
    return None


def _click_first(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for selector in selectors:
            try:
                locator = page.locator(selector)
            except Exception:
                continue
            for index in range(12):
                try:
                    target = locator.nth(index)
                    if not target.is_visible(timeout=200) or not target.is_enabled(timeout=200):
                        continue
                    target.click(timeout=1500)
                    return selector if index == 0 else f"{selector} nth={index}"
                except Exception:
                    continue
        time.sleep(0.1)
    return None


def _click_first_no_wait(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    """Click a visible element without waiting for navigation.

    OpenAI's add-phone page sometimes leaves the submit XHR pending long enough
    that a normal Playwright click waits too long after the action was delivered.
    Only visible, enabled elements are considered and no DOM click fallback is
    used.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for selector in selectors:
            try:
                locator = page.locator(selector)
            except Exception:
                continue
            for index in range(12):
                try:
                    target = locator.nth(index)
                    if not target.is_visible(timeout=200) or not target.is_enabled(timeout=200):
                        continue
                    target.click(timeout=3000, no_wait_after=True)
                    return selector if index == 0 else f"{selector} nth={index}"
                except Exception:
                    continue
        time.sleep(0.1)
    return None


def _click_visible_button_by_text(page, texts: list[str], *, timeout: int = 3) -> str | None:
    deadline = time.time() + timeout
    candidates = [str(text or "").strip() for text in texts if str(text or "").strip()]
    while time.time() < deadline:
        try:
            clicked_text = str(
                page.evaluate(
                    """
                    (texts) => {
                      const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                      const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.display !== 'none' && style.visibility !== 'hidden'
                          && rect.width > 0 && rect.height > 0;
                      };
                      const disabled = (el) => Boolean(
                        el.disabled ||
                        el.getAttribute('aria-disabled') === 'true' ||
                        el.closest('[aria-disabled="true"]')
                      );
                      const controls = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'))
                        .slice(0, 80)
                        .map((el) => ({
                          el,
                          text: normalize(el.innerText || el.value || el.textContent || el.getAttribute('aria-label')),
                        }))
                        .filter((item) => item.text && visible(item.el) && !disabled(item.el));
                      const exact = controls.find((item) => texts.some((text) => item.text === text));
                      const loose = controls.find((item) =>
                        !/google|microsoft|apple|github/i.test(item.text) &&
                        texts.some((text) => text && item.text.includes(text))
                      );
                      const target = exact || loose;
                      if (!target) return '';
                      target.el.click();
                      return target.text;
                    }
                    """,
                    candidates,
                )
                or ""
            ).strip()
            if clicked_text:
                return f'text="{clicked_text}"'
        except Exception:
            pass
        time.sleep(0.1)
    return None


def _summarize_otp_submit_state(page) -> str:
    try:
        summary = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const buttons = Array.from(document.querySelectorAll('button, input[type="submit"]'))
                .slice(0, 12)
                .map((el) => ({
                  text: normalize(el.innerText || el.value || el.textContent).slice(0, 40),
                  visible: visible(el),
                  disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                  type: String(el.getAttribute('type') || el.tagName || '').slice(0, 20),
                }));
              const inputs = Array.from(document.querySelectorAll(
                  'input[autocomplete="one-time-code"], input[inputmode="numeric"], input[name*="code" i], input[id*="code" i], input[type="text"]'
                ))
                .slice(0, 8)
                .map((el) => ({
                  autocomplete: String(el.getAttribute('autocomplete') || ''),
                  inputmode: String(el.getAttribute('inputmode') || ''),
                  visible: visible(el),
                  disabled: Boolean(el.disabled),
                  readOnly: Boolean(el.readOnly),
                  valueLength: String(el.value || '').trim().length,
                }));
              return { buttons, inputs };
            }
            """
        )
    except Exception as exc:
        return f"diagnostics_failed={str(exc)[:120]}"
    if not isinstance(summary, dict):
        return "diagnostics_unavailable"
    buttons = summary.get("buttons") if isinstance(summary.get("buttons"), list) else []
    inputs = summary.get("inputs") if isinstance(summary.get("inputs"), list) else []
    button_text = "; ".join(
        f"{item.get('text') or item.get('type') or '-'} visible={item.get('visible')} disabled={item.get('disabled')}"
        for item in buttons[:5]
        if isinstance(item, dict)
    )
    input_text = "; ".join(
        f"otpInput visible={item.get('visible')} disabled={item.get('disabled')} readOnly={item.get('readOnly')} valueLength={item.get('valueLength')}"
        for item in inputs[:3]
        if isinstance(item, dict)
    )
    return f"buttons=[{button_text or '-'}] inputs=[{input_text or '-'}]"


def _click_otp_submit_button(page, log: Callable[[str], None], *, timeout: int = 8) -> str | None:
    start_url = str(getattr(page, "url", "") or "")
    deadline = time.time() + max(int(timeout), 1)
    last_error = ""
    seen_controls: set[str] = set()
    while time.time() < deadline:
        for selector in OTP_SUBMIT_SELECTORS:
            try:
                locator = page.locator(selector)
            except Exception:
                continue
            # locator.count() has no per-call timeout in the sync Playwright
            # API and can block indefinitely on OpenAI's dynamically replacing
            # OTP document. Probe a small bounded set of candidates directly;
            # is_visible() retains the explicit 200ms timeout for absent nodes.
            for index in range(5):
                try:
                    target = locator.nth(index)
                    if not target.is_visible(timeout=200):
                        continue
                    if not target.is_enabled(timeout=200):
                        continue
                    try:
                        signature = str(
                            target.evaluate(
                                """
                                (el) => [
                                  el.tagName,
                                  el.id,
                                  el.getAttribute('name'),
                                  el.getAttribute('value'),
                                  String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                                ].join('|')
                                """
                            )
                            or ""
                        )
                    except Exception:
                        signature = ""
                    lowered_signature = signature.lower()
                    if any(token in lowered_signature for token in ("resend", "重新发送", "重发", "再送信")):
                        continue
                    if any(token in lowered_signature for token in ("google", "apple", "microsoft", "facebook", "github")):
                        continue
                    if signature and signature in seen_controls:
                        continue
                    if signature:
                        seen_controls.add(signature)
                    target.click(timeout=3000, no_wait_after=True)
                    return selector if index == 0 else f"{selector} nth={index}"
                except Exception as exc:
                    last_error = f"{selector} nth={index}: {str(exc)[:120]}"
                    if _wait_for_otp_submit_progress(page, start_url=start_url, timeout=1.5):
                        return f"{selector} nth={index} delayed"
                    try:
                        hit_target = bool(
                            target.evaluate(
                                """
                                (el) => {
                                  const rect = el.getBoundingClientRect();
                                  const hit = document.elementFromPoint(
                                    rect.left + rect.width / 2,
                                    rect.top + rect.height / 2
                                  );
                                  return Boolean(hit && (hit === el || el.contains(hit)));
                                }
                                """
                            )
                        )
                        box = target.bounding_box() if hit_target else None
                        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                            x = float(box["x"]) + float(box["width"]) / 2
                            y = float(box["y"]) + float(box["height"]) / 2
                            page.mouse.move(x, y, steps=3)
                            page.mouse.click(x, y)
                            if _wait_for_otp_submit_progress(page, start_url=start_url, timeout=8):
                                return f"{selector} nth={index} real mouse"
                    except Exception:
                        pass
                    try:
                        target.focus(timeout=1000)
                        target.press("Enter", timeout=1500)
                        if _wait_for_otp_submit_progress(page, start_url=start_url, timeout=8):
                            return f"{selector} nth={index} keyboard Enter"
                    except Exception:
                        pass
                    continue
        time.sleep(0.25)
    if last_error:
        log(f"验证码页提交按钮点击失败: {last_error}")
    log(f"验证码页提交按钮状态: {_summarize_otp_submit_state(page)}")
    return None


def _otp_submit_progress_url(url: str) -> bool:
    value = str(url or "").lower()
    return (
        "about-you" in value
        or "add-phone" in value
        or "create-account/password" in value
        or "log-in/password" in value
        or "chatgpt.com" in value
        or "code=" in value
        or "consent" in value
        or "sign-in-with-chatgpt" in value
        or "workspace" in value
        or "organization" in value
    )


def _wait_for_otp_submit_progress(page, *, start_url: str, timeout: float) -> bool:
    deadline = time.time() + max(float(timeout), 0)
    while time.time() < deadline:
        current_url = str(getattr(page, "url", "") or "")
        if _otp_submit_progress_url(current_url) and (
            current_url != str(start_url or "")
            or ("email-verification" not in current_url and "email-otp" not in current_url)
        ):
            return True
        time.sleep(0.25)
    return False


def _wait_for_about_you_submit_progress(page, *, start_url: str, timeout: float) -> bool:
    """Wait until the about-you form has actually left its starting page.

    The OTP progress predicate deliberately treats an about-you URL as success,
    so it cannot be reused here: when called from about-you it would report
    success before the navigation had even started.  Only a changed URL that is
    a recognized post-profile destination counts as progress.
    """
    original_url = str(start_url or "")
    deadline = time.time() + max(float(timeout), 0)
    while time.time() < deadline:
        current_url = str(getattr(page, "url", "") or "")
        if _about_you_submit_progress_url(current_url, original_url):
            return True
        time.sleep(0.25)
    return False


def _about_you_submit_progress_url(current_url: str, start_url: str) -> bool:
    current = str(current_url or "")
    return bool(
        current
        and current != str(start_url or "")
        and "about-you" not in current.lower()
        and _otp_submit_progress_url(current)
    )


def _fill_otp_with_keyboard_fallback(page, otp: str, log: Callable[[str], None]) -> bool:
    selectors = [
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
        "input[name*='code' i]",
        "input[id*='code' i]",
        "input[type='text']",
    ]
    for selector in selectors:
        try:
            target = page.locator(selector).first
            target.wait_for(state="visible", timeout=1200)
            if not target.is_enabled(timeout=500):
                continue
            try:
                target.click(timeout=1200)
            except Exception:
                target.focus(timeout=1000)
            time.sleep(0.1)
            cleared = False
            for shortcut in ("Control+A", "Meta+A"):
                try:
                    page.keyboard.press(shortcut)
                    page.keyboard.press("Backspace")
                    cleared = True
                    break
                except Exception:
                    continue
            if not cleared:
                try:
                    target.fill("")
                except Exception:
                    pass
            page.keyboard.type(str(otp), delay=random.randint(30, 70))
            time.sleep(0.3)
            try:
                final_value = str(target.input_value() or "").strip()
            except Exception:
                final_value = ""
            if final_value == str(otp).strip():
                log(f"验证码页已使用键盘 fallback 重新填写输入框: {selector}")
                return True
        except Exception:
            continue
    log("验证码页键盘 fallback 重新填写失败")
    return False


def _auth_timeout_retry_page_state(page, *, path_patterns: list[str] | None = None) -> dict:
    try:
        result = page.evaluate(
            """
            (pathPatterns) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const pathname = String(location.pathname || '');
              if (Array.isArray(pathPatterns) && pathPatterns.length) {
                const matched = pathPatterns.some((raw) => {
                  try { return new RegExp(raw, 'i').test(pathname); } catch (_) { return false; }
                });
                if (!matched) return { retryPage: false, url: location.href, text: '' };
              }
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
              const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'));
              const retryButton = document.querySelector('button[data-dd-action-name="Try again"]')
                || buttons.find((button) => {
                  const label = String([button.value, button.textContent, button.getAttribute?.('aria-label'), button.getAttribute?.('title')].filter(Boolean).join(' '));
                  return visible(button) && /try\\s+again|重试|重試|再試行|もう一度|やり直す/i.test(label);
                });
              return {
                retryPage: Boolean(retryButton && /try\\s+again|重试|重試/i.test(text) && (/oops,?\\s*an\\s*error\\s*occurred|operation\\s+timed\\s+out|route\\s+error|405\\s+method\\s+not\\s+allowed|failed\\s+to\\s+fetch|network\\s+error/i.test(text))),
                retryEnabled: Boolean(retryButton && visible(retryButton) && !retryButton.disabled && retryButton.getAttribute('aria-disabled') !== 'true'),
                url: location.href,
                text,
              };
            }
            """,
            path_patterns or [],
        )
        if isinstance(result, dict):
            result["retryPage"] = bool(result.get("retryPage") or _is_auth_timeout_retry_text(str(result.get("text") or "")))
            return result
    except Exception:
        pass
    return {"retryPage": False, "retryEnabled": False, "url": str(page.url or ""), "text": ""}


def _recover_auth_timeout_retry_page(
    page,
    log,
    *,
    path_patterns: list[str] | None = None,
    max_clicks: int = 3,
    wait_after_click: float = 3.0,
) -> dict:
    last_state = {}
    for attempt in range(1, max_clicks + 1):
        state = _auth_timeout_retry_page_state(page, path_patterns=path_patterns)
        last_state = state
        if not state.get("retryPage"):
            return {"recovered": attempt > 1, "clicks": attempt - 1, "url": str(state.get("url") or page.url)}
        if not state.get("retryEnabled"):
            time.sleep(0.5)
            continue
        log(f"  检测到 OpenAI auth 超时重试页，点击 Try again ({attempt}/{max_clicks})")
        clicked = _click_first_no_wait(
            page,
            [
                'button[data-dd-action-name="Try again"]',
                'button:has-text("Try again")',
                'button:has-text("try again")',
                'button:has-text("重试")',
                'button:has-text("重試")',
                'button:has-text("再試行")',
                'button:has-text("もう一度")',
                'button:has-text("やり直す")',
            ],
            timeout=2,
        )
        if not clicked:
            try:
                clicked = "dom" if page.evaluate(
                    """
                    () => {
                      const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                      };
                      const direct = document.querySelector('button[data-dd-action-name="Try again"]');
                      const target = direct || Array.from(document.querySelectorAll('button, [role="button"]')).find((el) => {
                        const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        return visible(el) && /try\\s+again|重试|重試/i.test(text);
                      });
                      if (!target) return false;
                      target.click();
                      return true;
                    }
                    """
                ) else ""
            except Exception:
                clicked = ""
        if not clicked:
            break
        time.sleep(wait_after_click)
        state = _auth_timeout_retry_page_state(page, path_patterns=path_patterns)
        last_state = state
        if not state.get("retryPage"):
            return {"recovered": True, "clicks": attempt, "url": str(state.get("url") or page.url)}
    return {
        "recovered": False,
        "clicks": max_clicks,
        "url": str(last_state.get("url") or page.url),
        "text": str(last_state.get("text") or "")[:300],
    }


def _is_login_password_url(url: str) -> bool:
    return bool(
        re.search(
            r"(?:auth|accounts)\.openai\.com/(?:.*/)?log-?in(?:/password)?(?:[/?#]|$)",
            str(url or ""),
            flags=re.I,
        )
    )


def _build_manual_flow_state(page_type: str, current_url: str) -> dict:
    state = _extract_flow_state(None, current_url)
    state["page_type"] = page_type
    state["current_url"] = current_url
    return state


def _get_visible_page_text(page) -> str:
    try:
        return str(page.evaluate("() => document.body?.innerText || ''") or "")
    except Exception:
        return ""


def _has_signup_registration_choice(page) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if _find_first_selector(page, SIGNUP_RECOVERY_SELECTORS):
        return True
    text = _get_visible_page_text(page)
    return bool(re.search(r"sign\s*up|register|create\s*account|还没有帐户|还没有账户|請註冊|请注册|去注册|注册", text, flags=re.I))


def _click_passwordless_login_if_available(page, log, *, context: str) -> bool:
    selector = _click_first(page, PASSWORDLESS_LOGIN_SELECTORS, timeout=1)
    if selector:
        log(f"{context} 已选择一次性验证码登录: {selector}")
        time.sleep(1)
        return True
    try:
        clicked = bool(
            page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const target = nodes.find((el) => {
                    const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return visible(el) && /使用一次性验证码登录|使用一次性驗證碼登入|one-time code|one time code|passwordless|ワンタイムコード|一回限りのコード|認証コード/i.test(text);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """
            )
        )
    except Exception:
        clicked = False
    if clicked:
        log(f"{context} 已选择一次性验证码登录")
        time.sleep(1)
    return clicked


def _wait_for_passwordless_login_state(page, *, timeout: float = 12) -> dict:
    deadline = time.time() + max(float(timeout or 0), 0)
    last_state = _derive_registration_state_from_page(page)
    while time.time() < deadline:
        if str(last_state.get("page_type") or "") != "login_password":
            return last_state
        time.sleep(0.25)
        last_state = _derive_registration_state_from_page(page)
    return last_state


def _get_page_oauth_url(page) -> str:
    try:
        return str(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const anchors = Array.from(document.querySelectorAll('a[href*="/api/oauth/authorize"], a[href*="/oauth/authorize"]'));
                  const anchor = anchors.find((el) => visible(el));
                  return anchor ? String(anchor.href || anchor.getAttribute('href') || '') : '';
                }
                """
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _oauth_url_matches_state(url: str, state: str) -> bool:
    if not url or not state:
        return False
    return f"state={state}" in url or f"state%3D{state}" in url


ACCOUNT_DEACTIVATED_ERROR_TOKENS = (
    "account_deactivated",
    "account deactivated",
    "deactivated account",
    "deleted or disabled",
    "account has been deleted or disabled",
    "アカウントが削除または無効",
    "削除または無効化",
    "削除または無効",
    "账号已停用",
    "帳號已停用",
    "账号已禁用",
)


def _is_account_deactivated_error(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(token.casefold() in lowered for token in ACCOUNT_DEACTIVATED_ERROR_TOKENS)


def _normalize_auth_error_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return ""
    if _is_account_deactivated_error(compact) and "account_deactivated" not in compact.casefold():
        compact = f"error_code: account_deactivated · {compact}"
    return compact[:1000]


def _read_page_body_text(page, *, timeout: int = 350) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=timeout) or "").strip()
    except Exception:
        return ""


def _auth_error_data(text: str) -> dict | None:
    if _is_account_deactivated_error(text):
        return {"failure_code": "account_deactivated"}
    return None


def _extract_auth_error_text(page) -> str:
    body_text = _read_page_body_text(page)
    if _is_account_deactivated_error(body_text):
        return _normalize_auth_error_text(body_text)

    selectors = [
        "text=account_deactivated",
        "text=deleted or disabled",
        "text=account has been deleted or disabled",
        "text=アカウントが削除または無効",
        "text=削除または無効化",
        "text=Failed to create account",
        "text=Sorry, we cannot create your account",
        "text=Please try again",
        "text=Invalid code",
        "text=Incorrect code",
        "text=验证码错误",
        "text=验证码无效",
        "text=無効なコード",
        "text=コードが無効",
        "text=コードの有効期限が切れ",
        "text=認証コードが正しくありません",
        "text=Enter a valid age to continue",
        "text=doesn't look right",
        "[role='alert']",
        ".error, [class*='error'], [class*='Error']",
    ]
    for selector in selectors:
        try:
            text = str(page.locator(selector).first.text_content(timeout=350) or "").strip()
        except Exception:
            text = ""
        if text and "oai_log" not in text and "SSR_HTML" not in text:
            return _normalize_auth_error_text(text)
    for token in (
        "account_deactivated",
        "deleted or disabled",
        "アカウントが削除または無効",
        "削除または無効化",
        "Invalid code",
        "Incorrect code",
        "expired code",
        "code has expired",
        "验证码错误",
        "验证码无效",
        "验证码已过期",
        "無効なコード",
        "コードが無効",
        "コードの有効期限が切れ",
        "認証コードが正しくありません",
    ):
        if token in body_text:
            return _normalize_auth_error_text(token)
    return ""


def _fill_input_like_user(page, selector: str, value: str) -> bool:
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=2000)
        current = str(locator.input_value() or "").strip()
        if current == str(value).strip():
            return True
        locator.click(timeout=1500)
        _browser_pause(page)
        try:
            locator.fill("")
        except Exception:
            pass
        _browser_pause(page, headed=False)
        try:
            locator.type(value, delay=random.randint(35, 85))
        except Exception:
            try:
                page.fill(selector, value)
            except Exception:
                return False
        final_value = str(locator.input_value() or "").strip()
        if final_value == str(value):
            return True
    except Exception:
        pass

    try:
        ok = page.evaluate(
            """
            ({ selector, value }) => {
              const input = document.querySelector(selector);
              if (!input) return false;
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
              if (!setter) return false;
              setter.call(input, value);
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              return String(input.value || '') === String(value || '');
            }
            """,
            {"selector": selector, "value": value},
        )
        return bool(ok)
    except Exception:
        return False


def _press_enter_on_input(page, input_selector: str) -> bool:
    try:
        target = page.locator(input_selector).first
        if not target.is_visible(timeout=500) or not target.is_enabled(timeout=500):
            return False
        try:
            target.click(timeout=1000)
        except Exception:
            target.focus(timeout=1000)
        target.press("Enter", timeout=1500)
        return True
    except Exception:
        return False


def _sync_hidden_birthday_input(page, birthdate: str, log) -> bool:
    try:
        synced = bool(
            page.evaluate(
                """
                (value) => {
                  const input = document.querySelector("input[name='birthday']");
                  if (!input) return false;
                  input.value = value;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  return String(input.value || '') === String(value || '');
                }
                """,
                birthdate,
            )
        )
    except Exception:
        synced = False
    if synced:
        log(f"about_you 已同步隐藏 birthday: {birthdate}")
    return synced


ABOUT_YOU_VALUE_INPUT_DOM_SELECTOR = (
    "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
    ":not([type='submit']):not([type='button']):not([type='reset'])"
    ":not([type='file']):not([type='image']):not([disabled]):not([readonly])"
)
ABOUT_YOU_VALUE_INPUT_SELECTOR = f"{ABOUT_YOU_VALUE_INPUT_DOM_SELECTOR}:visible"


def _collect_visible_text_inputs(page) -> list[dict]:
    try:
        inputs = page.evaluate(
            """
            (inputSelector) => {
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const nodes = Array.from(document.querySelectorAll(inputSelector));
              const visible = nodes.filter((el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style
                  && style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && rect.width > 0
                  && rect.height > 0;
              });
              return visible.map((el, visibleIndex) => {
                const explicitLabels = Array.from(document.querySelectorAll('label'))
                  .filter((label) => String(label.getAttribute('for') || '') === String(el.id || ''))
                  .map((label) => normalize(label.textContent));
                const wrappedLabel = normalize(el.closest('label')?.textContent || '');
                const ariaLabel = normalize(el.getAttribute('aria-label'));
                const labelledByText = normalize(
                  String(el.getAttribute('aria-labelledby') || '')
                    .split(/\\s+/)
                    .filter(Boolean)
                    .map((id) => normalize(document.getElementById(id)?.textContent || ''))
                    .join(' ')
                );
                const parentText = normalize(el.parentElement?.textContent || '');
                return {
                  visibleIndex,
                  type: normalize(el.getAttribute('type') || el.type || ''),
                  name: normalize(el.getAttribute('name') || ''),
                  id: normalize(el.id || ''),
                  autocomplete: normalize(el.getAttribute('autocomplete') || ''),
                  inputMode: normalize(el.getAttribute('inputmode') || ''),
                  dataType: normalize(el.getAttribute('data-type') || ''),
                  testId: normalize(el.getAttribute('data-testid') || ''),
                  min: normalize(el.getAttribute('min') || ''),
                  max: normalize(el.getAttribute('max') || ''),
                  maxLength: Number(el.maxLength || 0),
                  placeholder: normalize(el.getAttribute('placeholder') || ''),
                  ariaLabel,
                  labels: explicitLabels.filter(Boolean),
                  wrappedLabel,
                  labelledByText,
                  parentText,
                };
              });
            }
            """,
            ABOUT_YOU_VALUE_INPUT_DOM_SELECTOR,
        ) or []
    except Exception:
        inputs = []
    text_like_types = {"", "text", "number", "date", "tel", "email"}
    return [
        item
        for item in inputs
        if isinstance(item, dict) and str(item.get("type") or "").strip().lower() in text_like_types
    ]


def _about_you_input_hints(entry: dict) -> str:
    parts: list[str] = []
    labels = entry.get("labels") or []
    if isinstance(labels, list):
        parts.extend(str(item or "") for item in labels)
    parts.extend(
        [
            str(entry.get("wrappedLabel") or ""),
            str(entry.get("labelledByText") or ""),
            str(entry.get("ariaLabel") or ""),
            str(entry.get("placeholder") or ""),
            str(entry.get("name") or ""),
            str(entry.get("id") or ""),
            str(entry.get("parentText") or ""),
        ]
    )
    return " ".join(part for part in parts if part).strip().lower()


def _about_you_stable_input_hints(entry: dict) -> str:
    """Return language-independent form metadata for an about-you input."""
    return " ".join(
        str(entry.get(key) or "").strip().lower()
        for key in ("name", "id", "autocomplete", "type", "inputMode", "dataType", "testId")
        if str(entry.get(key) or "").strip()
    )


def _about_you_input_has_semantic_field(entry: dict, field: str) -> bool:
    """Identify a field from stable DOM attributes, without relying on UI language."""
    hints = _about_you_stable_input_hints(entry)
    if not hints:
        return False

    tokens_by_field = {
        "name": ("name", "fullname", "full-name", "full_name"),
        "age": ("age",),
        "birthday": ("birthday", "birthdate", "birth-date", "birth_date", "dob"),
    }
    tokens = tokens_by_field.get(field, ())
    if any(re.search(rf"(?:^|[^a-z0-9]){re.escape(token)}(?:$|[^a-z0-9])", hints) for token in tokens):
        return True
    return field == "birthday" and str(entry.get("type") or "").strip().lower() == "date"


def _infer_about_you_mode(
    entries: list[dict],
    mode_probe: dict,
    *,
    has_birthday_select: bool = False,
    has_segmented_birthday: bool = False,
) -> str:
    """Classify the profile form using control semantics first and translated text last."""
    if has_birthday_select:
        return "birthday_select"

    has_age_control = any(_about_you_input_has_semantic_field(entry, "age") for entry in entries)
    has_birthday_control = any(_about_you_input_has_semantic_field(entry, "birthday") for entry in entries)
    if has_segmented_birthday or has_birthday_control:
        return "birthday"
    if has_age_control:
        return "age"

    has_age_label = bool(mode_probe.get("hasAge"))
    has_birthday_label = bool(mode_probe.get("hasBirthday"))
    if has_age_label and not has_birthday_label:
        return "age"
    return "birthday"


def _collect_visible_checkboxes(page) -> list[dict]:
    try:
        checkboxes = page.evaluate(
            """
            () => {
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const nodes = Array.from(document.querySelectorAll("input[type='checkbox']:not([disabled])"));
              const visible = nodes.filter((el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              });
              return visible.map((el, visibleIndex) => {
                const explicitLabels = Array.from(document.querySelectorAll('label'))
                  .filter((label) => String(label.getAttribute('for') || '') === String(el.id || ''))
                  .map((label) => normalize(label.textContent));
                const labelledByText = normalize(
                  String(el.getAttribute('aria-labelledby') || '')
                    .split(/\\s+/)
                    .filter(Boolean)
                    .map((id) => normalize(document.getElementById(id)?.textContent || ''))
                    .join(' ')
                );
                return {
                  visibleIndex,
                  name: normalize(el.getAttribute('name') || ''),
                  id: normalize(el.id || ''),
                  testId: normalize(el.getAttribute('data-testid') || ''),
                  required: Boolean(el.required),
                  ariaRequired: String(el.getAttribute('aria-required') || '').toLowerCase() === 'true',
                  ariaInvalid: String(el.getAttribute('aria-invalid') || '').toLowerCase() === 'true',
                  nativeInvalid: typeof el.checkValidity === 'function' ? !el.checkValidity() : false,
                  checked: Boolean(el.checked),
                  labels: explicitLabels.filter(Boolean),
                  wrappedLabel: normalize(el.closest('label')?.textContent || ''),
                  labelledByText,
                  parentText: normalize(el.parentElement?.textContent || ''),
                };
              });
            }
            """
        ) or []
    except Exception:
        checkboxes = []
    return [item for item in checkboxes if isinstance(item, dict)]


def _about_you_checkbox_hints(entry: dict) -> str:
    labels = entry.get("labels") if isinstance(entry.get("labels"), list) else []
    parts = [
        *(str(value or "") for value in labels),
        str(entry.get("wrappedLabel") or ""),
        str(entry.get("labelledByText") or ""),
        str(entry.get("parentText") or ""),
    ]
    return " ".join(value for value in parts if value).strip().lower()


def _about_you_checkbox_is_master(entry: dict) -> bool:
    stable = " ".join(
        str(entry.get(key) or "").strip().lower() for key in ("name", "id", "testId")
    )
    return bool(re.search(r"(?:^|[^a-z0-9])(all(?:checkbox(?:es)?|consents?|agreements?)?|selectall)(?:$|[^a-z0-9])", stable))


def _about_you_checkbox_is_required(entry: dict) -> bool:
    if any(bool(entry.get(key)) for key in ("required", "ariaRequired", "ariaInvalid", "nativeInvalid")):
        return True
    # Some localized React forms expose the requirement only in their label.
    # DOM validity remains the primary signal; these markers are a conservative fallback.
    hints = _about_you_checkbox_hints(entry)
    return any(
        marker in hints
        for marker in (
            "(required)",
            " required",
            "(필수)",
            "필수",
            "必須",
            "必填",
            "obligatoire",
            "erforderlich",
            "obligatorio",
            "obbligatorio",
            "obrigatório",
        )
    )


def _check_required_about_you_consents(page, log) -> int:
    """Check only required individual consents, never the optional/all master control."""
    entries = _collect_visible_checkboxes(page)
    required_entries = [
        entry
        for entry in entries
        if not _about_you_checkbox_is_master(entry) and _about_you_checkbox_is_required(entry)
    ]
    checked_count = 0
    locator = page.locator("input[type='checkbox']:visible:not([disabled])")
    for entry in required_entries:
        try:
            visible_index = int(entry.get("visibleIndex"))
            target = locator.nth(visible_index)
            if not target.is_checked(timeout=500):
                try:
                    target.check(timeout=1500)
                except Exception:
                    # React can replace the node after check while preserving
                    # state. Re-resolve before click so a stale locator cannot
                    # toggle an already-checked consent back off.
                    target = page.locator("input[type='checkbox']:visible:not([disabled])").nth(
                        visible_index
                    )
                    if not target.is_checked(timeout=500):
                        target.click(timeout=1500)
            if target.is_checked(timeout=500):
                checked_count += 1
        except Exception:
            continue
    if entries:
        log(
            "about_you 同意项: "
            f"visible={len(entries)}, required={len(required_entries)}, checked={checked_count}, "
            f"master_skipped={sum(1 for entry in entries if _about_you_checkbox_is_master(entry))}"
        )
        if checked_count < len(required_entries):
            log("about_you 部分必选同意项未能勾选，将由页面校验反馈触发一次重试")
    return checked_count


def _pick_best_about_you_input(entries: list[dict], field: str, exclude_visible_indices: set[int] | None = None) -> dict | None:
    exclude = {int(value) for value in (exclude_visible_indices or set())}
    best_entry = None
    best_score = float("-inf")
    for entry in entries:
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            continue
        if visible_index in exclude:
            continue
        hints = _about_you_input_hints(entry)
        if not hints:
            continue

        score = 0
        if field == "name":
            if _about_you_input_has_semantic_field(entry, "name"):
                score += 20
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet", "vollständiger name", "nome completo")):
                score += 10
            if any(token in hints for token in (" name ", "name", "autocomplete=name", "nombre", "nom", "nome")):
                score += 3
            if any(token in hints for token in ("age", "年龄", "edad", "âge", "alter", "idade", "birthday", "birth", "date of birth", "出生", "生日")):
                score -= 8
        elif field == "age":
            if _about_you_input_has_semantic_field(entry, "age"):
                score += 20
            if any(token in hints for token in ("age", "年龄", "how old", "edad", "âge", "alter", "idade", "나이", "연령")):
                score += 10
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet")):
                score -= 10
            if "name" in hints and "age" not in hints and "年龄" not in hints and "edad" not in hints:
                score -= 6
            if any(token in hints for token in ("birthday", "birth", "date of birth", "出生", "生日", "fecha de nacimiento", "nascimento")):
                score -= 3
        else:
            continue

        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score > 0:
        return best_entry

    if field == "age" and len(entries) == 2:
        ordered = []
        for entry in entries:
            try:
                visible_index = int(entry.get("visibleIndex"))
            except Exception:
                continue
            if visible_index not in exclude:
                ordered.append(entry)
        if len(ordered) == 1:
            return ordered[0]
        if len(ordered) == 2:
            return ordered[1]
    return None


def _derive_registration_state_from_page(page) -> dict:
    current_url = str(page.url or "")
    state = _extract_flow_state(None, current_url)
    if state.get("page_type"):
        return state

    if _find_first_visible_selector(page, PASSWORD_INPUT_SELECTORS):
        page_type = "login_password" if _is_login_password_url(current_url) else "create_account_password"
        return _build_manual_flow_state(page_type, current_url)

    otp_selector = _find_first_visible_selector(page, OTP_INPUT_SELECTORS)
    if otp_selector and "password" not in otp_selector:
        return _build_manual_flow_state("email_otp_verification", current_url)

    try:
        about_visible = bool(
            page.evaluate(
                """
                () => {
                  const inputs = Array.from(document.querySelectorAll("input:not([type='hidden'])"));
                  const text = String(document.body?.innerText || '').toLowerCase();
                  const hasName = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('name') || hint.includes('姓名') || hint.includes('全名');
                  });
                  const hasAgeOrBirth = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('age') || hint.includes('birth') || hint.includes('birthday') || hint.includes('年龄') || hint.includes('生日');
                  });
                  return (hasName && hasAgeOrBirth) || text.includes('about you');
                }
                """
            )
        )
    except Exception:
        about_visible = False
    if about_visible:
        return _build_manual_flow_state("about_you", current_url)

    return state


def _recover_signup_password_page(page, log) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if not _has_signup_registration_choice(page):
        return False
    selector = _click_first(page, SIGNUP_RECOVERY_SELECTORS, timeout=2)
    if not selector:
        return False
    log(f"密码页落到登录态，尝试点击注册入口恢复: {selector}")
    time.sleep(1.2)
    return True


def _wait_for_signup_entry_transition(page, log, timeout: int = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _click_passwordless_login_if_available(page, log, context="邮箱页提交后"):
            time.sleep(0.5)
            continue
        state = _derive_registration_state_from_page(page)
        if state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
            "chatgpt_home",
            "oauth_callback",
        }:
            if state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return state
        error_text = _extract_auth_error_text(page)
        if error_text:
            raise RuntimeError(f"邮箱页提交失败: {error_text[:300]}")
        time.sleep(0.25)
    raise RuntimeError("邮箱页提交后未进入密码/验证码页面")


def _start_browser_signup_via_page(page, email: str, log, *, flow_label: str = "注册") -> dict:
    for entry_url in (PLATFORM_LOGIN_ENTRY, f"{OPENAI_AUTH}/log-in"):
        try:
            log(f"打开 OpenAI {flow_label}入口: {entry_url}")
            _goto_with_retry(page, entry_url, wait_until="domcontentloaded", timeout=30000, log=log)
        except Exception as exc:
            log(f"{flow_label}入口访问失败: {entry_url} -> {exc}")
            continue

        initial_state = _derive_registration_state_from_page(page)
        if initial_state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
        }:
            return initial_state

        email_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=12)
        if not email_selector:
            continue
        if not _fill_input_like_user(page, email_selector, email):
            raise RuntimeError("邮箱页填写失败")
        log(f"邮箱页输入框: {email_selector}")

        inline_state = _derive_registration_state_from_page(page)
        if inline_state.get("page_type") in {"create_account_password", "login_password"}:
            if inline_state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return inline_state

        submit_selector = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
        if submit_selector:
            log(f"邮箱页已点击继续按钮: {submit_selector}")
        elif _press_enter_on_input(page, email_selector):
            log("邮箱页未找到可点击 Continue，已在邮箱输入框按 Enter")
        else:
            raise RuntimeError("邮箱页未找到 Continue 按钮")

        return _wait_for_signup_entry_transition(page, log)

    raise RuntimeError(f"未找到 OpenAI {flow_label}入口邮箱输入框")


def _start_browser_signup_via_authorize(page, email: str, device_id: str, log) -> dict:
    log("访问 ChatGPT 首页...")
    _goto_with_retry(page, f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000, log=log)

    log("获取 CSRF token...")
    csrf_token = ""
    for attempt in range(1, 4):
        csrf_token = _get_browser_csrf_token(page)
        if csrf_token:
            break
        if attempt < 3:
            delay = 1.5 * attempt
            log(f"CSRF token 瞬时获取失败 ({attempt}/3)，{delay:.1f}s 后在当前页面重试")
            time.sleep(delay)
    if not csrf_token:
        raise RuntimeError("连续 3 次获取 CSRF token 失败")

    log(f"提交邮箱: {email}")
    authorize_url = _start_browser_signin(page, email, device_id, csrf_token)
    if not authorize_url:
        raise RuntimeError("提交邮箱失败，未获取 authorize URL")

    final_url = _browser_authorize(page, authorize_url, log)
    if not final_url:
        raise RuntimeError("访问 authorize URL 失败")
    return _derive_registration_state_from_page(page)


def _dump_debug(page, prefix: str) -> None:
    page.screenshot(path=f"/tmp/{prefix}.png")
    with open(f"/tmp/{prefix}.html", "w") as f:
        f.write(page.content())


def _get_cookies(page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


def _cookies_to_header(cookies_dict: dict) -> str:
    parts = []
    for name, value in (cookies_dict or {}).items():
        if name and value not in (None, ""):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _decode_jwt_payload_no_verify(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_chatgpt_account_id(access_token: str) -> str:
    payload = _decode_jwt_payload_no_verify(access_token)
    auth_info = payload.get("https://api.openai.com/auth") or {}
    if isinstance(auth_info, dict):
        account_id = str(auth_info.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    return str(payload.get("sub") or "").strip()


def _chatgpt_session_result_from_data(data: dict, page, cookies_dict: dict, log) -> tuple[dict | None, str]:
    if not isinstance(data, dict):
        return None, "session API JSON 不是对象"

    access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    if not access_token:
        return None, "session API 未返回 accessToken"

    latest_cookies = dict(cookies_dict or {})
    try:
        latest_cookies.update(_get_cookies(page))
    except Exception as exc:
        log(f"ChatGPT session cookies 读取失败，使用已捕获 cookies: {exc}")
    session_token = str(latest_cookies.get("__Secure-next-auth.session-token") or "").strip()
    account_id = _extract_chatgpt_account_id(access_token)
    result = {
        "access_token": access_token,
        "refresh_token": str(data.get("refreshToken") or data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("idToken") or data.get("id_token") or "").strip(),
        "session_token": session_token,
        "account_id": account_id,
        "workspace_id": str(data.get("workspaceId") or data.get("workspace_id") or "").strip(),
        "profile": data.get("user") if isinstance(data.get("user"), dict) else {},
        "expires_at": str(data.get("expires") or "").strip(),
        "cookies": _cookies_to_header(latest_cookies),
        "session": data,
    }
    log(
        "ChatGPT session 获取成功: "
        f"accessToken=yes, session_token={'yes' if session_token else 'no'}, "
        f"account_id={account_id or '-'}"
    )
    return result, ""


def _chatgpt_session_result_from_text(text: str, page, cookies_dict: dict, log) -> tuple[dict | None, str]:
    try:
        data = json.loads(text)
    except Exception as exc:
        return None, f"session API JSON 解析失败: {exc}"
    return _chatgpt_session_result_from_data(data, page, cookies_dict, log)


def _fetch_chatgpt_session_via_same_origin(page, cookies_dict: dict, log, session_url: str) -> tuple[dict | None, str, bool]:
    current_url = str(getattr(page, "url", "") or "")
    if "chatgpt.com" not in current_url.lower():
        return None, "", False

    log(f"浏览器内请求 ChatGPT session API: {session_url}")
    try:
        payload = page.evaluate(
            """
            async (sessionUrl) => {
              const response = await fetch(sessionUrl, {
                method: "GET",
                credentials: "include",
                headers: { "accept": "application/json" },
              });
              return {
                status: response.status,
                url: response.url,
                text: await response.text(),
              };
            }
            """,
            session_url,
        )
    except Exception as exc:
        return None, str(exc), True

    if not isinstance(payload, dict):
        return None, "session API 浏览器内请求未返回对象", True

    status = int(payload.get("status") or 0)
    response_url = str(payload.get("url") or "")
    text = str(payload.get("text") or "")
    log(f"ChatGPT session API 浏览器内请求状态: {status} url={response_url[:120]}")
    if status == 200 and text:
        return (*_chatgpt_session_result_from_text(text, page, cookies_dict, log), True)
    return None, f"session API HTTP {status}: {text[:200]}", True


def _fetch_chatgpt_session_from_page(page, cookies_dict: dict, log, timeout: int = 45) -> dict:
    deadline = time.time() + max(int(timeout or 0), 5)
    last_error = ""
    session_url = f"{CHATGPT_APP}/api/auth/session"
    log(f"打开 ChatGPT session API: {session_url}")

    while time.time() < deadline:
        same_origin_result, same_origin_error, same_origin_attempted = _fetch_chatgpt_session_via_same_origin(
            page,
            cookies_dict,
            log,
            session_url,
        )
        if same_origin_result:
            return same_origin_result
        if same_origin_attempted and same_origin_error:
            last_error = same_origin_error
            log(f"ChatGPT session API 浏览器内请求暂未拿到 token: {last_error}")
            if "object has no attribute 'evaluate'" not in last_error:
                time.sleep(2)
                continue

        try:
            response = page.goto(session_url, wait_until="domcontentloaded", timeout=15000)
            status = int(response.status if response else 0)
            if response:
                try:
                    text = response.text()
                except Exception as body_exc:
                    last_error = str(body_exc)
                    log(f"ChatGPT session API 响应体不可直接读取，改读页面正文: {last_error}")
                    text = page.locator("body").inner_text(timeout=3000)
            else:
                text = page.locator("body").inner_text(timeout=3000)
            current_url = str(getattr(page, "url", "") or "")
            log(f"ChatGPT session API 状态: {status} url={current_url[:120]}")
            if status == 200 and text:
                result, error = _chatgpt_session_result_from_text(text, page, cookies_dict, log)
                if result:
                    return result
                last_error = error
            else:
                last_error = f"session API HTTP {status}: {text[:200]}"
            log(f"ChatGPT session API 暂未拿到 token: {last_error}")
        except Exception as exc:
            last_error = str(exc)
            log(f"ChatGPT session API 打开异常: {last_error}")
        time.sleep(2)

    raise RuntimeError(f"ChatGPT session 未返回 accessToken: {last_error}")


def _random_chrome_ua() -> str:
    patch = random.randint(0, 220)
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/136.0.7103.{patch} Safari/537.36"
    )


def _infer_sec_ch_ua(user_agent: str) -> str:
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    major = str(match.group(1) if match else "136")
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'


def _build_browser_headers(
    *,
    user_agent: str,
    accept: str,
    referer: str = "",
    origin: str = "",
    content_type: str = "",
    navigation: bool = False,
    extra_headers: dict | None = None,
) -> dict:
    headers = {
        "user-agent": user_agent or _random_chrome_ua(),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": _infer_sec_ch_ua(user_agent),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "accept": accept,
    }
    if referer:
        headers["referer"] = referer
    if origin:
        headers["origin"] = origin
    if content_type:
        headers["content-type"] = content_type
    if navigation:
        headers["sec-fetch-dest"] = "document"
        headers["sec-fetch-mode"] = "navigate"
        headers["sec-fetch-user"] = "?1"
        headers["upgrade-insecure-requests"] = "1"
    else:
        headers["sec-fetch-dest"] = "empty"
        headers["sec-fetch-mode"] = "cors"
    for key, value in dict(extra_headers or {}).items():
        if value is not None:
            headers[key] = value
    return headers


def _browser_pause(page, *, headed: bool = True):
    delay_ms = random.randint(150, 450) if headed else random.randint(60, 180)
    try:
        page.wait_for_timeout(delay_ms)
    except Exception:
        time.sleep(delay_ms / 1000)


def _generate_datadog_trace_headers() -> dict:
    trace_hex = secrets.token_hex(8).rjust(16, "0")
    parent_hex = secrets.token_hex(8).rjust(16, "0")
    trace_id = str(int(trace_hex, 16))
    parent_id = str(int(parent_hex, 16))
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _infer_page_type(data: dict | None, current_url: str = "") -> str:
    raw = data if isinstance(data, dict) else {}
    url = (current_url or "").lower()
    try:
        hostname = str(urlparse(current_url or "").hostname or "").lower()
    except Exception:
        hostname = ""
    if hostname == "accounts.google.com" or hostname.endswith(".accounts.google.com"):
        return "google_oauth"
    page_type = str(((raw.get("page") or {}).get("type")) or "").strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    if page_type:
        return page_type
    if "code=" in url:
        return "oauth_callback"
    if "create-account/password" in url:
        return "create_account_password"
    if "email-verification" in url or "email-otp" in url:
        return "email_otp_verification"
    if "about-you" in url:
        return "about_you"
    if "log-in/password" in url:
        return "login_password"
    if "choose-an-account" in url:
        return "account_chooser"
    if "sign-in-with-chatgpt" in url and "consent" in url:
        return "consent"
    if "workspace" in url and "select" in url:
        return "workspace_selection"
    if "organization" in url and "select" in url:
        return "organization_selection"
    if "add-phone" in url:
        return "add_phone"
    if "/api/oauth/oauth2/auth" in url:
        return "external_url"
    if "chatgpt.com" in url:
        return "chatgpt_home"
    return ""


def _extract_flow_state(data: dict | None, current_url: str = "") -> dict:
    raw = data if isinstance(data, dict) else {}
    page = raw.get("page") or {}
    payload = page.get("payload") or {}
    continue_url = str(raw.get("continue_url") or payload.get("url") or "").strip()
    if continue_url and continue_url.startswith("/"):
        continue_url = urljoin(OPENAI_AUTH, continue_url)
    effective_url = continue_url or current_url
    return {
        "page_type": _infer_page_type(raw, effective_url),
        "continue_url": continue_url,
        "method": str(raw.get("method") or payload.get("method") or "GET").upper(),
        "current_url": effective_url,
        "payload": payload if isinstance(payload, dict) else {},
        "raw": raw,
    }


def _extract_code_from_url(url: str) -> str:
    if not url or "code=" not in url:
        return ""
    try:
        from urllib.parse import parse_qs, urlparse as _up

        parsed = _up(url)
        values = parse_qs(parsed.query, keep_blank_values=True)
        return str((values.get("code") or [""])[0] or "").strip()
    except Exception:
        return ""


def _normalize_url(target_url: str, base_url: str = OPENAI_AUTH) -> str:
    value = str(target_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    try:
        return urljoin(base_url, value)
    except Exception:
        return value


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * ((4 - (len(payload) % 4)) % 4)
        return json.loads(base64.urlsafe_b64decode((payload + pad).encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


class _SentinelTokenGenerator:
    def __init__(self, device_id: str, user_agent: str):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or _random_chrome_ua()
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        return f"{h & 0xFFFFFFFF:08x}"

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def _config(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            "1920x1080",
            time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            int(time.time() * 1000 - perf_now),
        ]

    def generate_requirements_token(self) -> str:
        cfg = self._config()
        cfg[3] = 1
        cfg[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        max_attempts = 500000
        cfg = self._config()
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = round(int(time.time() * 1000) - start_ms)
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None)


def _browser_fetch(page, url: str, *, method: str = "GET", headers: dict | None = None, body: str | None = None, redirect: str = "manual", timeout_ms: int = 30000) -> dict:
    return page.evaluate(
        """
        async ({ url, method, headers, body, redirect, timeoutMs }) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(new Error(`fetch timeout after ${timeoutMs}ms`)), timeoutMs);
          try {
            const resp = await fetch(url, {
              method,
              headers: headers || {},
              body: body === null ? undefined : body,
              redirect,
              signal: controller.signal,
            });
            const respHeaders = {};
            resp.headers.forEach((v, k) => { respHeaders[k] = v; });
            let text = '';
            try { text = await resp.text(); } catch {}
            let data = null;
            try { data = JSON.parse(text); } catch {}
            return { ok: resp.ok, status: resp.status, url: resp.url || url, headers: respHeaders, text, data };
          } catch (e) {
            return { ok: false, status: 0, url, headers: {}, text: String(e && e.message || e), data: null };
          } finally {
            clearTimeout(timer);
          }
        }
        """,
        {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
            "redirect": redirect,
            "timeoutMs": timeout_ms,
        },
    )


def _build_browser_sentinel_token(page, device_id: str, flow: str, user_agent: str) -> str:
    generator = _SentinelTokenGenerator(device_id, user_agent)
    req_body = json.dumps(
        {"p": generator.generate_requirements_token(), "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    result = _browser_fetch(
        page,
        SENTINEL_REQ_URL,
        method="POST",
        headers=_build_browser_headers(
            user_agent=user_agent,
            accept="*/*",
            referer=SENTINEL_FRAME_URL,
            origin=SENTINEL_BASE,
            content_type="text/plain;charset=UTF-8",
            extra_headers={
                "sec-fetch-site": "same-origin",
            },
        ),
        body=req_body,
        redirect="follow",
    )
    data = result.get("data") or {}
    challenge_token = str(data.get("token") or "").strip()
    if not challenge_token:
        return ""
    pow_meta = data.get("proofofwork") or {}
    if pow_meta.get("required") and pow_meta.get("seed"):
        p_value = generator.generate_token(str(pow_meta.get("seed") or ""), str(pow_meta.get("difficulty") or "0"))
    else:
        p_value = generator.generate_requirements_token()
    return json.dumps(
        {
            "p": p_value,
            "t": "",
            "c": challenge_token,
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )


def _is_registration_complete(state: dict) -> bool:
    page_type = str(state.get("page_type") or "")
    url = str(state.get("current_url") or state.get("continue_url") or "").lower()
    return page_type in {"callback", "oauth_callback", "chatgpt_home"} or (
        "chatgpt.com" in url and "redirect_uri" not in url and "about-you" not in url
    )


def _handle_post_signup_onboarding(page, log) -> None:
    current_url = str(page.url or "")
    if "chatgpt.com" not in current_url:
        return
    try:
        # 可能弹出 persistent storage 提示，优先点 Allow，不影响主流程也可点 Block。
        allow_selector = _click_first(
            page,
            [
                'button:has-text("Allow")',
                'button:has-text("allow")',
                'button:has-text("Block")',
                'button:has-text("block")',
                'button:has-text("許可")',
                'button:has-text("ブロック")',
                'button:has-text("拒否")',
            ],
            timeout=1,
        )
        if allow_selector:
            log(f"已处理浏览器弹窗: {allow_selector}")
    except Exception:
        pass

    # 新账号常见 onboarding 问卷页，优先 Skip。
    try:
        if _locator_is_visible(page.locator("text=What brings you to ChatGPT?"), timeout=500):
            skip_selector = _click_first(
                page,
                [
                    'button:has-text("Skip")',
                    'button:has-text("skip")',
                    'button:has-text("Next")',
                    'button:has-text("next")',
                    'button:has-text("スキップ")',
                    'button:has-text("次へ")',
                ],
                timeout=5,
            )
            if skip_selector:
                log(f"已处理 onboarding 页面: {skip_selector}")
                _browser_pause(page)
    except Exception:
        pass


def _is_password_registration(state: dict) -> bool:
    return str(state.get("page_type") or "") in {"create_account_password", "password"}


def _is_email_otp(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "email_otp_verification" or "email-verification" in target or "email-otp" in target


def _is_about_you(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "about_you" or "about-you" in target


def _requires_registration_navigation(state: dict) -> bool:
    if str(state.get("method") or "GET").upper() != "GET":
        return False
    if str(state.get("page_type") or "") == "external_url" and state.get("continue_url"):
        return True
    continue_url = str(state.get("continue_url") or "")
    current_url = str(state.get("current_url") or "")
    return bool(continue_url and continue_url != current_url)


def _browser_add_cookies(page, cookies: list[dict]) -> None:
    try:
        page.context.add_cookies(cookies)
    except Exception:
        pass


def _seed_browser_device_id(page, device_id: str) -> None:
    _browser_add_cookies(
        page,
        [
            {"name": "oai-did", "value": device_id, "domain": "chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "auth.openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".auth.openai.com", "path": "/"},
        ],
    )


def _get_browser_csrf_token(page) -> str:
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/csrf",
        method="GET",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "sec-fetch-site": "same-origin",
        },
        redirect="follow",
    )
    if result.get("ok") and isinstance(result.get("data"), dict):
        return str((result.get("data") or {}).get("csrfToken") or "").strip()
    return ""


def _start_browser_signin(page, email: str, device_id: str, csrf_token: str) -> str:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "prompt": "login",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
    )
    body = urlencode(
        {
            "callbackUrl": f"{CHATGPT_APP}/",
            "csrfToken": csrf_token,
            "json": "true",
        }
    )
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
        method="POST",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "origin": CHATGPT_APP,
            "content-type": "application/x-www-form-urlencoded",
            "sec-fetch-site": "same-origin",
        },
        body=body,
        redirect="follow",
    )
    if result.get("ok") and isinstance(result.get("data"), dict):
        return str((result.get("data") or {}).get("url") or "").strip()
    return ""


def _browser_authorize(page, auth_url: str, log) -> str:
    if not auth_url:
        return ""
    try:
        _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
        final_url = page.url
        log(f"Authorize -> {final_url[:120]}")
        return final_url
    except Exception as exc:
        log(f"Authorize 失败: {exc}")
        return ""


def _submit_oauth_password_direct(page, password: str, log) -> dict:
    """OAuth 流程专用：直接填密码登录，不尝试恢复到注册态。"""
    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        # 密码输入框没出现，可能页面还在加载或跳转了
        # 等一下再试
        time.sleep(2)
        input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=10)
    if not input_selector:
        raise RuntimeError("OAuth 密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("OAuth 密码页填写失败")
    log(f"  OAuth 密码页输入框: {input_selector}")
    _browser_pause(page)

    submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"  OAuth 密码页已点击继续按钮: {submit_selector}")
    elif _press_enter_on_input(page, input_selector):
        log("  OAuth 密码页未找到可点击 Continue，已在密码输入框按 Enter")
    else:
        raise RuntimeError("OAuth 密码页未找到 Continue 按钮")

    submit_started_at = time.time()
    deadline = submit_started_at + 45
    extended_wait_logged = False
    while time.time() < deadline:
        current_url = str(page.url or "")
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {"email_otp_verification", "about_you", "consent", "workspace_selection",
                         "organization_selection", "add_phone", "oauth_callback", "chatgpt_home", "external_url"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "code=" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        error_text = _extract_auth_error_text(page)
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        if not extended_wait_logged and time.time() - submit_started_at >= 20:
            extended_wait_logged = True
            log("OAuth 密码提交后 20 秒仍无跳转且未发现错误，代理链路较慢，继续观察至 45 秒")
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": str(page.url or ""), "data": None, "text": "OAuth 密码提交后观察 45 秒仍未跳转"}


def _submit_password_via_page(page, password: str, log) -> dict:
    if _recover_signup_password_page(page, log):
        time.sleep(1)

    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        raise RuntimeError("密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("密码页填写失败")
    log(f"密码页输入框: {input_selector}")
    _browser_pause(page)

    start_url = str(page.url or "")
    submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"密码页已点击继续按钮: {submit_selector}")
    elif _press_enter_on_input(page, input_selector):
        log("密码页未找到可点击 Continue，已在密码输入框按 Enter")
    else:
        raise RuntimeError("密码页未找到 Continue 按钮")

    submit_started_at = time.time()
    deadline = submit_started_at + 45
    extended_wait_logged = False
    last_url = str(page.url or "")
    while time.time() < deadline:
        current_url = str(page.url or "")
        last_url = current_url or last_url
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {"email_otp_verification", "about_you", "add_phone", "oauth_callback", "chatgpt_home"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if current_url != start_url and page_type and page_type not in {"create_account_password", "login_password"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if page_type == "login_password" and _recover_signup_password_page(page, log):
            input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=5)
            if not input_selector:
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后未找到注册密码输入框"}
            if not _fill_input_like_user(page, input_selector, password):
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后密码重新填写失败"}
            submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=5)
            if submit_selector:
                log(f"恢复后重新点击密码提交按钮: {submit_selector}")
                start_url = str(page.url or start_url)
                time.sleep(0.4)
                continue
            if _press_enter_on_input(page, input_selector):
                log("恢复后未找到可点击密码提交按钮，已在密码输入框按 Enter")
                start_url = str(page.url or start_url)
                time.sleep(0.4)
                continue
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后未找到提交方式"}
        error_text = _extract_auth_error_text(page)
        if error_text:
            _dump_debug(page, "chatgpt_password_fail")
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        if not extended_wait_logged and time.time() - submit_started_at >= 20:
            extended_wait_logged = True
            log("密码提交后 20 秒仍无跳转且未发现错误，代理链路较慢，继续观察至 45 秒")
        time.sleep(0.5)
    _dump_debug(page, "chatgpt_password_fail")
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "密码页提交后观察 45 秒仍未跳转"}


def _submit_otp_via_page(
    page,
    code: str,
    log,
    otp_callback: Callable[[], str] | None = None,
    resend_attempts: int = 0,
) -> dict:
    otp = str(code or "").strip()
    if not otp:
        return {"ok": False, "status": 400, "url": page.url, "data": None, "text": "验证码为空"}
    otp_entry_url = str(getattr(page, "url", "") or "")

    # 等待页面加载完成，确保 OTP 输入框已渲染
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    time.sleep(1)

    filled = False
    filled_target = None
    used_dom_fallback = False

    # 先尝试 6 格 OTP 输入框
    try:
        digit_inputs = page.locator(
            "input[inputmode='numeric'], input[autocomplete='one-time-code'], input[type='tel'], input[type='number']"
        )
        visible_digit_inputs = []
        for i in range(max(8, len(otp))):
            try:
                box = digit_inputs.nth(i)
                if box.is_visible(timeout=200):
                    visible_digit_inputs.append(box)
            except Exception:
                continue
        if len(visible_digit_inputs) >= len(otp):
            done = 0
            for i in range(min(len(visible_digit_inputs), len(otp))):
                box = visible_digit_inputs[i]
                try:
                    box.wait_for(state="visible", timeout=800)
                    box.fill("")
                    box.type(otp[i], delay=random.randint(20, 60))
                    done += 1
                except Exception:
                    break
            if done >= len(otp):
                filled = True
                filled_target = visible_digit_inputs[max(done - 1, 0)]
                log(f"验证码页已填写 {done} 位分格输入框")
    except Exception:
        pass

    # 再尝试单输入框
    if not filled:
        otp_candidates = [
            page.get_by_label(re.compile(r"verification code|code|otp|認証コード|確認コード|ワンタイムコード", re.IGNORECASE)),
            page.get_by_role("textbox", name=re.compile(r"verification code|code|otp|認証コード|確認コード|ワンタイムコード", re.IGNORECASE)),
            page.locator("input[autocomplete='one-time-code']"),
            page.locator("input[name*='code' i]"),
            page.locator("input[id*='code' i]"),
            page.locator("input[type='text']"),
            page.locator("input"),
        ]
        for candidate in otp_candidates:
            try:
                target = candidate.first
                target.wait_for(state="visible", timeout=1200)
                try:
                    target.click(timeout=1200)
                except Exception:
                    target.focus(timeout=1000)
                cleared = False
                for shortcut in ("Control+A", "Meta+A"):
                    try:
                        target.press(shortcut, timeout=1000)
                        target.press("Backspace", timeout=1000)
                        cleared = True
                        break
                    except Exception:
                        continue
                if not cleared:
                    target.fill("")
                target.type(otp, delay=random.randint(18, 45))
                final_value = str(target.input_value() or "").strip()
                if final_value:
                    filled = True
                    filled_target = target
                    log("验证码页已填写单输入框")
                    break
            except Exception:
                continue

    if not filled:
        # 再等 3 秒重试一次（页面可能还在渲染）
        time.sleep(3)
        otp_retry_selectors = [
            "input[inputmode='numeric']",
            "input[autocomplete='one-time-code']",
            "input[name*='code' i]",
            "input[type='text']",
        ]
        for sel in otp_retry_selectors:
            try:
                target = page.locator(sel).first
                if target.is_visible(timeout=2000):
                    target.click(timeout=1500)
                    target.fill("")
                    target.type(otp, delay=random.randint(18, 45))
                    if str(target.input_value() or "").strip():
                        filled = True
                        filled_target = target
                        log("验证码页已填写单输入框(重试)")
                        break
            except Exception:
                continue

    if not filled:
        try:
            result = page.evaluate(
                """
                (otp) => {
                  const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden'
                      && rect.width > 0 && rect.height > 0;
                  };
                  const selectors = [
                    'input[autocomplete="one-time-code"]',
                    'input[name="code"]',
                    'input[name*="code" i]',
                    'input[id*="code" i]',
                    'input[inputmode="numeric"]',
                    'input[type="text"]',
                  ];
                  let input = null;
                  let selector = '';
                  for (const candidate of selectors) {
                    input = Array.from(document.querySelectorAll(candidate))
                      .find((el) => visible(el) && !el.disabled && !el.readOnly);
                    if (input) {
                      selector = candidate;
                      break;
                    }
                  }
                  if (!input) return { ok: false, reason: 'no-input' };
                  input.focus();
                  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                  if (setter) setter.call(input, '');
                  else input.value = '';
                  input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
                  if (setter) setter.call(input, otp);
                  else input.value = otp;
                  input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: otp }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  input.dispatchEvent(new Event('blur', { bubbles: true }));
                  return { ok: String(input.value || '').trim() === String(otp), selector, value: String(input.value || '') };
                }
                """,
                otp,
            )
            if isinstance(result, dict) and result.get("ok"):
                filled = True
                used_dom_fallback = True
                log(f"验证码页已使用 DOM fallback 填写输入框: {result.get('selector') or '-'}")
        except Exception as exc:
            log(f"验证码页 DOM fallback 填写失败: {exc}")

    if not filled:
        return {"ok": False, "status": 0, "url": page.url, "data": None, "text": "验证码页未找到可填写输入框"}

    # OpenAI's current single OTP input can auto-submit as soon as the final
    # digit is entered.  Issuing another locator query while that navigation is
    # in flight can block the sync Playwright connection until the outer worker
    # watchdog fires.  Observe the URL first and avoid a redundant click when
    # the page has already accepted the code.
    log("验证码已填入，检查页面是否自动提交")
    if _wait_for_otp_submit_progress(page, start_url=otp_entry_url, timeout=3):
        current_url = str(getattr(page, "url", "") or otp_entry_url)
        log("验证码输入后页面已自动提交")
        return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
    post_fill_error = _extract_auth_error_text(page)
    if post_fill_error:
        return {
            "ok": False,
            "status": 400,
            "url": str(getattr(page, "url", "") or otp_entry_url),
            "data": _auth_error_data(post_fill_error),
            "text": post_fill_error,
        }

    # A process-local sleep provides the same human-like pause without asking
    # the browser transport to service wait_for_timeout during navigation.
    time.sleep(random.uniform(0.15, 0.45))

    # Reuse the already-resolved input handle before making any new locator
    # query.  This avoids a known Camoufox/Playwright stall while OpenAI swaps
    # the OTP document and also matches the form's normal keyboard behavior.
    if filled_target is not None:
        log("验证码未自动提交，先在已填写输入框按 Enter")
        try:
            filled_target.press("Enter", timeout=1500)
        except Exception as exc:
            log(f"验证码输入框按 Enter 未确认完成: {str(exc)[:160]}")
        if _wait_for_otp_submit_progress(page, start_url=otp_entry_url, timeout=5):
            current_url = str(getattr(page, "url", "") or otp_entry_url)
            log("验证码输入框按 Enter 后页面已推进")
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}

    log("验证码未自动提交，查找继续按钮")
    submit_selector = _click_otp_submit_button(page, log, timeout=8)
    if not submit_selector and used_dom_fallback:
        log("验证码页 DOM fallback 后提交按钮不可点击，改用键盘重新输入验证码")
        if _fill_otp_with_keyboard_fallback(page, otp, log):
            _browser_pause(page)
            submit_selector = _click_otp_submit_button(page, log, timeout=5)
    if not submit_selector:
        for input_selector in OTP_INPUT_SELECTORS:
            if not _press_enter_on_input(page, input_selector):
                continue
            log(f"验证码页提交按钮不可点击，已在验证码输入框按 Enter: {input_selector}")
            if _wait_for_otp_submit_progress(page, start_url=str(page.url or ""), timeout=8):
                submit_selector = f"{input_selector} keyboard Enter"
            break
    if not submit_selector:
        log("验证码页未确认点击成功，继续观察页面是否已延迟跳转")
        if _wait_for_otp_submit_progress(page, start_url=str(page.url or ""), timeout=12):
            submit_selector = "delayed submit progress"
    if not submit_selector:
        error_text = _extract_auth_error_text(page)
        if error_text:
            return {
                "ok": False,
                "status": 400,
                "url": page.url,
                "data": _auth_error_data(error_text),
                "text": error_text,
            }
        return {
            "ok": False,
            "status": 0,
            "url": page.url,
            "data": None,
            "text": "验证码页未找到可点击 Continue 按钮",
        }
    log(f"验证码页已点击继续按钮: {submit_selector}")

    submit_started_at = time.time()
    deadline = submit_started_at + 45
    extended_wait_logged = False
    last_url = page.url
    while time.time() < deadline:
        current_url = page.url
        last_url = current_url or last_url
        if "about-you" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if (
            "add-phone" in current_url
            or "create-account/password" in current_url
            or "log-in/password" in current_url
            or "chatgpt.com" in current_url
            or "code=" in current_url
        ):
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "consent" in current_url or "sign-in-with-chatgpt" in current_url or "workspace" in current_url or "organization" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        error_text = _extract_auth_error_text(page)
        normalized_error = str(error_text or "").lower()
        incorrect_code = any(
            token in normalized_error
            for token in (
                "invalid code",
                "incorrect code",
                "expired code",
                "code has expired",
                "验证码错误",
                "验证码无效",
                "验证码已过期",
                "無効なコード",
                "コードが無効",
                "コードの有効期限が切れ",
                "認証コードが正しくありません",
            )
        )
        if incorrect_code and callable(otp_callback) and resend_attempts < 2:
            resend_attempts += 1
            refresh_before_ids = getattr(otp_callback, "refresh_before_ids", None)
            if callable(refresh_before_ids):
                try:
                    refresh_before_ids()
                except Exception as exc:
                    log(f"验证码页刷新邮件基线失败: {exc}")
            resend_selector = _click_first(
                page,
                [
                    'button[type="submit"][name="intent"][value="resend"]',
                    'button:has-text("Resend email")',
                    'button:has-text("Resend")',
                    'button:has-text("重新发送")',
                    'button:has-text("重发")',
                    'button:has-text("メールを再送信する")',
                    'button:has-text("再送信")',
                ],
                timeout=5,
            )
            if not resend_selector:
                return {
                    "ok": False,
                    "status": 400,
                    "url": current_url,
                    "data": _auth_error_data(error_text),
                    "text": error_text,
                }
            log(f"验证码无效或过期，已重发邮件 ({resend_attempts}/2): {resend_selector}")
            time.sleep(1)
            new_code = str(otp_callback() or "").strip()
            if not new_code:
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "重发验证码后未获取到新验证码"}
            return _submit_otp_via_page(
                page,
                new_code,
                log,
                otp_callback=otp_callback,
                resend_attempts=resend_attempts,
            )
        if error_text:
            return {
                "ok": False,
                "status": 400,
                "url": current_url,
                "data": _auth_error_data(error_text),
                "text": error_text,
            }
        if not extended_wait_logged and time.time() - submit_started_at >= 20:
            extended_wait_logged = True
            log("验证码提交后 20 秒仍无跳转且未发现错误，代理链路较慢，继续观察至 45 秒")
        time.sleep(0.5)
    state_summary = _summarize_otp_submit_state(page)
    return {
        "ok": False,
        "status": 0,
        "url": last_url,
        "data": None,
        "text": f"验证码页提交后观察 45 秒仍未跳转: {state_summary}",
    }


def _submit_about_you_via_page(page, log) -> dict:
    from .constants import generate_random_user_info

    user_info = generate_random_user_info()
    name = str(user_info.get("name") or "").strip()
    birthdate = str(user_info.get("birthdate") or "").strip()
    if not name or not birthdate:
        raise RuntimeError("about_you 数据生成失败")
    date_parts = birthdate.split("-")
    if len(date_parts) == 3:
        yyyy, mm, dd = date_parts
        us_birthdate = f"{mm}/{dd}/{yyyy}"
        cn_birthdate = f"{yyyy}/{mm}/{dd}"
    else:
        us_birthdate = birthdate
        cn_birthdate = birthdate.replace("-", "/")
    log(f"about_you 表单: name={name}, birthdate={birthdate}, ui_birthdate={us_birthdate}, cn_birthdate={cn_birthdate}")

    def _fill_locator(locator, value: str) -> bool:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=1500)
            target.click(timeout=1500)
            _browser_pause(page, headed=False)
            try:
                applied = bool(
                    target.evaluate(
                        """
                        (input, nextValue) => {
                          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                          if (!setter) return false;
                          setter.call(input, nextValue);
                          input.dispatchEvent(new Event('input', { bubbles: true }));
                          input.dispatchEvent(new Event('change', { bubbles: true }));
                          return String(input.value || '') === String(nextValue || '');
                        }
                        """,
                        value,
                    )
                )
            except Exception:
                applied = False
            if not applied:
                target.fill("")
                target.type(value, delay=random.randint(25, 70))
            try:
                target.dispatch_event("blur")
            except Exception:
                pass
            final_val = str(target.input_value() or "").strip()
            return final_val == str(value).strip()
        except Exception:
            return False

    def _locator_from_visible_input_entry(entry: dict):
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            return None
        return page.locator(ABOUT_YOU_VALUE_INPUT_SELECTOR).nth(visible_index)

    def _fill_visible_input_entry(entry: dict | None, value: str) -> bool:
        if not entry:
            return False
        locator = _locator_from_visible_input_entry(entry)
        if locator is None:
            return False
        return _fill_locator(locator, value)

    def _resolve_visible_input_selector(selectors: list[str]) -> str | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=500)
                return selector
            except Exception:
                continue
        return None

    def _fill_second_visible_input(values: list[str], excluded_visible_indices: set[int] | None = None) -> bool:
        """兜底：about_you 卡片一般是 Full name + Birthday/Age 两个输入框。"""
        try:
            locator = page.locator(ABOUT_YOU_VALUE_INPUT_SELECTOR)
            if not _locator_is_visible(locator.nth(1)):
                return False
            excluded = {int(value) for value in (excluded_visible_indices or set())}
            target_index = None
            for idx in range(6):
                if not _locator_is_visible(locator.nth(idx), timeout=200):
                    continue
                if idx not in excluded:
                    target_index = idx
                    if idx > 0:
                        break
            if target_index is None:
                return False
            target = locator.nth(target_index)
            target.click(timeout=1200)
            _browser_pause(page, headed=False)
            for value in values:
                try:
                    target.fill("")
                except Exception:
                    pass
                try:
                    target.type(str(value), delay=random.randint(18, 45))
                except Exception:
                    continue
                final_val = str(target.input_value() or "").strip()
                if final_val:
                    return True
            return False
        except Exception:
            return False

    def _has_visible(locator) -> bool:
        try:
            locator.first.wait_for(state="visible", timeout=700)
            return True
        except Exception:
            return False

    def _fill_birthday_selects(yyyy: str, mm: str, dd: str) -> bool:
        """处理 Month/Day/Year 下拉样式的生日控件。"""
        try:
            select_locator = page.locator("select:visible")
            visible_selects = [
                select_locator.nth(index)
                for index in range(5)
                if _locator_is_visible(select_locator.nth(index), timeout=200)
            ]
            if len(visible_selects) < 2:
                return False

            month_num = int(mm)
            day_num = int(dd)
            year_num = int(yyyy)
            month_short = time.strftime("%b", time.strptime(str(month_num), "%m"))
            month_full = time.strftime("%B", time.strptime(str(month_num), "%m"))

            assigned = {"month": False, "day": False, "year": False}

            for sel in visible_selects:
                try:
                    texts = list(
                        sel.evaluate(
                            "(el) => Array.from(el.options || []).slice(0, 80).map((option) => String(option.textContent || '').trim())"
                        )
                        or []
                    )
                except Exception:
                    texts = []
                if not texts:
                    continue
                joined = " ".join(texts).lower()

                try:
                    if (not assigned["month"]) and (
                        "january" in joined or "february" in joined or "march" in joined or "april" in joined
                    ):
                        for candidate in (month_full, month_short, str(month_num), f"{month_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["month"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["month"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["year"]) and any(str(y) in joined for y in (year_num, year_num - 1, year_num + 1, 2026, 2025)):
                        for candidate in (str(year_num),):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["year"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["year"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["day"]) and any(str(x) in joined for x in (" 1 ", "2", "30", "31")):
                        for candidate in (str(day_num), f"{day_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["day"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["day"] = True
                                    break
                                except Exception:
                                    continue
                except Exception:
                    continue

            # 下拉顺序兜底：month/day/year
            if len(visible_selects) >= 3:
                try:
                    if not assigned["month"]:
                        visible_selects[0].select_option(label=month_short, timeout=800)
                        assigned["month"] = True
                except Exception:
                    pass
                try:
                    if not assigned["day"]:
                        visible_selects[1].select_option(label=str(day_num), timeout=800)
                        assigned["day"] = True
                except Exception:
                    pass
                try:
                    if not assigned["year"]:
                        visible_selects[2].select_option(label=str(year_num), timeout=800)
                        assigned["year"] = True
                except Exception:
                    pass

            return assigned["month"] and assigned["day"] and assigned["year"]
        except Exception:
            return False

    visible_inputs = _collect_visible_text_inputs(page)
    if visible_inputs:
        log(
            "about_you 可见输入框: "
            + " | ".join(
                f"#{int(item.get('visibleIndex', 0))} {(_about_you_input_hints(item) or '-')[:80]}"
                for item in visible_inputs[:4]
            )
        )
    ordered_visible_entries = sorted(
        [item for item in visible_inputs if str(item.get("visibleIndex", "")).isdigit()],
        key=lambda item: int(item.get("visibleIndex", 0)),
    )
    name_entry = _pick_best_about_you_input(visible_inputs, "name")
    age_entry = _pick_best_about_you_input(
        visible_inputs,
        "age",
        exclude_visible_indices={int(name_entry.get("visibleIndex"))} if name_entry and str(name_entry.get("visibleIndex", "")).isdigit() else set(),
    )

    name_candidates = [
        page.get_by_label(re.compile(r"full\s*name", re.IGNORECASE)),
        page.get_by_label(re.compile(r"全名|姓名|氏名|お名前|フルネーム", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"full\s*name|name", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"全名|姓名|氏名|お名前|フルネーム", re.IGNORECASE)),
        page.locator("input[autocomplete='name']"),
        page.locator("input[name*='name' i]"),
        page.locator("input[id*='name' i]"),
        page.locator("input[name*='姓名']"),
        page.locator("input[id*='姓名']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'full name')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'全名') or contains(normalize-space(string(.)),'姓名')]/following::input[1]"),
    ]
    birthday_candidates = [
        page.get_by_label(re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_label(re.compile(r"生日|出生|生年月日|誕生日", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"生日|出生|生年月日|誕生日", re.IGNORECASE)),
        page.get_by_placeholder(re.compile(r"mm.?dd.?yyyy|yyyy.?mm.?dd|birthday|生日|生年月日|誕生日", re.IGNORECASE)),
        page.locator("input[name*='birth' i]"),
        page.locator("input[id*='birth' i]"),
        page.locator("input[placeholder*='MM' i]"),
        page.locator("input[placeholder*='DD' i]"),
        page.locator("input[placeholder*='YYYY' i]"),
        page.locator("input[placeholder*='年']"),
        page.locator("input[placeholder*='月']"),
        page.locator("input[placeholder*='日']"),
        page.locator("input[inputmode='numeric']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'birthday')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'生日') or contains(normalize-space(string(.)),'出生')]/following::input[1]"),
        page.locator("input[type='date']"),
    ]

    age_years = None
    try:
        birth_year, birth_month, birth_day = (int(part) for part in str(birthdate).split("-"))
        today = time.localtime()
        age_years = today.tm_year - birth_year - (
            (today.tm_mon, today.tm_mday) < (birth_month, birth_day)
        )
        if not 18 <= age_years <= 120:
            raise ValueError("generated age is outside the accepted adult range")
    except Exception:
        age_years = random.randint(25, 35)

    age_candidates = [
        page.get_by_label(re.compile(r"age", re.IGNORECASE)),
        page.get_by_label(re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"age", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.locator("input[name*='age' i]"),
        page.locator("input[id*='age' i]"),
        page.locator("input[placeholder*='Age' i]"),
        page.locator("input[placeholder*='年龄']"),
        page.locator("input[placeholder*='年齢']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'age')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'年龄')]/following::input[1]"),
    ]

    fill_result = {"name": False, "birthdate": False, "age": False, "month": False, "day": False, "year": False}
    if _fill_visible_input_entry(name_entry, name):
        fill_result["name"] = True
    if not fill_result.get("name"):
        for candidate in name_candidates:
            if _fill_locator(candidate, name):
                fill_result["name"] = True
                break
    mode_probe = {}
    try:
        mode_probe = page.evaluate(
            """
            () => {
              const labels = Array.from(document.querySelectorAll('label'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const placeholders = Array.from(document.querySelectorAll('input'))
                .map((n) => String(n.placeholder || '').trim().toLowerCase())
                .filter(Boolean);
              const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const allText = labels.concat(placeholders).concat(headings);
              const hasAge = allText.some((t) => t === 'age' || t === 'edad' || t === 'âge' || t === 'alter' || t === 'idade' || t === '年齢' || t.includes('how old') || t.includes('年龄') || t.includes('年齢') || t.includes('나이') || t.includes('연령'));
              const hasBirthday = allText.some((t) =>
                t.includes('birthday') || t.includes('date of birth') || t.includes('birth') || t.includes('生日') || t.includes('出生') || t.includes('生年月日') || t.includes('誕生日') || t.includes('fecha de nacimiento') || t.includes('nascimento') || t.includes('geburtstag') || t.includes('naissance') || t.includes('생년월일') || t.includes('생일')
              );
              return { labels, placeholders, headings, hasAge, hasBirthday };
            }
            """
        ) or {}
    except Exception:
        mode_probe = {}

    has_birthday_select = False
    try:
        has_birthday_select = _locator_is_visible(page.locator("select:visible").nth(1), timeout=250)
    except Exception:
        has_birthday_select = False
    try:
        has_segmented_birthday = all(
            _locator_is_visible(page.locator(f'[data-type="{part}"]:visible'), timeout=250)
            for part in ("month", "day", "year")
        )
    except Exception:
        has_segmented_birthday = False
    about_mode = _infer_about_you_mode(
        visible_inputs,
        mode_probe,
        has_birthday_select=has_birthday_select,
        has_segmented_birthday=has_segmented_birthday,
    )
    semantic_age_fields = sum(
        1 for entry in visible_inputs if _about_you_input_has_semantic_field(entry, "age")
    )
    semantic_birthday_fields = sum(
        1 for entry in visible_inputs if _about_you_input_has_semantic_field(entry, "birthday")
    )
    log(
        f"about_you 页面模式: {about_mode} semantic_age={semantic_age_fields} "
        f"semantic_birthday={semantic_birthday_fields} labels={mode_probe.get('labels', [])[:4]}"
    )
    direct_name_selector = _resolve_visible_input_selector(
        [
            'input[name="name"]',
            'input[name="full_name"]',
            'input[autocomplete="name"]',
            'input[placeholder*="全名"]',
            'input[placeholder*="name" i]',
            'input[id*="name" i]:not([type="hidden"])',
        ]
    )
    direct_age_selector = _resolve_visible_input_selector(
        [
            'input[name="age"]',
            'input[placeholder="Age"]',
            'input[placeholder="age"]',
            'input[placeholder*="年龄"]',
            'input[id*="age" i]',
        ]
    )
    if about_mode == "age" and len(ordered_visible_entries) >= 2:
        if not name_entry:
            name_entry = ordered_visible_entries[0]
        if not age_entry:
            excluded_name_index = int(name_entry.get("visibleIndex", -1)) if name_entry else -1
            age_entry = next(
                (
                    entry
                    for entry in ordered_visible_entries
                    if int(entry.get("visibleIndex", -1)) != excluded_name_index
                ),
                None,
            )
    if about_mode == "age" and name_entry and age_entry:
        log(
            f"about_you age 输入框映射: name=#{int(name_entry.get('visibleIndex', 0))}, "
            f"age=#{int(age_entry.get('visibleIndex', 0))}"
        )
    if about_mode == "age":
        log(
            "about_you age 直接定位: "
            f"name={direct_name_selector or '-'}, age={direct_age_selector or '-'}"
        )

    def _verify_age_value(expected_age: int) -> bool:
        candidates = []
        if direct_age_selector:
            candidates.append(page.locator(direct_age_selector).first)
        entry_locator = _locator_from_visible_input_entry(age_entry) if age_entry else None
        if entry_locator is not None:
            candidates.append(entry_locator)
        for target in candidates:
            try:
                actual = str(target.input_value(timeout=700) or "").strip()
                valid = bool(
                    target.evaluate(
                        "(el) => typeof el.checkValidity !== 'function' || el.checkValidity()"
                    )
                )
                if actual == str(expected_age) and valid:
                    return True
            except Exception:
                continue
        return False

    def _fill_segmented_date(mm: str, dd: str, yyyy: str) -> bool:
        """处理 MM / DD / YYYY 分段日期输入框（React DateField 样式）。
        特征：一个 Birthday label 下有多个小 input 或 div[data-type] 段。"""
        def _clear_focused_segment() -> None:
            for shortcut in ("Control+A", "Meta+A"):
                try:
                    page.keyboard.press(shortcut)
                    page.keyboard.press("Backspace")
                    time.sleep(0.1)
                    return
                except Exception:
                    continue

        def _typed_year_is_four_digits() -> bool:
            try:
                return bool(
                    page.evaluate(
                        """
                        (expectedYear) => {
                          const text = String(document.body?.innerText || '');
                          const active = document.activeElement;
                          const activeText = String(active?.value || active?.textContent || '').trim();
                          const yearNodes = Array.from(document.querySelectorAll('[data-type="year"], input[placeholder*="YYYY" i], input[aria-label*="year" i]'));
                          const yearText = yearNodes.map((node) => String(node.value || node.textContent || '').trim()).join(' ');
                          return activeText.includes(expectedYear) || yearText.includes(expectedYear) || text.includes(expectedYear);
                        }
                        """,
                        yyyy,
                    )
                )
            except Exception:
                return False

        try:
            # 方式1: div[data-type] 段 (React Aria DateField)
            month_seg = page.locator('div[data-type="month"], input[data-type="month"]')
            day_seg = page.locator('div[data-type="day"], input[data-type="day"]')
            year_seg = page.locator('div[data-type="year"], input[data-type="year"]')
            if all(_locator_is_visible(segment, timeout=300) for segment in (month_seg, day_seg, year_seg)):
                month_seg.first.click(force=True)
                _clear_focused_segment()
                page.keyboard.type(mm, delay=50)
                time.sleep(0.3)
                day_seg.first.click(force=True)
                _clear_focused_segment()
                page.keyboard.type(dd, delay=50)
                time.sleep(0.3)
                year_seg.first.click(force=True)
                _clear_focused_segment()
                page.keyboard.type(yyyy, delay=50)
                time.sleep(0.3)
                if _typed_year_is_four_digits():
                    return True

            # 方式2: 单个 date input 里有 MM/DD/YYYY 占位符
            # 点击输入框，然后按顺序输入 MM DD YYYY（Tab 切换段）
            date_input = page.locator("input[placeholder*='MM'], input[placeholder*='mm'], input[type='date']")
            if _locator_is_visible(date_input, timeout=300):
                date_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(f"{mm}/{dd}/{yyyy}", delay=50)
                time.sleep(0.3)
                if _typed_year_is_four_digits():
                    return True

            # 方式3: Birthday label 下的第二个可见 input，直接点击后按数字键输入
            birthday_input = page.get_by_label(re.compile(r"birthday|birth", re.IGNORECASE))
            if _locator_is_visible(birthday_input, timeout=300):
                birthday_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(f"{mm}/{dd}/{yyyy}", delay=50)
                time.sleep(0.3)
                if _typed_year_is_four_digits():
                    return True

            # 方式4: 第二个可见 input（name 是第一个）
            inputs = page.locator(ABOUT_YOU_VALUE_INPUT_SELECTOR)
            if _locator_is_visible(inputs.nth(1), timeout=300):
                target = inputs.nth(1)
                target.click(force=True)
                time.sleep(0.3)
                # 先清空
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                time.sleep(0.1)
                # 输入 MM，Tab 到 DD，Tab 到 YYYY
                page.keyboard.type(f"{mm}/{dd}/{yyyy}", delay=80)
                time.sleep(0.3)
                # 验证是否填入了正确的值
                val = str(target.input_value() or "").strip()
                if yyyy in val:
                    return True
                # 如果直接输入不行，试 Tab 切换
                target.click(force=True)
                time.sleep(0.2)
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                for i, part in enumerate([mm, dd, yyyy]):
                    page.keyboard.type(part, delay=80)
                    if i < 2:
                        page.keyboard.press("Tab")
                        time.sleep(0.2)
                time.sleep(0.3)
                return _typed_year_is_four_digits()
        except Exception:
            pass
        return False

    if about_mode == "birthday_select":
        if len(date_parts) == 3 and _fill_birthday_selects(yyyy, mm, dd):
            fill_result["month"] = True
            fill_result["day"] = True
            fill_result["year"] = True
            fill_result["birthdate"] = True
    elif about_mode == "age":
        if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
            fill_result["name"] = True
        elif _fill_visible_input_entry(name_entry, name):
            fill_result["name"] = True
        if age_years is not None:
            if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                fill_result["age"] = True
            elif _fill_visible_input_entry(age_entry, str(age_years)):
                fill_result["age"] = True
            if not fill_result.get("age") and len(ordered_visible_entries) < 2:
                for candidate in age_candidates:
                    if _fill_locator(candidate, str(age_years)):
                        fill_result["age"] = True
                        break
        # fallback: 直接找 placeholder="Age" 的输入框
        if not fill_result.get("age") and age_years is not None and len(ordered_visible_entries) < 2:
            try:
                age_input = page.locator("input[placeholder='Age'], input[placeholder='age']")
                if _locator_is_visible(age_input, timeout=300):
                    age_input.first.click(force=True)
                    time.sleep(0.2)
                    age_input.first.fill("")
                    age_input.first.type(str(age_years), delay=random.randint(30, 60))
                    fill_result["age"] = True
            except Exception:
                pass
        if not fill_result.get("age") and age_years is not None:
            excluded_indices = set()
            if name_entry and str(name_entry.get("visibleIndex", "")).isdigit():
                excluded_indices.add(int(name_entry.get("visibleIndex")))
            if _fill_second_visible_input([str(age_years)], excluded_visible_indices=excluded_indices):
                fill_result["age"] = True
        if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
            fill_result["birthdate"] = True
        if fill_result.get("age") and age_years is not None:
            fill_result["age"] = _verify_age_value(age_years)
            log(f"about_you age 有效性: value_match={fill_result['age']}")
    elif about_mode == "birthday" or about_mode == "birthday_text":
        # 先尝试分段日期输入（MM / DD / YYYY 格式的 DateField）
        if len(date_parts) == 3 and _fill_segmented_date(mm, dd, yyyy):
            fill_result["birthdate"] = True
            log("about_you 使用分段日期输入成功")
        # 再尝试普通文本输入
        if not fill_result.get("birthdate"):
            for candidate in birthday_candidates:
                if _fill_locator(candidate, cn_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, cn_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
        if not fill_result.get("birthdate"):
            fallback_values = [cn_birthdate, cn_birthdate.replace("/", " / "), cn_birthdate.replace("/", ""), us_birthdate, us_birthdate.replace("/", " / "), us_birthdate.replace("/", ""), birthdate]
            if _fill_second_visible_input(fallback_values):
                fill_result["birthdate"] = True

    log(f"about_you 填写结果: {fill_result}")
    if not fill_result.get("name"):
        raise RuntimeError("about_you 未成功填写 Full name")
    if about_mode == "age" and not fill_result.get("age"):
        raise RuntimeError("about_you age 控件未通过值与有效性校验")
    if about_mode != "age" and not (
        fill_result.get("birthdate")
        or (fill_result.get("month") and fill_result.get("day") and fill_result.get("year"))
    ):
        raise RuntimeError("about_you 未成功填写 Birthday/Age")
    _check_required_about_you_consents(page, log)
    _browser_pause(page)

    about_submit_url = str(getattr(page, "url", "") or "")
    enter_selector = direct_age_selector or direct_name_selector
    if enter_selector and _press_enter_on_input(page, enter_selector):
        log(f"about_you 已从输入框按 Enter 提交: {enter_selector}")
        if _wait_for_about_you_submit_progress(page, start_url=about_submit_url, timeout=15):
            current_url = str(getattr(page, "url", "") or about_submit_url)
            log("about_you 按 Enter 后页面已推进")
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}

    submit_selector = _click_first(page, ABOUT_YOU_SUBMIT_SELECTORS, timeout=8)
    if not submit_selector:
        submit_selector = _click_visible_button_by_text(page, ABOUT_YOU_SUBMIT_TEXTS, timeout=3)
    if not submit_selector:
        current_url = str(getattr(page, "url", "") or "")
        if _about_you_submit_progress_url(current_url, about_submit_url):
            log("about_you 查找提交按钮期间页面已推进")
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        log(f"about_you 提交控件状态: {_summarize_otp_submit_state(page)}")
        raise RuntimeError("about_you 未找到提交按钮")
    log(f"about_you 已点击继续按钮: {submit_selector}")

    deadline = time.time() + 20
    retried_generic_validation = False
    last_url = page.url
    while time.time() < deadline:
        current_url = page.url
        last_url = current_url or last_url
        if "code=" in current_url or "chatgpt.com" in current_url or "sign-in-with-chatgpt" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "add-phone" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        live_state = _derive_registration_state_from_page(page)
        if str(live_state.get("page_type") or "") in {
            "chatgpt_home",
            "oauth_callback",
            "consent",
            "workspace_selection",
            "organization_selection",
            "add_phone",
        }:
            return {"ok": True, "status": 200, "url": current_url, "data": live_state, "text": ""}
        try:
            error_text = page.locator("text=Sorry, we cannot create your account").first.text_content(timeout=500)
        except Exception:
            error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=Enter a valid age to continue").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=doesn't look right").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("[role='alert']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator(".error, [class*='error'], [class*='Error']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if error_text and "oai_log" not in error_text and "SSR_HTML" not in error_text:
            normalized_error = str(error_text).strip().lower()
            if about_mode == "age" and not retried_generic_validation and "about-you" in str(current_url):
                retried_generic_validation = True
                log("about_you age 模式提交被拒，按字段语义重新同步资料与必选同意项后重试一次...")
                if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
                    fill_result["name"] = True
                elif _fill_visible_input_entry(name_entry, name):
                    fill_result["name"] = True
                elif len(ordered_visible_entries) < 2:
                    for candidate in name_candidates:
                        if _fill_locator(candidate, name):
                            fill_result["name"] = True
                            break
                if age_years is not None:
                    if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                        fill_result["age"] = True
                    elif _fill_visible_input_entry(age_entry, str(age_years)):
                        fill_result["age"] = True
                    elif len(ordered_visible_entries) < 2:
                        for candidate in age_candidates:
                            if _fill_locator(candidate, str(age_years)):
                                fill_result["age"] = True
                                break
                if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
                    fill_result["birthdate"] = True
                _check_required_about_you_consents(page, log)
                if age_years is not None and not _verify_age_value(age_years):
                    return {
                        "ok": False,
                        "status": 400,
                        "url": current_url,
                        "data": None,
                        "text": "about_you age 字段重填后仍未通过控件有效性校验",
                    }
                _browser_pause(page)
                retry_submit_selector = _click_first(
                    page,
                    [
                        'button:has-text("Finish creating account")',
                        'button:has-text("finish creating account")',
                        'button[type="submit"]',
                        'button[data-testid="continue-button"]',
                        'button:has-text("Continue")',
                        'button:has-text("continue")',
                        'button:has-text("Next")',
                        'button:has-text("next")',
                    ],
                    timeout=5,
                )
                if retry_submit_selector:
                    log(f"about_you 重试提交按钮: {retry_submit_selector}")
                    time.sleep(0.5)
                    continue
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    _dump_debug(page, "chatgpt_about_you_fail")
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "about_you 提交后未跳转"}


def _probe_password_registration_page(page, state: dict, log) -> dict:
    if not _is_email_otp(state):
        return state
    original_url = str(getattr(page, "url", "") or state.get("current_url") or "")
    log("检查验证码页面提供的官方“使用密码继续”入口")
    clicked_selector = ""
    try:
        clicked_selector = _click_first(page, PASSWORD_CONTINUE_LINK_SELECTORS, timeout=2) or ""
        if not clicked_selector:
            log("验证码页面未提供官方密码入口，保持邮箱验证码流程")
            return state
        log(f"已点击验证码页面官方密码入口: {clicked_selector}")
        deadline = time.time() + 12
        probed_state = _derive_registration_state_from_page(page)
        while time.time() < deadline and str(probed_state.get("page_type") or "") not in {
            "create_account_password",
            "password",
            "login_password",
        }:
            time.sleep(0.25)
            probed_state = _derive_registration_state_from_page(page)
        if _is_password_registration(probed_state):
            log("官方入口进入密码创建页，继续密码注册流程")
            return probed_state
        if str(probed_state.get("page_type") or "") == "login_password":
            log("官方入口进入已有账号密码页，切换已有账号登录流程")
            return probed_state
        log(
            "官方密码入口未进入可识别的密码页: "
            f"page={probed_state.get('page_type') or '-'} url={str(getattr(page, 'url', '') or '')[:110]}"
        )
    except Exception as exc:
        log(f"点击官方密码入口失败，恢复原验证码页面: {str(exc).splitlines()[0][:180]}")
    if clicked_selector and original_url:
        _goto_with_retry(page, original_url, wait_until="domcontentloaded", timeout=30000, log=log)
    return _derive_registration_state_from_page(page)


def _browser_registration_flow(
    page,
    email: str,
    password: str,
    otp_callback,
    log,
    *,
    prefer_password_registration: bool = False,
    password_provided: bool = True,
    existing_account_callback: Optional[Callable[..., None]] = None,
    existing_account_only: bool = False,
) -> dict:
    flow_label = "重新登录" if existing_account_only else "注册"
    device_id = str(uuid.uuid4())
    _seed_browser_device_id(page, device_id)
    try:
        log(f"使用 ChatGPT NextAuth {flow_label}入口启动浏览器{flow_label}")
        state = _start_browser_signup_via_authorize(page, email, device_id, log)
    except Exception as exc:
        log(f"ChatGPT NextAuth {flow_label}入口失败: {exc}")
        state = _derive_registration_state_from_page(page)
        if str(state.get("page_type") or "") == "chatgpt_home":
            fallback_cookies = _get_cookies(page)
            authenticated_cookie_names = {
                "login_session",
                "oai-client-auth-session",
                "next-auth.session-token",
                "__Secure-next-auth.session-token",
            }
            if not authenticated_cookie_names.intersection(fallback_cookies):
                log("NextAuth 访问失败后页面 URL 虽为首页，但没有认证 Cookie，忽略假完成状态")
                state = {}
        if not str(state.get("page_type") or ""):
            log(f"NextAuth 未返回可继续状态，改用可见 OpenAI {flow_label}页面")
            if existing_account_only:
                state = _start_browser_signup_via_page(page, email, log, flow_label=flow_label)
            else:
                state = _start_browser_signup_via_page(page, email, log)
    auth_cookies = _get_cookies(page)
    log(
        "授权态 cookies: "
        f"login_session={'yes' if auth_cookies.get('login_session') else 'no'}, "
        f"oai-did={'yes' if auth_cookies.get('oai-did') else 'no'}"
    )
    log(f"{flow_label}状态起点: page={state.get('page_type') or '-'} url={(state.get('current_url') or '')[:100]}")
    if prefer_password_registration:
        state = _probe_password_registration_page(page, state, log)
    register_submitted = False
    existing_account_detected = False
    login_auth_mode = ""
    seen_states: dict[str, int] = {}

    for step in range(12):
        signature = "|".join(
            [
                str(state.get("page_type") or ""),
                str(state.get("method") or ""),
                str(state.get("continue_url") or ""),
                str(state.get("current_url") or ""),
            ]
        )
        seen_states[signature] = seen_states.get(signature, 0) + 1
        log(
            f"{flow_label}状态推进: step={step+1} page={state.get('page_type') or '-'} "
            f"next={str(state.get('continue_url') or '')[:60]} seen={seen_states[signature]}"
        )
        if seen_states[signature] > 2:
            raise RuntimeError(f"{flow_label}状态卡住: page={state.get('page_type') or '-'}")

        if _is_registration_complete(state):
            _handle_post_signup_onboarding(page, log)
            final_state = _extract_flow_state(None, page.url)
            final_state["registration_auth_mode"] = login_auth_mode or ("password" if register_submitted else "email_otp")
            if existing_account_detected or existing_account_only:
                final_state["existing_account"] = True
                final_state["account_status"] = "existing_account"
            return final_state

        if str(state.get("page_type") or "") == "google_oauth":
            reason = "该邮箱已通过 Google 账户注册 ChatGPT"
            if callable(existing_account_callback):
                existing_account_callback(reason)
            log(f"检测到 Google OAuth 跳转，停止注册并停用邮箱: {reason}")
            raise ExistingAccountAuthenticationError(reason)

        if _is_password_registration(state):
            if existing_account_only:
                raise ExistingAccountAuthenticationError(
                    "重新登录进入新账号密码创建页，已拒绝继续注册"
                )
            if register_submitted:
                raise RuntimeError("重复进入密码注册阶段")
            log("提交注册密码...")
            pre_cookies = _get_cookies(page)
            log(
                "密码阶段 cookies: "
                f"login_session={'yes' if pre_cookies.get('login_session') else 'no'}, "
                f"oai-client-auth-session={'yes' if pre_cookies.get('oai-client-auth-session') else 'no'}"
            )
            reg_resp = _submit_password_via_page(page, password, log)
            log(f"密码页提交状态: {reg_resp.get('status', 0)}")
            if not reg_resp.get("ok"):
                raise RuntimeError(f"密码页提交失败: {(reg_resp.get('text') or '')[:300]}")
            register_submitted = True
            state = _extract_flow_state(reg_resp.get("data"), reg_resp.get("url", page.url))
            if not state.get("page_type") or _is_password_registration(state):
                state = _derive_registration_state_from_page(page)
            continue

        if str(state.get("page_type") or "") == "login_password":
            if not existing_account_detected and callable(existing_account_callback):
                existing_account_callback()
            existing_account_detected = True
            if existing_account_only:
                log("检测到已有账号(login_password)，执行重新登录认证")
            else:
                log("检测到已有账号(login_password)，停止注册流程，切换登录认证")

            if password_provided and str(password or "").strip():
                log("使用调用方显式提供的已有账号密码登录")
                login_resp = _submit_oauth_password_direct(page, password, log)
                log(f"已有账号密码登录提交状态: {login_resp.get('status', 0)}")
                if login_resp.get("ok"):
                    login_auth_mode = "password"
                    state = _extract_flow_state(login_resp.get("data"), login_resp.get("url", page.url))
                    if not state.get("page_type"):
                        state = _derive_registration_state_from_page(page)
                    continue
                password_error = str(login_resp.get("text") or login_resp.get("url") or "")[:300]
                if callable(otp_callback) and _click_passwordless_login_if_available(
                    page, log, context="已有账号密码校验失败"
                ):
                    log("已有账号真实密码不可用，改用邮箱一次性验证码登录")
                    login_auth_mode = "email_otp"
                    state = _wait_for_passwordless_login_state(page)
                    if str(state.get("page_type") or "") == "login_password":
                        raise ExistingAccountAuthenticationError(
                            "已有账号密码失败，选择一次性验证码后仍停留在密码页"
                        )
                    continue
                raise ExistingAccountAuthenticationError(f"已有账号真实密码登录失败: {password_error}")

            if not callable(otp_callback):
                raise ExistingAccountAuthenticationError(
                    "检测到已有账号，但未提供真实密码或可用的邮箱一次性验证码"
                )
            log("未提供已有账号真实密码，禁止使用随机注册密码，改用邮箱一次性验证码登录")
            if not _click_passwordless_login_if_available(page, log, context="已有账号登录"):
                raise ExistingAccountAuthenticationError(
                    "已有账号密码页未找到一次性验证码登录入口"
                )
            login_auth_mode = "email_otp"
            state = _wait_for_passwordless_login_state(page)
            if str(state.get("page_type") or "") == "login_password":
                raise ExistingAccountAuthenticationError(
                    "选择一次性验证码后仍停留在已有账号密码页"
                )
            continue

        if _is_email_otp(state):
            if not otp_callback:
                raise RuntimeError("ChatGPT 注册需要邮箱验证码但未提供 otp_callback")
            log("等待 ChatGPT 验证码")
            code = otp_callback()
            if not code:
                raise RuntimeError("未获取到验证码")
            otp_resp = _submit_otp_via_page(page, code, log, otp_callback=otp_callback)
            log(f"验证码页提交状态: {otp_resp.get('status', 0)}")
            if not otp_resp.get("ok"):
                raise RuntimeError(f"验证码校验失败: {(otp_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(otp_resp.get("data"), otp_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _is_about_you(state):
            if existing_account_only:
                raise ExistingAccountAuthenticationError(
                    "重新登录进入新账号资料页，已拒绝继续注册"
                )
            log("提交 about_you 信息...")
            target_url = _normalize_url(
                str(state.get("current_url") or state.get("continue_url") or f"{OPENAI_AUTH}/about-you"),
                OPENAI_AUTH,
            )
            if "about-you" not in str(page.url):
                log(f"跳转到 about_you 页面: {target_url[:120]}")
                _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            about_resp = _submit_about_you_via_page(page, log)
            if (
                not about_resp.get("ok")
                and "about_you 提交后未跳转" in str(about_resp.get("text") or "")
            ):
                log("about_you 提交后观察 20 秒仍确认停留当前页，刷新后重填并重试一次")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    log(f"about_you 刷新失败，重新打开页面重试: {exc}")
                    _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
                about_resp = _submit_about_you_via_page(page, log)
            log(f"about_you 提交状态: {about_resp.get('status', 0)}")
            if not about_resp.get("ok"):
                raise RuntimeError(f"about_you 提交失败: {(about_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(about_resp.get("data"), about_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _requires_registration_navigation(state):
            target_url = _normalize_url(str(state.get("continue_url") or state.get("current_url") or ""), OPENAI_AUTH)
            if not target_url:
                raise RuntimeError("缺少可跟随的 continue_url")
            _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            state = _extract_flow_state(None, page.url)
            continue

        raise RuntimeError(f"未支持的{flow_label}状态: page={state.get('page_type') or '-'}")

    raise RuntimeError(f"{flow_label}状态机超出最大步数")


class ChatGPTBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        post_codex_oauth: bool = False,
        codex_phone_callback: Optional[Callable[[], str]] = None,
        codex_oauth_timeout: int = 300,
        keep_browser_open: bool = False,
        prefer_password_registration: bool = False,
        existing_account_callback: Optional[Callable[..., None]] = None,
        existing_account_only: bool = False,
        cancel_check: Optional[Callable[[], bool]] = None,
        worker_idle_timeout: float = 120,
        worker_hard_timeout: float = 0,
        flow_mode: str = "dom",
        log_fn: Callable[[str], None] = print,
        backend_config: Optional[BrowserBackendConfig] = None,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.post_codex_oauth = bool(post_codex_oauth)
        self.codex_phone_callback = codex_phone_callback
        self.codex_oauth_timeout = int(codex_oauth_timeout or 300)
        self.keep_browser_open = bool(keep_browser_open)
        self.prefer_password_registration = bool(prefer_password_registration)
        self.existing_account_callback = existing_account_callback
        self.existing_account_only = bool(existing_account_only)
        self.cancel_check = cancel_check if callable(cancel_check) else (lambda: False)
        self.worker_idle_timeout = max(float(worker_idle_timeout or 120), 1.0)
        configured_hard_timeout = float(worker_hard_timeout or 0)
        self.worker_hard_timeout = (
            max(configured_hard_timeout, self.worker_idle_timeout)
            if configured_hard_timeout > 0
            else 0.0
        )
        self.flow_mode = str(flow_mode or "dom").strip().lower()
        self.log = log_fn
        # backend_config 为 None 时默认 Camoufox，跟老调用方一致。
        # BitBrowser 路径需要上层 plugin.py 显式传 backend_config。
        self.backend_config = backend_config or BrowserBackendConfig.camoufox(
            headless=bool(headless)
        )
        if self.backend_config.is_bitbrowser:
            log_fn(
                f"ChatGPT 注册使用 BitBrowser backend "
                f"(profile={self.backend_config.bit_profile_id}, "
                f"window_mode={self.backend_config.window_mode})"
            )

    def run_isolated(self, email: str, password: str, *, password_provided: bool = True) -> dict:
        """Run the browser state machine in a watchdog-supervised process.

        Playwright/Camoufox protocol calls can become permanently blocked after
        a driver crash.  The parent cannot stop a Python thread in that state,
        but it can terminate this disposable process and its browser children.
        """
        if self.keep_browser_open and not self.backend_config.is_headless:
            self.log("完成后保留浏览器窗口与独立子进程不兼容，本次使用进程内浏览器模式")
            return self.run(email, password, password_provided=password_provided)

        from core.isolated_worker import IsolatedCall, run_isolated_call

        callback_flags = {
            "otp": callable(self.otp_callback),
            "phone": callable(self.codex_phone_callback),
            "existing_account": callable(self.existing_account_callback),
        }
        phone_attribute_values = {}
        if callable(self.codex_phone_callback):
            phone_attribute_values["phone_max_attempts"] = int(
                getattr(self.codex_phone_callback, "phone_max_attempts", 3) or 3
            )
        config = {
            "init": {
                "headless": self.headless,
                "proxy": self.proxy,
                "post_codex_oauth": self.post_codex_oauth,
                "codex_oauth_timeout": self.codex_oauth_timeout,
                "keep_browser_open": False,
                "prefer_password_registration": self.prefer_password_registration,
                "existing_account_only": self.existing_account_only,
                "flow_mode": self.flow_mode,
            },
            "backend_config": {
                "backend": self.backend_config.backend,
                "window_mode": self.backend_config.window_mode,
                "bit_profile_id": self.backend_config.bit_profile_id,
                "bit_api_url": self.backend_config.bit_api_url,
                "bit_api_token": self.backend_config.bit_api_token,
            },
            "callbacks": callback_flags,
            "phone_attribute_values": phone_attribute_values,
            "run": {
                "email": email,
                "password": password,
                "password_provided": bool(password_provided),
            },
        }
        callbacks = {}
        if callback_flags["otp"]:
            callbacks["otp"] = self.otp_callback
        if callback_flags["phone"]:
            callbacks["phone"] = self.codex_phone_callback
        if callback_flags["existing_account"]:
            callbacks["existing_account"] = self.existing_account_callback

        return run_isolated_call(
            IsolatedCall(
                callable_path="platforms.chatgpt.browser_register:_run_chatgpt_browser_process",
                args=(config,),
            ),
            callbacks=callbacks,
            log_fn=self.log,
            cancel_check=self.cancel_check,
            idle_timeout=self.worker_idle_timeout,
            hard_timeout=self.worker_hard_timeout,
        )

    def _open_browser(self, launch_opts: dict):
        """与业务代码代期使用的 ``with Camoufox(**launch_opts) as browser:`` 接口
        保持兑现：按 ``self.backend_config`` 路由到 Camoufox 或 BitBrowser。
        BitBrowser 路径下 launch_opts 里的 proxy/geoip 会被忽略（profile
        自带代理）。"""
        _apply_camoufox_visible_window_limit(launch_opts, self.backend_config)
        return open_browser_backend(
            launch_opts=launch_opts,
            config=self.backend_config,
            camoufox_class=Camoufox,
            log=self.log,
        )

    def run(self, email: str, password: str, *, password_provided: bool = True) -> dict:
        if self.backend_config.is_bitbrowser:
            # BitBrowser 路径：profile 已配代理/指纹，launch_opts 不传这些。
            launch_opts = {"headless": self.backend_config.is_headless}
        else:
            proxy = _build_proxy_config(self.proxy)
            launch_opts = {"headless": self.headless}
            if proxy:
                launch_opts["proxy"] = proxy
                launch_opts["geoip"] = True

        browser_context = self._open_browser(launch_opts)
        browser = browser_context.__enter__()
        keep_open = bool(self.keep_browser_open and not self.backend_config.is_headless)
        completed = False
        try:
            page = browser.new_page()
            flow_label = "重新登录" if self.existing_account_only else "注册"
            if self.flow_mode == "browser_protocol":
                if self.existing_account_only:
                    raise ExistingAccountAuthenticationError(
                        "Browser Protocol 当前仅用于新账号注册"
                    )
                from .browser_protocol_register import browser_protocol_registration_flow

                self.log("启动 Browser Protocol 页面内 Fetch 状态机")
                final_state = browser_protocol_registration_flow(
                    page,
                    email,
                    password,
                    self.otp_callback,
                    self.log,
                    existing_account_callback=self.existing_account_callback,
                    cancel_check=self.cancel_check,
                )
            else:
                self.log(f"启动浏览器上下文{flow_label}状态机")
                final_state = _browser_registration_flow(
                    page,
                    email,
                    password,
                    self.otp_callback,
                    self.log,
                    prefer_password_registration=self.prefer_password_registration,
                    password_provided=password_provided,
                    existing_account_callback=self.existing_account_callback,
                    existing_account_only=self.existing_account_only,
                )
            self.log(f"{flow_label}流程完成: page={final_state.get('page_type') or '-'}")

            # 获取 session token 和 cookies
            cookies_dict = _get_cookies(page)
            session_info = _fetch_chatgpt_session_from_page(page, cookies_dict, self.log)
            result_password = password
            if (
                final_state.get("existing_account")
                and final_state.get("registration_auth_mode") != "password"
            ):
                # The supplied password was either absent or rejected before
                # the successful OTP fallback.  Do not persist it as a valid
                # credential for this existing account.
                result_password = ""
            result = {
                "email": email,
                "password": result_password,
                "account_id": session_info.get("account_id", ""),
                "access_token": session_info.get("access_token", ""),
                "refresh_token": session_info.get("refresh_token", ""),
                "id_token": session_info.get("id_token", ""),
                "session_token": session_info.get("session_token", ""),
                "workspace_id": session_info.get("workspace_id", ""),
                "cookies": session_info.get("cookies", "") or _cookies_to_header(cookies_dict),
                "profile": session_info.get("profile", {}),
                "expires_at": session_info.get("expires_at", ""),
                "session": session_info.get("session", {}),
                "registration_state": final_state,
                "registration_auth_mode": final_state.get("registration_auth_mode") or "email_otp",
            }
            if self.post_codex_oauth:
                self.log("注册后动作: 复用当前浏览器窗口执行 Codex OAuth")
                from platforms.chatgpt.codex_oauth import perform_codex_oauth_login_on_page

                try:
                    codex_result = perform_codex_oauth_login_on_page(
                        page,
                        email=email,
                        password=result_password,
                        registration_auth_mode=result["registration_auth_mode"],
                        proxy=self.proxy,
                        log_fn=self.log,
                        otp_callback=self.otp_callback,
                        phone_callback=self.codex_phone_callback,
                        timeout=self.codex_oauth_timeout,
                    )
                    result.update(codex_result)
                    result["post_codex_oauth"] = {"ok": True}
                except Exception as exc:
                    result["post_codex_oauth"] = {"ok": False, "error": str(exc)}
                    self.log(f"注册后 Codex OAuth 授权失败: {exc}")
            completed = True
            return result
        finally:
            if keep_open and completed:
                keep_browser_context_open(browser_context, browser, label=f"chatgpt-register:{email}")
                self.log("浏览器窗口已保留，可手动关闭")
            else:
                browser_context.__exit__(*sys.exc_info())


def _run_chatgpt_browser_process(channel, config: dict) -> dict:
    """Child-process entrypoint used by :meth:`run_isolated`."""
    init_kwargs = dict(config.get("init") or {})
    callback_flags = dict(config.get("callbacks") or {})
    backend_config = BrowserBackendConfig(**dict(config.get("backend_config") or {}))

    otp_callback = channel.callback("otp") if callback_flags.get("otp") else None
    phone_callback = None
    if callback_flags.get("phone"):
        phone_callback = channel.callback(
            "phone",
            attribute_values=dict(config.get("phone_attribute_values") or {}),
        )
    existing_account_callback = (
        channel.callback("existing_account")
        if callback_flags.get("existing_account")
        else None
    )
    worker = ChatGPTBrowserRegister(
        **init_kwargs,
        otp_callback=otp_callback,
        codex_phone_callback=phone_callback,
        existing_account_callback=existing_account_callback,
        log_fn=channel.log,
        backend_config=backend_config,
    )
    run_kwargs = dict(config.get("run") or {})
    email = str(run_kwargs.pop("email", "") or "")
    password = str(run_kwargs.pop("password", "") or "")
    return worker.run(email, password, **run_kwargs)
