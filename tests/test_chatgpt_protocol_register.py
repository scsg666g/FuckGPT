from __future__ import annotations

import json
import sys
import types

import pytest

from platforms.chatgpt.constants import CHATGPT_APP, OPENAI_API_ENDPOINTS, SENTINEL_REQ_URL
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.protocol_register import (
    ChatGPTProtocolRegister,
    OpenAISentinelClient,
    _SentinelBrowserRuntime,
)
from platforms.chatgpt.browser_register import (
    ABOUT_YOU_SUBMIT_SELECTORS,
    ABOUT_YOU_SUBMIT_TEXTS,
    ChatGPTBrowserRegister,
    ExistingAccountAuthenticationError,
    OTP_SUBMIT_SELECTORS,
    _about_you_checkbox_is_master,
    _about_you_checkbox_is_required,
    _browser_registration_flow,
    _check_required_about_you_consents,
    _click_first,
    _click_visible_button_by_text,
    _click_otp_submit_button,
    _collect_visible_text_inputs,
    _extract_auth_error_text,
    _derive_registration_state_from_page,
    _infer_about_you_mode,
    _is_account_deactivated_error,
    _is_login_password_url,
    _pick_best_about_you_input,
    _press_enter_on_input,
    _probe_password_registration_page,
    _start_browser_signup_via_authorize,
    _submit_password_via_page,
    _submit_otp_via_page,
    _wait_for_about_you_submit_progress,
)


def test_google_oauth_url_wins_over_hidden_password_inputs():
    class HiddenNode:
        def is_visible(self):
            return False

    class Page:
        url = (
            "https://accounts.google.com/v3/signin/identifier?"
            "redirect_uri=https%3A%2F%2Fauth.openai.com%2Fapi%2Faccounts%2Fcallback%2Fgoogle"
        )

        def query_selector(self, _selector):
            return HiddenNode()

    state = _derive_registration_state_from_page(Page())

    assert state["page_type"] == "google_oauth"


def test_google_oauth_stops_registration_and_marks_mailbox_existing(monkeypatch):
    logs = []
    reasons = []

    class Page:
        url = "https://accounts.google.com/v3/signin/identifier"

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args, **kwargs: {"page_type": "google_oauth", "current_url": Page.url},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})

    with pytest.raises(ExistingAccountAuthenticationError, match="已通过 Google 账户注册 ChatGPT") as caught:
        _browser_registration_flow(
            Page(),
            "existing@gmail.com",
            "generated-password-must-not-be-used",
            lambda: "123456",
            logs.append,
            existing_account_callback=reasons.append,
        )

    assert caught.value.preserve_mailbox is True
    assert reasons == ["该邮箱已通过 Google 账户注册 ChatGPT"]
    assert any("停止注册并停用邮箱" in item for item in logs)


def test_about_you_mode_uses_stable_age_attributes_for_unknown_locale():
    entries = [
        {
            "visibleIndex": 0,
            "type": "text",
            "name": "name",
            "id": "_r_3_-name",
            "labels": ["성명"],
        },
        {
            "visibleIndex": 1,
            "type": "text",
            "name": "age",
            "id": "_r_3_-age",
            "inputMode": "numeric",
            "labels": ["연령"],
        },
    ]

    age_entry = _pick_best_about_you_input(entries, "age", exclude_visible_indices={0})

    assert age_entry is entries[1]
    assert _infer_about_you_mode(entries, {"hasAge": False, "hasBirthday": False}) == "age"


def test_about_you_mode_prefers_visible_birthday_semantics():
    entries = [
        {"visibleIndex": 0, "type": "text", "name": "name", "id": "profile-name"},
        {"visibleIndex": 1, "type": "date", "name": "birthdate", "id": "profile-birthdate"},
    ]

    assert _infer_about_you_mode(entries, {}) == "birthday"
    assert _infer_about_you_mode(entries[:1], {}, has_segmented_birthday=True) == "birthday"
    assert _infer_about_you_mode(entries[:1], {}, has_birthday_select=True) == "birthday_select"


def test_collect_visible_text_inputs_excludes_consent_checkboxes():
    class Page:
        def evaluate(self, _script, _selector):
            return [
                {"visibleIndex": 0, "type": "text", "name": "name"},
                {"visibleIndex": 1, "type": "number", "name": "age"},
                {"visibleIndex": 2, "type": "checkbox", "name": "allcheckboxes"},
                {"visibleIndex": 3, "type": "checkbox", "name": "privacy"},
            ]

    assert [item["name"] for item in _collect_visible_text_inputs(Page())] == ["name", "age"]


def test_about_you_consent_classifier_skips_master_and_optional_items():
    master = {"name": "allcheckboxes", "id": "_r_3_-allcheckboxes", "labels": ["Agree to all"]}
    required_native = {"name": "privacy", "required": True, "labels": ["Privacy"]}
    required_korean = {"name": "privacy", "labels": ["개인정보 수집 및 사용(필수)"]}
    optional = {"name": "marketing", "labels": ["Marketing updates (optional)"]}

    assert _about_you_checkbox_is_master(master) is True
    assert _about_you_checkbox_is_required(required_native) is True
    assert _about_you_checkbox_is_required(required_korean) is True
    assert _about_you_checkbox_is_required(optional) is False


def test_about_you_checks_only_required_individual_consents():
    entries = [
        {"visibleIndex": 0, "name": "allcheckboxes", "required": True},
        {"visibleIndex": 1, "name": "privacy", "required": True},
        {"visibleIndex": 2, "name": "marketing", "labels": ["Optional updates"]},
        {"visibleIndex": 3, "name": "terms", "ariaRequired": True, "checked": True},
    ]

    class Checkbox:
        def __init__(self, checked=False):
            self.checked = checked
            self.check_calls = 0

        def is_checked(self, **_kwargs):
            return self.checked

        def check(self, **_kwargs):
            self.check_calls += 1
            self.checked = True

        def click(self, **_kwargs):
            raise AssertionError("native check should succeed")

    controls = [Checkbox(), Checkbox(), Checkbox(), Checkbox(checked=True)]

    class Locator:
        def nth(self, index):
            return controls[index]

    class Page:
        def evaluate(self, _script):
            return entries

        def locator(self, selector):
            assert selector == "input[type='checkbox']:visible:not([disabled])"
            return Locator()

    logs = []
    assert _check_required_about_you_consents(Page(), logs.append) == 2
    assert [control.check_calls for control in controls] == [0, 1, 0, 0]
    assert "required=2" in logs[-1]
    assert "master_skipped=1" in logs[-1]


class _FakeCookies:
    def get(self, key):
        return "device-from-cookie" if key == "oai-did" else None

    def get_dict(self):
        return {"oai-did": "device-from-cookie"}


def test_login_password_url_recognizes_password_form_on_log_in_root():
    assert _is_login_password_url("https://auth.openai.com/log-in") is True
    assert _is_login_password_url("https://auth.openai.com/log-in/password") is True
    assert _is_login_password_url("https://auth.openai.com/create-account/password") is False


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.cookies = _FakeCookies()
        self.calls = []
        self.create_headers = {}
        self.password_body = {}
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url == f"{CHATGPT_APP}/api/auth/csrf":
            return _FakeResponse(payload={"csrfToken": "csrf-token"})
        if url == "https://auth.openai.com/authorize-start":
            return _FakeResponse(headers={"location": "/email-verification"})
        if url == f"{CHATGPT_APP}/api/auth/session":
            return _FakeResponse(
                payload={
                    "accessToken": "header.payload.signature",
                    "sessionToken": "session-token",
                    "expires": "2026-08-01T00:00:00Z",
                    "account": {"id": "account-123", "planType": "free"},
                }
            )
        return _FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
            return _FakeResponse(payload={"url": "https://auth.openai.com/authorize-start"})
        if url == OPENAI_API_ENDPOINTS["validate_otp"]:
            assert kwargs["json"] == {"code": "123456"}
            return _FakeResponse(payload={"continue_url": "/about-you"})
        if url == SENTINEL_REQ_URL:
            request_payload = json.loads(kwargs["data"])
            return _FakeResponse(
                payload={
                    "token": "challenge-token",
                    "proofofwork": {"required": False},
                    "flow": request_payload["flow"],
                }
            )
        if url == OPENAI_API_ENDPOINTS["create_account"]:
            self.create_headers = kwargs["headers"]
            return _FakeResponse(
                payload={
                    "continue_url": f"{CHATGPT_APP}/api/auth/callback/openai?code=ok&state=test"
                }
            )
        if url == OPENAI_API_ENDPOINTS["register"]:
            self.password_body = kwargs["json"]
            return _FakeResponse(payload={"continue_url": "/api/accounts/email-otp/send"})
        raise AssertionError(f"unexpected POST {url}")

    def close(self):
        self.closed = True


def test_browser_registration_retries_about_you_after_no_navigation(monkeypatch):
    logs = []
    responses = iter(
        [
            {"ok": False, "status": 0, "url": "https://auth.openai.com/about-you", "text": "about_you 提交后未跳转"},
            {"ok": True, "status": 200, "url": "https://chatgpt.com/", "text": ""},
        ]
    )

    class Page:
        url = "https://auth.openai.com/about-you"

        def __init__(self):
            self.reloads = 0

        def reload(self, **kwargs):
            self.reloads += 1

    page = Page()

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args, **kwargs: {"page_type": "about_you", "current_url": "https://auth.openai.com/about-you"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.browser_register._submit_about_you_via_page", lambda page, log: next(responses))
    monkeypatch.setattr("platforms.chatgpt.browser_register._extract_flow_state", lambda data, url: {"page_type": "chatgpt_home", "current_url": url})
    monkeypatch.setattr("platforms.chatgpt.browser_register._handle_post_signup_onboarding", lambda page, log: None)

    result = _browser_registration_flow(page, "user@example.com", "Secret123!", lambda: "123456", logs.append)

    assert result["page_type"] == "chatgpt_home"
    assert page.reloads == 1
    assert any("about_you 提交后观察 20 秒仍确认停留当前页" in item for item in logs)


def test_browser_registration_falls_back_to_visible_page_after_nextauth_failure(monkeypatch):
    calls = []
    logs = []

    class Page:
        url = "https://chatgpt.com/"

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("missing authorize URL")),
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._derive_registration_state_from_page", lambda page: {})
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_page",
        lambda page, email, log: calls.append(("visible", email)) or {"page_type": "chatgpt_home"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.browser_register._handle_post_signup_onboarding", lambda page, log: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._extract_flow_state",
        lambda data, url: {"page_type": "chatgpt_home", "current_url": url},
    )

    result = _browser_registration_flow(Page(), "user@example.com", "Secret123!", lambda: "123456", logs.append)

    assert result["page_type"] == "chatgpt_home"
    assert calls == [("visible", "user@example.com")]
    assert any("改用可见 OpenAI 注册页面" in item for item in logs)


def test_probe_password_registration_page_clicks_official_create_password_link(monkeypatch):
    calls = []
    logs = []

    class Page:
        url = "https://auth.openai.com/email-verification"

    page = Page()

    def fake_click(page, selectors, **kwargs):
        calls.append(list(selectors))
        page.url = "https://auth.openai.com/create-account/password"
        return 'a[href="/create-account/password"]'

    monkeypatch.setattr("platforms.chatgpt.browser_register._click_first", fake_click)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda page: {"page_type": "create_account_password", "current_url": page.url},
    )

    result = _probe_password_registration_page(
        page,
        {"page_type": "email_otp_verification", "current_url": page.url},
        logs.append,
    )

    assert result["page_type"] == "create_account_password"
    assert len(calls) == 1
    assert 'a[href="/create-account/password"]' in calls[0]
    assert any("官方入口进入密码创建页" in item for item in logs)


def test_probe_password_registration_page_keeps_otp_when_official_link_is_absent(monkeypatch):
    logs = []

    class Page:
        url = "https://auth.openai.com/email-verification"

    state = {"page_type": "email_otp_verification", "current_url": Page.url}
    monkeypatch.setattr("platforms.chatgpt.browser_register._click_first", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._goto_with_retry",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not synthesize a password URL")),
    )

    result = _probe_password_registration_page(Page(), state, logs.append)

    assert result is state
    assert any("未提供官方密码入口" in item for item in logs)


def test_probe_password_registration_page_follows_official_existing_account_link(monkeypatch):
    logs = []

    class Page:
        url = "https://auth.openai.com/email-verification"

    page = Page()

    def fake_click(page, selectors, **kwargs):
        page.url = "https://auth.openai.com/log-in/password"
        return 'a[href="/log-in/password"]'

    monkeypatch.setattr("platforms.chatgpt.browser_register._click_first", fake_click)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda page: {"page_type": "login_password", "current_url": page.url},
    )

    result = _probe_password_registration_page(
        page,
        {"page_type": "email_otp_verification", "current_url": page.url},
        logs.append,
    )

    assert result["page_type"] == "login_password"
    assert any("已有账号密码页" in item for item in logs)


def test_existing_account_never_submits_generated_registration_password(monkeypatch):
    logs = []
    calls = []
    markers = []

    class Page:
        url = "https://auth.openai.com/log-in/password"

    page = Page()

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args, **kwargs: {"page_type": "login_password", "current_url": page.url},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.browser_register._click_passwordless_login_if_available", lambda *args, **kwargs: calls.append("otp") or True)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._wait_for_passwordless_login_state",
        lambda page: {"page_type": "email_otp_verification", "current_url": "https://auth.openai.com/email-verification"},
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._submit_oauth_password_direct",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("generated password must not be submitted")),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._submit_otp_via_page",
        lambda page, code, log, **kwargs: page.__dict__.update(url="https://chatgpt.com/") or {"ok": True, "status": 200, "url": page.url},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._handle_post_signup_onboarding", lambda page, log: None)

    result = _browser_registration_flow(
        page,
        "existing@example.com",
        "generated-not-a-real-password",
        lambda: "123456",
        logs.append,
        password_provided=False,
        existing_account_callback=lambda: markers.append("existing"),
    )

    assert result["existing_account"] is True
    assert result["account_status"] == "existing_account"
    assert result["registration_auth_mode"] == "email_otp"
    assert calls == ["otp"]
    assert markers == ["existing"]
    assert any("禁止使用随机注册密码" in item for item in logs)


def test_existing_account_uses_explicit_password_when_provided(monkeypatch):
    logs = []
    submitted = []

    class Page:
        url = "https://auth.openai.com/log-in/password"

    page = Page()

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args, **kwargs: {"page_type": "login_password", "current_url": page.url},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._submit_oauth_password_direct",
        lambda page, password, log: submitted.append(password) or {"ok": True, "status": 200, "url": "https://chatgpt.com/"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._extract_flow_state", lambda data, url: {"page_type": "chatgpt_home", "current_url": url})
    monkeypatch.setattr("platforms.chatgpt.browser_register._handle_post_signup_onboarding", lambda page, log: None)

    result = _browser_registration_flow(
        page,
        "existing@example.com",
        "real-password-supplied-by-user",
        None,
        logs.append,
        password_provided=True,
    )

    assert submitted == ["real-password-supplied-by-user"]
    assert result["existing_account"] is True
    assert result["registration_auth_mode"] == "password"


def test_existing_account_otp_success_does_not_persist_rejected_password(monkeypatch):
    class Browser:
        def new_page(self):
            return object()

    class BrowserContext:
        def __enter__(self):
            return Browser()

        def __exit__(self, *args):
            return None

    worker = ChatGPTBrowserRegister(headless=True, otp_callback=lambda: "123456")
    monkeypatch.setattr(worker, "_open_browser", lambda launch_opts: BrowserContext())
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._browser_registration_flow",
        lambda *args, **kwargs: {
            "page_type": "chatgpt_home",
            "existing_account": True,
            "account_status": "existing_account",
            "registration_auth_mode": "email_otp",
        },
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.browser_register._fetch_chatgpt_session_from_page", lambda *args: {})

    result = worker.run(
        "existing@example.com",
        "rejected-user-password",
        password_provided=True,
    )

    assert result["password"] == ""
    assert result["registration_auth_mode"] == "email_otp"


def test_browser_register_isolated_serializes_config_and_callbacks(monkeypatch):
    captured = {}
    otp_callback = lambda: "123456"

    def fake_run(call, **kwargs):
        captured["call"] = call
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr("core.isolated_worker.run_isolated_call", fake_run)
    worker = ChatGPTBrowserRegister(
        headless=True,
        proxy="http://127.0.0.1:8080",
        otp_callback=otp_callback,
        worker_idle_timeout=75,
        worker_hard_timeout=700,
    )

    result = worker.run_isolated("user@example.com", "password", password_provided=False)

    assert result == {"ok": True}
    assert captured["call"].callable_path.endswith(":_run_chatgpt_browser_process")
    child_config = captured["call"].args[0]
    assert child_config["init"]["proxy"] == "http://127.0.0.1:8080"
    assert child_config["callbacks"]["otp"] is True
    assert captured["kwargs"]["callbacks"]["otp"] is otp_callback
    assert captured["kwargs"]["idle_timeout"] == 75
    assert captured["kwargs"]["hard_timeout"] == 700


def test_browser_register_total_deadline_is_disabled_by_default():
    worker = ChatGPTBrowserRegister(headless=True)

    assert worker.worker_idle_timeout == 120
    assert worker.worker_hard_timeout == 0


def test_signup_authorize_retries_csrf_without_repeating_email_submission(monkeypatch):
    csrf_values = iter(["", "", "csrf-token"])
    calls = []
    logs = []

    class Page:
        url = "https://chatgpt.com/"

    monkeypatch.setattr("platforms.chatgpt.browser_register._goto_with_retry", lambda *args, **kwargs: calls.append("goto"))
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_browser_csrf_token", lambda page: next(csrf_values))
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signin",
        lambda page, email, device_id, csrf: calls.append(("signin", email, csrf)) or "https://auth.openai.com/authorize",
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._browser_authorize",
        lambda page, url, log: calls.append(("authorize", url)) or "https://auth.openai.com/email-verification",
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda page: {"page_type": "email_otp_verification"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register.time.sleep", lambda seconds: calls.append(("sleep", seconds)))

    result = _start_browser_signup_via_authorize(Page(), "user@example.com", "device-id", logs.append)

    assert result["page_type"] == "email_otp_verification"
    assert [item for item in calls if isinstance(item, tuple) and item[0] == "signin"] == [
        ("signin", "user@example.com", "csrf-token")
    ]
    assert [item for item in calls if isinstance(item, tuple) and item[0] == "sleep"] == [
        ("sleep", 1.5),
        ("sleep", 3.0),
    ]
    assert sum("CSRF token 瞬时获取失败" in item for item in logs) == 2


def test_password_submit_observes_slow_proxy_transition_without_resubmitting(monkeypatch):
    now = {"value": 1000.0}
    calls = []
    logs = []

    class Page:
        url = "https://auth.openai.com/create-account/password"

    monkeypatch.setattr("platforms.chatgpt.browser_register._recover_signup_password_page", lambda *args: False)
    monkeypatch.setattr("platforms.chatgpt.browser_register._wait_for_any_selector", lambda *args, **kwargs: 'input[type="password"]')
    monkeypatch.setattr("platforms.chatgpt.browser_register._fill_input_like_user", lambda *args: calls.append("fill") or True)
    monkeypatch.setattr("platforms.chatgpt.browser_register._click_first", lambda *args, **kwargs: calls.append("click") or 'button[type="submit"]')
    monkeypatch.setattr("platforms.chatgpt.browser_register._browser_pause", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.browser_register._extract_auth_error_text", lambda page: "")
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda page: {"page_type": "email_otp_verification" if now["value"] >= 1025.0 else "create_account_password"},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register.time.sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    result = _submit_password_via_page(Page(), "Secret123!", logs.append)

    assert result["ok"] is True
    assert calls == ["fill", "click"]
    assert sum("密码提交后 20 秒仍无跳转" in item for item in logs) == 1


def test_click_first_skips_hidden_and_disabled_matches():
    clicked = []

    class Target:
        def __init__(self, *, visible=True, enabled=True, name=""):
            self.visible = visible
            self.enabled = enabled
            self.name = name

        def is_visible(self, **kwargs):
            return self.visible

        def is_enabled(self, **kwargs):
            return self.enabled

        def click(self, **kwargs):
            clicked.append(self.name)

    class Locator:
        def __init__(self, targets):
            self.targets = targets

        def count(self):
            return len(self.targets)

        def nth(self, index):
            return self.targets[index]

    class Page:
        def locator(self, selector):
            assert selector == 'button[type="submit"]'
            return Locator(
                [
                    Target(visible=False, name="hidden"),
                    Target(enabled=False, name="disabled"),
                    Target(name="enabled"),
                ]
            )

    selector = _click_first(Page(), ['button[type="submit"]'], timeout=1)

    assert selector == 'button[type="submit"] nth=2'
    assert clicked == ["enabled"]


def test_press_enter_on_input_uses_real_locator_keyboard_event():
    actions = []

    class Target:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            actions.append("click")

        def press(self, key, **kwargs):
            actions.append(("press", key))

    class Locator:
        first = Target()

    class Page:
        def locator(self, selector):
            assert selector == 'input[type="email"]'
            return Locator()

    assert _press_enter_on_input(Page(), 'input[type="email"]') is True
    assert actions == ["click", ("press", "Enter")]


def test_press_enter_on_input_focuses_when_pointer_click_is_intercepted():
    actions = []

    class Target:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            raise RuntimeError("pointer intercepted")

        def focus(self, **kwargs):
            actions.append("focus")

        def press(self, key, **kwargs):
            actions.append(("press", key))

    class Locator:
        first = Target()

    class Page:
        def locator(self, selector):
            return Locator()

    assert _press_enter_on_input(Page(), "input[autocomplete='one-time-code']") is True
    assert actions == ["focus", ("press", "Enter")]


def test_otp_submit_selectors_prioritize_japanese_continue_over_generic_submit():
    japanese_continue = 'button:text-is("続行")'

    assert japanese_continue in OTP_SUBMIT_SELECTORS
    assert OTP_SUBMIT_SELECTORS.index(japanese_continue) < OTP_SUBMIT_SELECTORS.index('button[type="submit"]')


def test_extract_auth_error_text_recognizes_japanese_expired_code():
    class Target:
        def __init__(self, text=""):
            self.text = text

        @property
        def first(self):
            return self

        def text_content(self, **kwargs):
            return self.text

        def inner_text(self, **kwargs):
            return self.text

    class Page:
        def locator(self, selector):
            if selector == "body":
                return Target("コードの有効期限が切れています。メールを再送信してください。")
            return Target("")

    assert _extract_auth_error_text(Page()) == "コードの有効期限が切れ"


def test_extract_auth_error_text_recognizes_account_deactivated_page():
    class Target:
        def __init__(self, text=""):
            self.text = text

        @property
        def first(self):
            return self

        def text_content(self, **kwargs):
            return self.text

        def inner_text(self, **kwargs):
            return self.text

    class Page:
        def locator(self, selector):
            if selector == "body":
                return Target(
                    "認証エラー\n"
                    "アカウントは削除または無効化されているため、ご利用いただけません。\n"
                    "誤りと思われる場合は、help.openai.comのへルプセンタ一からお問い合わせください。\n"
                    "error_code: account_deactivated\n"
                    "request_id: req_test"
                )
            return Target("")

    error_text = _extract_auth_error_text(Page())
    assert _is_account_deactivated_error(error_text)
    assert "account_deactivated" in error_text
    assert "認証エラー" in error_text
    assert "アカウントは削除または無効化されているため、ご利用いただけません" in error_text


def test_click_otp_submit_button_skips_hidden_submit():
    clicks = []

    class Button:
        def __init__(self, *, visible, enabled=True):
            self.visible = visible
            self.enabled = enabled

        def is_visible(self, **kwargs):
            return self.visible

        def is_enabled(self, **kwargs):
            return self.enabled

        def click(self, **kwargs):
            clicks.append(kwargs.get("timeout"))

    class Locator:
        def __init__(self, buttons):
            self.buttons = buttons

        def count(self):
            return len(self.buttons)

        def nth(self, index):
            return self.buttons[index]

    class Page:
        def locator(self, selector):
            if selector == 'button[type="submit"]':
                return Locator([Button(visible=False), Button(visible=True)])
            return Locator([])

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button[type="submit"] nth=1'
    assert clicks == [3000]


def test_click_otp_submit_button_does_not_use_unbounded_locator_count():
    class Button:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def evaluate(self, script):
            return "BUTTON|||continue|Continue"

        def click(self, **kwargs):
            return None

    class Locator:
        def __init__(self, available):
            self.available = available

        def count(self):
            raise AssertionError("locator.count() must not be called")

        def nth(self, index):
            if not self.available or index:
                raise IndexError(index)
            return Button()

    class Page:
        def locator(self, selector):
            return Locator(selector == 'button[data-testid="continue-button"]')

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button[data-testid="continue-button"]'


def test_click_otp_submit_button_skips_oauth_provider_submit():
    clicks = []

    class Button:
        def __init__(self, text):
            self.text = text

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def evaluate(self, script):
            return f"BUTTON|||submit|{self.text}"

        def click(self, **kwargs):
            clicks.append(self.text)

    class Locator:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Page:
        def locator(self, selector):
            if selector == 'button[type="submit"]':
                return Locator([Button("Google で続行"), Button("続行")])
            return Locator([])

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button[type="submit"] nth=1'
    assert clicks == ["続行"]


def test_click_otp_submit_button_uses_keyboard_when_pointer_click_is_intercepted(monkeypatch):
    now = {"value": 1000.0}
    actions = []

    class Button:
        def __init__(self, page):
            self.page = page

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            raise RuntimeError("pointer intercepted")

        def focus(self, **kwargs):
            actions.append("focus")

        def press(self, key, **kwargs):
            actions.append(("press", key))
            self.page.url = "https://auth.openai.com/about-you"

    class Locator:
        def __init__(self, page, available):
            self.page = page
            self.available = available

        def count(self):
            return 1 if self.available else 0

        def nth(self, index):
            if not self.available or index:
                raise IndexError(index)
            return Button(self.page)

    class Page:
        url = "https://auth.openai.com/email-verification"

        def locator(self, selector):
            return Locator(self, selector == 'button:text-is("続行")')

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register.time.sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button:text-is("続行") nth=0 keyboard Enter'
    assert actions == ["focus", ("press", "Enter")]


def test_click_otp_submit_button_uses_real_mouse_after_confirmed_hit_test(monkeypatch):
    now = {"value": 1000.0}
    actions = []

    class Button:
        def __init__(self, page):
            self.page = page

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def evaluate(self, script):
            if "join('|')" in script:
                return "BUTTON|||continue|続行"
            return True

        def bounding_box(self):
            return {"x": 10, "y": 20, "width": 100, "height": 40}

        def click(self, **kwargs):
            raise RuntimeError("pointer actionability timeout")

    class Locator:
        def __init__(self, page, available):
            self.page = page
            self.available = available

        def count(self):
            return 1 if self.available else 0

        def nth(self, index):
            if not self.available or index:
                raise IndexError(index)
            return Button(self.page)

    class Mouse:
        def __init__(self, page):
            self.page = page

        def move(self, x, y, **kwargs):
            actions.append(("move", x, y))

        def click(self, x, y):
            actions.append(("click", x, y))
            self.page.url = "https://auth.openai.com/about-you"

    class Page:
        url = "https://auth.openai.com/email-verification"

        def __init__(self):
            self.mouse = Mouse(self)

        def locator(self, selector):
            return Locator(self, selector == 'button:text-is("続行")')

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register.time.sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button:text-is("続行") nth=0 real mouse'
    assert actions == [("move", 60.0, 40.0), ("click", 60.0, 40.0)]


def test_click_otp_submit_button_treats_timeout_then_navigation_as_progress(monkeypatch):
    now = {"value": 1000.0}

    class Button:
        def __init__(self, page):
            self.page = page

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            self.page.url = "https://auth.openai.com/about-you"
            raise RuntimeError("Locator.click: Timeout 3000ms exceeded")

    class Locator:
        def __init__(self, page, available):
            self.page = page
            self.available = available

        def count(self):
            return 1 if self.available else 0

        def nth(self, index):
            if not self.available or index:
                raise IndexError(index)
            return Button(self.page)

    class Page:
        url = "https://auth.openai.com/email-verification"

        def locator(self, selector):
            return Locator(self, selector == 'button[type="submit"]')

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr("platforms.chatgpt.browser_register.time.sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))

    selector = _click_otp_submit_button(Page(), lambda message: None, timeout=1)

    assert selector == 'button[type="submit"] nth=0 delayed'


def test_about_you_submit_selectors_support_japanese_continue_button():
    clicks = []

    class Button:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            clicks.append(True)

    class Locator:
        def __init__(self, available):
            self.available = available

        def count(self):
            return 1 if self.available else 0

        def nth(self, index):
            if not self.available or index:
                raise IndexError(index)
            return Button()

    class Page:
        def locator(self, selector):
            return Locator(selector == 'button:has-text("続ける")')

    selector = _click_first(Page(), ABOUT_YOU_SUBMIT_SELECTORS, timeout=1)

    assert selector == 'button:has-text("続ける")'
    assert clicks == [True]


def test_about_you_submit_dom_text_fallback_clicks_japanese_continue_button():
    calls = []

    class Page:
        def evaluate(self, _script, texts):
            assert "続行" in texts
            calls.append(list(texts))
            return "続行"

    selector = _click_visible_button_by_text(Page(), ABOUT_YOU_SUBMIT_TEXTS, timeout=1)

    assert selector == 'text="続行"'
    assert calls


def test_click_first_does_not_use_locator_count():
    clicks = []

    class Button:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            clicks.append(True)

    class Locator:
        def count(self):
            raise AssertionError("count must not be used on dynamic auth pages")

        def nth(self, index):
            if index:
                raise IndexError(index)
            return Button()

    class Page:
        def locator(self, selector):
            return Locator()

    selector = _click_first(Page(), ["button[type='submit']"], timeout=1)

    assert selector == "button[type='submit']"
    assert clicks == [True]


@pytest.mark.parametrize(
    "url",
    [
        "https://auth.openai.com/create-account/password",
        "https://auth.openai.com/log-in/password",
    ],
)
def test_otp_submit_progress_recognizes_password_pages(url):
    from platforms.chatgpt.browser_register import _otp_submit_progress_url

    assert _otp_submit_progress_url(url) is True


def test_about_you_submit_progress_rejects_unchanged_about_you_url(monkeypatch):
    now = {"value": 1000.0}

    class Page:
        url = "https://auth.openai.com/about-you"

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register.time.sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    assert (
        _wait_for_about_you_submit_progress(
            Page(),
            start_url="https://auth.openai.com/about-you",
            timeout=1,
        )
        is False
    )


def test_about_you_submit_progress_accepts_changed_chatgpt_url(monkeypatch):
    now = {"value": 1000.0}

    class Page:
        @property
        def url(self):
            return (
                "https://auth.openai.com/about-you"
                if now["value"] < 1000.5
                else "https://chatgpt.com/"
            )

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register.time.sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    assert (
        _wait_for_about_you_submit_progress(
            Page(),
            start_url="https://auth.openai.com/about-you",
            timeout=1,
        )
        is True
    )


def test_submit_otp_keyboard_refill_after_dom_fallback_enables_submit(monkeypatch):
    logs = []
    now = {"value": 1000.0}

    class Input:
        def __init__(self, page):
            self.page = page

        @property
        def first(self):
            return self

        def wait_for(self, **kwargs):
            return None

        def click(self, **kwargs):
            return None

        def fill(self, value):
            self.page.otp_value = value

        def type(self, value, **kwargs):
            raise RuntimeError("force DOM fallback")

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def input_value(self):
            return self.page.otp_value if self.page.dom_fallback_started else ""

    class Button:
        def __init__(self, page):
            self.page = page

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return self.page.keyboard_refilled

        def click(self, **kwargs):
            self.page.url = "https://auth.openai.com/about-you"

    class Locator:
        def __init__(self, items):
            self.items = items

        @property
        def first(self):
            return self.nth(0)

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Keyboard:
        def __init__(self, page):
            self.page = page

        def press(self, key):
            if key == "Backspace":
                self.page.otp_value = ""

        def type(self, value, **kwargs):
            self.page.otp_value = value
            self.page.keyboard_refilled = True

    class Page:
        url = "https://auth.openai.com/email-verification"

        def __init__(self):
            self.otp_value = ""
            self.dom_fallback_started = False
            self.keyboard_refilled = False
            self.keyboard = Keyboard(self)

        def wait_for_load_state(self, **kwargs):
            return None

        def locator(self, selector):
            if selector == 'button[type="submit"]':
                return Locator([Button(self)])
            if selector.startswith("input") or "input[" in selector:
                return Locator([Input(self)])
            return Locator([])

        def get_by_label(self, *args, **kwargs):
            return Locator([Input(self)])

        def get_by_role(self, *args, **kwargs):
            return Locator([Input(self)])

        def evaluate(self, script, *args):
            if args:
                self.dom_fallback_started = True
                self.otp_value = str(args[0])
                return {"ok": True, "selector": 'input[autocomplete="one-time-code"]', "value": self.otp_value}
            return {"buttons": [{"text": "Continue", "visible": True, "disabled": True}], "inputs": [{"visible": True, "disabled": False, "readOnly": False, "valueLength": len(self.otp_value)}]}

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))
    monkeypatch.setattr("platforms.chatgpt.browser_register.time.time", lambda: now["value"])
    monkeypatch.setattr("platforms.chatgpt.browser_register._browser_pause", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.browser_register._extract_auth_error_text", lambda page: "")

    result = _submit_otp_via_page(Page(), "112450", logs.append)

    assert result["ok"] is True
    assert result["status"] == 200
    assert any("DOM fallback 后提交按钮不可点击" in item for item in logs)
    assert any("键盘 fallback 重新填写输入框" in item for item in logs)


def test_submit_otp_accepts_single_input_auto_submit_without_clicking_button(monkeypatch):
    logs = []

    class EmptyLocator:
        def count(self):
            return 0

    class Input:
        def __init__(self, page):
            self.page = page
            self.value = ""

        @property
        def first(self):
            return self

        def wait_for(self, **kwargs):
            return None

        def click(self, **kwargs):
            return None

        def press(self, key, **kwargs):
            if key == "Backspace":
                self.value = ""

        def fill(self, value):
            self.value = value

        def type(self, value, **kwargs):
            self.value += value
            self.page.url = "https://auth.openai.com/about-you"

        def input_value(self):
            return self.value

    class Candidate:
        def __init__(self, target):
            self.target = target

        @property
        def first(self):
            return self.target

    class Page:
        def __init__(self):
            self.url = "https://auth.openai.com/email-verification"
            self.input = Input(self)

        def wait_for_load_state(self, **kwargs):
            return None

        def locator(self, selector):
            return EmptyLocator()

        def get_by_label(self, *args, **kwargs):
            return Candidate(self.input)

        def get_by_role(self, *args, **kwargs):
            return Candidate(self.input)

    def fail_click(*args, **kwargs):
        raise AssertionError("auto-submit flow must not search for a Continue button")

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.browser_register._click_otp_submit_button", fail_click)

    result = _submit_otp_via_page(Page(), "112450", logs.append)

    assert result["ok"] is True
    assert result["url"] == "https://auth.openai.com/about-you"
    assert any("页面已自动提交" in item for item in logs)


def test_submit_otp_presses_enter_on_existing_input_before_button_scan(monkeypatch):
    logs = []

    class EmptyLocator:
        def count(self):
            return 0

    class Input:
        def __init__(self, page):
            self.page = page
            self.value = ""

        @property
        def first(self):
            return self

        def wait_for(self, **kwargs):
            return None

        def click(self, **kwargs):
            return None

        def press(self, key, **kwargs):
            if key == "Backspace":
                self.value = ""
            if key == "Enter":
                self.page.url = "https://auth.openai.com/create-account/password"

        def fill(self, value):
            self.value = value

        def type(self, value, **kwargs):
            self.value += value

        def input_value(self):
            return self.value

    class Candidate:
        def __init__(self, target):
            self.target = target

        @property
        def first(self):
            return self.target

    class Page:
        def __init__(self):
            self.url = "https://auth.openai.com/email-verification"
            self.input = Input(self)

        def wait_for_load_state(self, **kwargs):
            return None

        def locator(self, selector):
            return EmptyLocator()

        def get_by_label(self, *args, **kwargs):
            return Candidate(self.input)

        def get_by_role(self, *args, **kwargs):
            return Candidate(self.input)

    def progressed(page, *, start_url, timeout):
        return page.url != start_url

    def fail_click(*args, **kwargs):
        raise AssertionError("existing OTP input should submit before button scan")

    monkeypatch.setattr("platforms.chatgpt.browser_register.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.browser_register._wait_for_otp_submit_progress", progressed)
    monkeypatch.setattr("platforms.chatgpt.browser_register._click_otp_submit_button", fail_click)

    result = _submit_otp_via_page(Page(), "112450", logs.append)

    assert result["ok"] is True
    assert result["url"] == "https://auth.openai.com/create-account/password"
    assert any("按 Enter 后页面已推进" in item for item in logs)


def test_protocol_register_uses_supported_coherent_default_impersonation(monkeypatch):
    captured = {}
    session = object()

    def create_session(**kwargs):
        captured.update(kwargs)
        return session

    monkeypatch.setattr("platforms.chatgpt.protocol_register.requests.Session", create_session)

    worker = ChatGPTProtocolRegister(sentinel_runtime=False)

    assert worker.session is session
    assert captured["impersonate"] == "chrome136"
    assert "Chrome/136.0.0.0" in worker.user_agent


def test_protocol_register_completes_email_flow_without_browser():
    session = _FakeSession()
    logs = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        log_fn=logs.append,
        sentinel_runtime=False,
    )

    result = worker.run(email="user@outlook.com", password="StrongPass123!")

    assert result["email"] == "user@outlook.com"
    assert result["password"] == "StrongPass123!"
    assert result["access_token"] == "header.payload.signature"
    assert result["session_token"] == "session-token"
    assert result["account_id"] == "account-123"
    assert session.password_body == {
        "username": "user@outlook.com",
        "password": "StrongPass123!",
    }
    assert session.closed is True
    sentinel = json.loads(session.create_headers["openai-sentinel-token"])
    assert sentinel["flow"] == "oauth_create_account"
    assert sentinel["c"] == "challenge-token"
    request_order = [url for method, url, _ in session.calls if method == "POST"]
    assert request_order.index(OPENAI_API_ENDPOINTS["register"]) < request_order.index(
        OPENAI_API_ENDPOINTS["validate_otp"]
    )
    assert any("协议注册完成" in line for line in logs)


def test_protocol_registration_accepts_current_chatgpt_otp_subjects():
    adapter = ChatGPTPlatform().build_protocol_mailbox_adapter()

    # Current messages are titled "Your temporary ChatGPT ... code" and may
    # not contain the old OpenAI brand keyword.
    assert adapter.otp_spec is not None
    assert adapter.otp_spec.keyword == ""


def test_sentinel_headers_include_vm_and_session_observer_tokens():
    class _FakeRuntime:
        def vm_tokens(self, chat_req, cached_proof):
            return {"t": "turnstile-proof", "so": "observer-proof"}

    client = OpenAISentinelClient(
        session=object(),
        user_agent="test-agent",
        use_browser_runtime=True,
    )
    client._browser_runtime = _FakeRuntime()
    client.session = type(
        "NoNetworkSession",
        (),
        {"post": lambda *args, **kwargs: None},
    )()

    # Bypass the network challenge and exercise the header assembly using a
    # deterministic VM result.
    def fake_post(*args, **kwargs):
        return _FakeResponse(
            payload={
                "token": "challenge",
                "proofofwork": {"required": False},
            }
        )

    client.session.post = fake_post
    headers = client.build_headers("device-1", "oauth_create_account")
    assert set(headers) == {
        "openai-sentinel-token",
        "openai-sentinel-so-token",
    }
    token = json.loads(headers["openai-sentinel-token"])
    so_token = json.loads(headers["openai-sentinel-so-token"])
    assert token["t"] == "turnstile-proof"
    assert so_token["so"] == "observer-proof"


def test_sentinel_runtime_uses_camoufox_and_releases_it(monkeypatch):
    events = []
    evaluated_code = []

    class _Page:
        def goto(self, *_args, **_kwargs):
            events.append("goto")

        def evaluate(self, expression, *args):
            if "typeof sdk.__D" in expression:
                return True
            if expression == "code => window.eval(code)":
                evaluated_code.append(args[0])
            return None

    class _Browser:
        def new_page(self):
            return _Page()

    class _Camoufox:
        options = None

        def __init__(self, **options):
            type(self).options = options

        def __enter__(self):
            events.append("enter")
            return _Browser()

        def __exit__(self, *_args):
            events.append("exit")

    class _Session:
        def get(self, *_args, **_kwargs):
            return _FakeResponse(text="var SentinelSDK=before t.token=ye,t}({}); after")

    monkeypatch.setitem(
        sys.modules,
        "camoufox.sync_api",
        types.SimpleNamespace(Camoufox=_Camoufox),
    )
    monkeypatch.setattr(_SentinelBrowserRuntime, "_sdk_code", None)

    runtime = _SentinelBrowserRuntime.create(
        _Session(),
        user_agent="unused-by-camoufox",
        proxy="http://name:pass@127.0.0.1:8080",
    )
    assert _Camoufox.options["headless"] is True
    assert _Camoufox.options["block_webrtc"] is True
    assert _Camoufox.options["proxy"] == {
        "server": "http://127.0.0.1:8080",
        "username": "name",
        "password": "pass",
    }
    assert evaluated_code
    assert evaluated_code[0].startswith("window.__ProtocolSentinelSDK=")
    assert "t.___n=_n,t.__Nt=Nt,t.__D=D,t.__jt=jt" in evaluated_code[0]

    runtime.close()
    runtime.close()
    assert events.count("enter") == 1
    assert events.count("exit") == 1


def test_sentinel_runtime_reinstalls_isolated_sdk_when_reference_is_cleared():
    events = []

    class _Page:
        ready = False

        def evaluate(self, expression, *args):
            if expression == "code => window.eval(code)":
                events.append(("install", args[0]))
                self.ready = True
                return None
            if "typeof sdk.__D" in expression:
                events.append(("probe", args[0]))
                return self.ready
            raise AssertionError(expression)

    runtime = _SentinelBrowserRuntime.__new__(_SentinelBrowserRuntime)
    runtime._page = _Page()
    runtime._patched_sdk_code = "window.__ProtocolSentinelSDK={};"

    runtime._ensure_sdk_runtime()

    assert events == [
        ("probe", "__ProtocolSentinelSDK"),
        ("install", "window.__ProtocolSentinelSDK={};"),
        ("probe", "__ProtocolSentinelSDK"),
    ]


def test_sentinel_runtime_releases_failed_camoufox_startup(monkeypatch):
    events = []

    class _Camoufox:
        def __init__(self, **_options):
            pass

        def __enter__(self):
            events.append("enter")
            raise RuntimeError("Camoufox startup failed")

        def __exit__(self, *_args):
            events.append("exit")

    monkeypatch.setitem(
        sys.modules,
        "camoufox.sync_api",
        types.SimpleNamespace(Camoufox=_Camoufox),
    )

    try:
        _SentinelBrowserRuntime.create(
            object(), user_agent="unused-by-camoufox", proxy=None
        )
    except RuntimeError as exc:
        assert str(exc) == "Camoufox startup failed"
    else:
        raise AssertionError("expected Camoufox startup error")

    assert events == ["enter", "exit"]
