from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import requests

from core.base_sms import SmsActivation, SmsStatus
from core.base_platform import Account, RegisterConfig
from infrastructure.platform_runtime import (
    PERSISTED_ACTION_DATA_KEYS,
    _safe_action_result_data,
)
from core.account_graph import CODEX_CREDENTIAL_TYPES
from platforms.chatgpt.codex_oauth import (
    ACCOUNT_CHOOSER_SUBMIT_GRACE_SECONDS,
    EMAIL_SUBMIT_GRACE_SECONDS,
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    PKCECodes,
    _BrowserFetchCodexOAuthTransport,
    _CodexOAuthHTTPResult,
    _OAuthCallbackBroker,
    _OAuthCallbackServer,
    _observe_callback_request_on_page,
    _detect_codex_next_step_from_dom,
    _do_add_phone_attempt,
    _exchange_code_for_tokens,
    _resume_oauth_after_add_phone_success,
    _submit_add_phone_number,
    build_codex_authorize_url,
    _drive_codex_oauth_page,
    _drive_codex_oauth_protocol_state_machine,
    _handle_account_chooser,
    _handle_add_phone_challenge,
    _account_chooser_submission_pending,
    _get_invalid_session_error_page,
    _phone_input_contains,
    _request_codex_oauth_callback_via_browser_fetch,
    _request_codex_oauth_callback_via_protocol,
    _is_incorrect_password_error,
    _is_whatsapp_fallback_prompt,
    _normalize_cookie_mapping,
    _normalize_protocol_phone_number,
    _select_text_message_delivery,
    _workspace_id_from_payload,
    has_codex_oauth_reusable_session,
    normalize_codex_oauth_mode,
    perform_codex_oauth_login_on_page,
    perform_codex_oauth_login_with_mode,
)
from platforms.chatgpt.plugin import ChatGPTPlatform, _CodexSmsPhoneCallback, _resolve_registration_auth_mode


def test_codex_oauth_callback_broker_routes_by_state():
    broker = _OAuthCallbackBroker(port=0)
    first = _OAuthCallbackServer(port=0, state="state-one")
    second = _OAuthCallbackServer(port=0, state="state-two")
    broker._waiters = {"state-one": first, "state-two": second}

    assert broker.deliver({"code": "code-two", "state": "state-two"}) is True

    assert not first.event.is_set()
    assert second.event.is_set()
    assert second.wait(1)["code"] == "code-two"


def test_codex_token_exchange_retries_transient_proxy_error(monkeypatch):
    calls = []
    sleeps = []
    logs = []

    class Response:
        status_code = 200

        def json(self):
            return {"access_token": "access", "refresh_token": "refresh"}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) < 3:
            raise requests.exceptions.ProxyError(
                "Max retries exceeded (Caused by ProxyError('Unable to connect to proxy'))"
            )
        return Response()

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.requests.post", fake_post)
    monkeypatch.setattr("core.network_retry.time.sleep", lambda seconds: sleeps.append(seconds))

    payload = _exchange_code_for_tokens(
        "authorization-code",
        PKCECodes(code_verifier="verifier", code_challenge="challenge"),
        proxy="http://proxy.example:8080",
        log=logs.append,
    )

    assert payload["access_token"] == "access"
    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert any("自动重试" in item for item in logs)


def test_codex_oauth_callback_broker_logs_first_delivery():
    logs = []
    broker = _OAuthCallbackBroker(port=0)
    waiter = _OAuthCallbackServer(port=0, state="state-one", log=logs.append)
    broker._waiters = {"state-one": waiter}

    assert broker.deliver({"code": "code-one", "state": "state-one"}) is True
    assert broker.deliver({"code": "code-one", "state": "state-one"}) is True

    assert logs == ["Codex OAuth 本地回调已到达"]


def test_codex_oauth_page_observer_delivers_only_matching_callback_state():
    logs = []
    handlers = {}
    waiter = _OAuthCallbackServer(port=1455, state="state-one")

    class Page:
        def on(self, event, handler):
            handlers[event] = handler

    class Request:
        def __init__(self, url):
            self.url = url

    _observe_callback_request_on_page(Page(), waiter, logs.append)
    handlers["request"](Request("http://localhost:1455/auth/callback?code=wrong&state=state-two"))
    assert not waiter.event.is_set()

    handlers["request"](Request("http://localhost:1455/auth/callback?code=secret-code&state=state-one"))

    assert waiter.event.is_set()
    assert waiter.wait(1) == {"code": "secret-code", "state": "state-one", "error": "", "error_description": ""}
    assert logs == ["Codex OAuth 已从浏览器本地回调请求捕获结果"]
    assert "secret-code" not in " ".join(logs)


def test_codex_oauth_authorize_url_matches_cli_proxy_api_flow():
    url = build_codex_authorize_url(
        "state-test",
        PKCECodes(code_verifier="verifier-test", code_challenge="challenge-test"),
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == [CODEX_CLIENT_ID]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [CODEX_REDIRECT_URI]
    assert query["scope"] == [CODEX_SCOPE]
    assert query["state"] == ["state-test"]
    assert query["code_challenge"] == ["challenge-test"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["prompt"] == ["login"]
    assert query["id_token_add_organizations"] == ["true"]
    assert query["codex_cli_simplified_flow"] == ["true"]


def test_chatgpt_codex_oauth_action_uses_browser_login_flow(monkeypatch):
    captured = {}

    def fake_login(**kwargs):
        captured.update(kwargs)
        return {
            "message": "Codex OAuth 授权完成",
            "codex_auth_path": "data/codex_auths/codex-test.json",
            "codex_access_token": "access-token",
            "codex_refresh_token": "refresh-token",
            "codex_id_token": "id-token",
        }

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.perform_codex_oauth_login_with_mode", fake_login)

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "codex_oauth_authorize",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
        ),
        {},
    )

    assert result["ok"] is True
    assert captured["email"] == "user@example.com"
    assert captured["password"] == "Secret123!"
    assert captured["oauth_mode"] == "browser"
    assert captured["headless"] is False
    assert captured["keep_browser_open"] is False
    assert "phone_callback" in captured
    assert result["data"]["codex_access_token"] == "access-token"


def test_chatgpt_codex_oauth_action_can_run_headless(monkeypatch):
    captured = {}

    def fake_login(**kwargs):
        captured.update(kwargs)
        return {"codex_access_token": "access-token"}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.perform_codex_oauth_login_with_mode", fake_login)

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "codex_oauth_authorize",
        Account(platform="chatgpt", email="user@example.com", password="Secret123!"),
        {"browser_mode": "headless"},
    )

    assert result["ok"] is True
    assert captured["headless"] is True


def test_chatgpt_codex_oauth_action_can_keep_headed_browser_open(monkeypatch):
    captured = {}

    def fake_login(**kwargs):
        captured.update(kwargs)
        return {"codex_access_token": "access-token"}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.perform_codex_oauth_login_with_mode", fake_login)

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "codex_oauth_authorize",
        Account(platform="chatgpt", email="user@example.com", password="Secret123!"),
        {"browser_mode": "headed", "keep_browser_open": "true"},
    )

    assert result["ok"] is True
    assert captured["headless"] is False
    assert captured["keep_browser_open"] is True


def test_chatgpt_codex_oauth_action_passes_browser_protocol_mode(monkeypatch):
    captured = {}

    def fake_login(**kwargs):
        captured.update(kwargs)
        return {"codex_access_token": "access-token"}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.perform_codex_oauth_login_with_mode", fake_login)

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "codex_oauth_authorize",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            extra={"session_token": "session-token", "cookies": "login_session=login"},
        ),
        {"oauth_mode": "browser_protocol", "browser_mode": "headless"},
    )

    assert result["ok"] is True
    assert captured["oauth_mode"] == "browser_protocol"
    assert captured["session_token"] == "session-token"
    assert captured["cookies"] == "login_session=login"


def test_chatgpt_codex_oauth_action_protocol_mode_allows_reusable_session_without_password(monkeypatch):
    captured = {}

    def fake_login(**kwargs):
        captured.update(kwargs)
        return {"codex_access_token": "access-token"}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.perform_codex_oauth_login_with_mode", fake_login)

    platform = ChatGPTPlatform(config=RegisterConfig())
    result = platform.execute_action(
        "codex_oauth_authorize",
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="",
            extra={"session_token": "session-token"},
        ),
        {"oauth_mode": "protocol"},
    )

    assert result["ok"] is True
    assert captured["oauth_mode"] == "protocol"
    assert captured["password"] == ""


def test_codex_oauth_mode_helpers_normalize_aliases():
    assert normalize_codex_oauth_mode("") == "browser"
    assert normalize_codex_oauth_mode("browser-protocol") == "browser_protocol"
    assert normalize_codex_oauth_mode("协议") == "protocol"
    assert has_codex_oauth_reusable_session(session_token="session-token")
    assert has_codex_oauth_reusable_session(cookies="login_session=abc; other=1")


def test_codex_oauth_cookie_parser_accepts_legacy_python_dict_text():
    assert _normalize_cookie_mapping("{'oai-did': 'device-1', 'login_session': 'old'}") == {
        "oai-did": "device-1",
        "login_session": "old",
    }


def test_codex_oauth_reads_workspace_from_real_client_auth_session_shape():
    assert _workspace_id_from_payload(
        {
            "page": {"type": "sign_in_with_chatgpt_codex_consent"},
            "oai-client-auth-session": {
                "workspaces": [{"id": "workspace-real", "kind": "personal"}],
            },
        }
    ) == "workspace-real"
    assert _normalize_protocol_phone_number("569 5785 7721") == "+56957857721"


def test_codex_oauth_protocol_state_machine_drives_password_otp_consent_callback():
    calls = []

    class Transport:
        kind = "protocol"

        def get(self, url):
            calls.append(("GET", url, None))
            return _CodexOAuthHTTPResult(200, "https://auth.openai.com/log-in", {}, "<html>", {})

        def sentinel_headers(self, flow):
            calls.append(("SENTINEL", flow, None))
            return {"openai-sentinel-token": flow}

        def post_json(self, url, payload, *, referer, headers=None):
            calls.append(("POST", url, payload))
            if url.endswith("/authorize/continue"):
                data = {"page": {"type": "login_password"}}
            elif url.endswith("/password/verify"):
                data = {"page": {"type": "email_otp_verification"}}
            elif url.endswith("/email-otp/validate"):
                data = {
                    "page": {
                        "type": "sign_in_with_chatgpt_codex_consent",
                        "payload": {
                            "current_workspace_id": "workspace-1",
                            "workspaces": [{"id": "workspace-1"}],
                        },
                    }
                }
            elif url.endswith("/workspace/select"):
                return _CodexOAuthHTTPResult(
                    302,
                    url,
                    {
                        "location": (
                            "http://localhost:1455/auth/callback?"
                            "code=code-1&state=state-1"
                        )
                    },
                    "",
                    {},
                )
            else:
                raise AssertionError(url)
            return _CodexOAuthHTTPResult(200, url, {}, "", data)

    callback = _drive_codex_oauth_protocol_state_machine(
        Transport(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-1",
        email="user@example.com",
        password="Password123!",
        registration_auth_mode="password",
        callback_server=_OAuthCallbackServer(port=0, state="state-1"),
        log=lambda _message: None,
        otp_callback=lambda: "123456",
        phone_callback=None,
    )

    assert callback["code"] == "code-1"
    assert [(method, url.rsplit("/", 1)[-1]) for method, url, _ in calls if method == "POST"] == [
        ("POST", "continue"),
        ("POST", "verify"),
        ("POST", "validate"),
        ("POST", "select"),
    ]
    assert ("SENTINEL", "authorize_continue", None) in calls
    assert ("SENTINEL", "password_verify", None) in calls


def test_codex_oauth_protocol_state_machine_drives_add_phone_and_phone_otp():
    calls = []

    class PhoneCallback:
        values = iter(["+15555550123", "654321"])

        def __init__(self):
            self.sent = False
            self.completed = False

        def __call__(self):
            return next(self.values)

        def mark_send_succeeded(self):
            self.sent = True

        def report_success(self):
            self.completed = True

    class Transport:
        kind = "browser_protocol"

        def get(self, url):
            return _CodexOAuthHTTPResult(200, "https://auth.openai.com/log-in", {}, "<html>", {})

        def sentinel_headers(self, flow):
            calls.append(("sentinel", flow))
            return {"openai-sentinel-token": flow}

        def post_json(self, url, payload, *, referer, headers=None):
            calls.append((url, payload))
            if url.endswith("/authorize/continue"):
                data = {"page": {"type": "email_otp_verification"}}
            elif url.endswith("/email-otp/validate"):
                data = {"page": {"type": "add_phone"}}
            elif url.endswith("/add-phone/send"):
                data = {"page": {"type": "phone_otp_verification"}}
            elif url.endswith("/phone-otp/validate"):
                data = {
                    "page": {
                        "type": "sign_in_with_chatgpt_codex_consent",
                        "payload": {"workspaces": [{"id": "workspace-1"}]},
                    }
                }
            elif url.endswith("/workspace/select"):
                return _CodexOAuthHTTPResult(
                    302,
                    url,
                    {"location": "http://localhost:1455/auth/callback?code=ok&state=s"},
                    "",
                    {},
                )
            else:
                raise AssertionError(url)
            return _CodexOAuthHTTPResult(200, url, {}, "", data)

    phone = PhoneCallback()
    callback = _drive_codex_oauth_protocol_state_machine(
        Transport(),
        auth_url="https://auth.openai.com/oauth/authorize?state=s",
        email="user@example.com",
        password="",
        registration_auth_mode="email_otp",
        callback_server=_OAuthCallbackServer(port=0, state="s"),
        log=lambda _message: None,
        otp_callback=lambda: "123456",
        phone_callback=phone,
    )

    assert callback["code"] == "ok"
    assert phone.sent is True
    assert phone.completed is True
    assert ("sentinel", "verify_phone_otp") in calls


def test_browser_fetch_oauth_transport_executes_get_and_post_inside_page(monkeypatch):
    calls = []
    page = object()

    def fake_fetch(actual_page, url, **kwargs):
        calls.append((actual_page, url, kwargs))
        return {"status": 200, "ok": True, "url": url, "headers": {}, "text": "{}", "data": {}}

    monkeypatch.setattr("platforms.chatgpt.browser_register._browser_fetch", fake_fetch)
    transport = _BrowserFetchCodexOAuthTransport(page, log=lambda _message: None, timeout=60)

    transport.get("https://auth.openai.com/oauth/authorize")
    transport.post_json(
        "https://auth.openai.com/api/accounts/authorize/continue",
        {"username": {"kind": "email", "value": "user@example.com"}},
        referer="https://auth.openai.com/log-in",
    )

    assert all(item[0] is page for item in calls)
    assert calls[0][2]["method"] == "GET"
    assert calls[1][2]["method"] == "POST"
    assert calls[1][2]["redirect"] == "follow"


def test_browser_fetch_transport_selects_callback_matching_current_state():
    handlers = []

    class Request:
        def __init__(self, url):
            self.url = url

    class Context:
        def on(self, event, handler):
            assert event == "request"
            handlers.append(handler)

        def remove_listener(self, event, handler):
            handlers.remove(handler)

    class Page:
        context = Context()
        url = "https://auth.openai.com/"

        def goto(self, url, **kwargs):
            for handler in list(handlers):
                handler(Request("http://localhost:1455/auth/callback?code=old&state=old-state"))
                handler(Request("http://localhost:1455/auth/callback?code=current&state=current-state"))

    transport = _BrowserFetchCodexOAuthTransport(Page(), log=lambda _message: None, timeout=60)
    callback = transport.complete_external(
        "https://auth.openai.com/api/oauth/oauth2/auth",
        _OAuthCallbackServer(port=0, state="current-state"),
    )

    assert callback["code"] == "current"
    assert callback["state"] == "current-state"


def test_codex_oauth_protocol_uses_chrome_tls_and_follows_callback_redirect(monkeypatch):
    captured = {}

    class CookieJar:
        def __init__(self):
            self.items = []

        def set(self, name, value, **kwargs):
            self.items.append((name, value, kwargs))

    class Response:
        status_code = 302
        url = "https://auth.openai.com/oauth/authorize"
        text = ""
        headers = {
            "Location": "http://localhost:1455/auth/callback?code=code-test&state=state-test",
        }

    class Session:
        def __init__(self, *, impersonate):
            captured["impersonate"] = impersonate
            self.cookies = CookieJar()
            self.headers = {}

        def get(self, url, **kwargs):
            captured["url"] = url
            captured["request"] = kwargs
            return Response()

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.curl_requests.Session", Session)
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth.retry_network_call",
        lambda callback, **kwargs: callback(),
    )

    callback = _request_codex_oauth_callback_via_protocol(
        "https://auth.openai.com/oauth/authorize?state=state-test",
        session_token="session-token",
        cookies="login_session=login",
        proxy="http://proxy.test:8080",
        log=lambda message: None,
        timeout=60,
    )

    assert callback == {
        "code": "code-test",
        "state": "state-test",
        "error": "",
        "error_description": "",
    }
    assert captured["impersonate"] == "chrome136"
    assert captured["request"]["allow_redirects"] is False
    assert captured["request"]["proxies"] == {
        "http": "http://proxy.test:8080",
        "https": "http://proxy.test:8080",
    }


def test_codex_oauth_browser_protocol_fetch_captures_callback_url(monkeypatch):
    logs = []

    class Page:
        url = "https://auth.openai.com/log-in"

    class Event:
        @staticmethod
        def is_set():
            return False

    class CallbackServer:
        event = Event()

    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._browser_fetch",
        lambda *args, **kwargs: {
            "ok": True,
            "status": 200,
            "url": "http://localhost:1455/auth/callback?code=code-test&state=state-test",
            "text": "",
        },
    )

    callback = _request_codex_oauth_callback_via_browser_fetch(
        Page(),
        "https://auth.openai.com/oauth/authorize?state=state-test",
        callback_server=CallbackServer(),
        log=logs.append,
        timeout=60,
    )

    assert callback == {
        "code": "code-test",
        "state": "state-test",
        "error": "",
        "error_description": "",
    }
    assert any("页面内 Fetch" in line for line in logs)


def test_codex_oauth_add_phone_uses_phone_callback(monkeypatch):
    calls = []

    class Event:
        def is_set(self):
            return any(item[0] == "phone" for item in calls)

    class CallbackServer:
        event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Page:
        url = "https://auth.openai.com/add-phone"

    def fake_goto(page, url, **kwargs):
        calls.append(("goto", url))

    states = iter(
        [
            {"page_type": "add_phone"},
            {"page_type": "oauth_callback"},
        ]
    )

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", fake_goto)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: next(states))
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._handle_add_phone_challenge",
        lambda page, phone_callback, **kwargs: calls.append(("phone", phone_callback())),
    )

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="Secret123!",
        callback_server=CallbackServer(),
        log=lambda message: None,
        otp_callback=None,
        phone_callback=lambda: "+15555550123",
        timeout=5,
    )

    assert result["code"] == "callback-code"
    assert calls == [
        ("goto", "https://auth.openai.com/oauth/authorize?state=state-test"),
        ("phone", "+15555550123"),
    ]


def test_codex_oauth_login_on_page_retries_with_new_authorize_url_after_browser_timeout(monkeypatch):
    logs = []
    attempts = []
    auth_modes = []

    class Page:
        pass

    class CallbackServer:
        def __init__(self, *, port=0, state="", log=None):
            self.state = state
            self.log = log

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_drive(page, **kwargs):
        attempts.append(kwargs["auth_url"])
        auth_modes.append(kwargs["registration_auth_mode"])
        if len(attempts) == 1:
            raise RuntimeError("Codex OAuth 浏览器登录超时")
        return {"code": "callback-code", "state": kwargs["callback_server"].state}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._OAuthCallbackServer", CallbackServer)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._drive_codex_oauth_page", fake_drive)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._exchange_code_for_tokens", lambda *args, **kwargs: {"access_token": "a", "refresh_token": "r", "id_token": "i"})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._save_codex_auth_file", lambda *args, **kwargs: "codex-auth.json")
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._token_identity", lambda token: {"email": "user@example.com", "account_id": "account", "plan_type": "unknown"})

    result = perform_codex_oauth_login_on_page(
        Page(),
        email="user@example.com",
        password="",
        registration_auth_mode="email_otp",
        log_fn=logs.append,
        timeout=1,
    )

    states = [parse_qs(urlparse(url).query)["state"][0] for url in attempts]
    assert len(attempts) == 2
    assert states[0] != states[1]
    assert result["codex_access_token"] == "a"
    assert auth_modes == ["email_otp", "email_otp"]
    assert "Codex OAuth 浏览器登录超时，重新生成授权链接重试" in logs


def test_codex_oauth_email_otp_passes_callback_for_incorrect_code_retry(monkeypatch):
    captured = {}

    class Event:
        def __init__(self):
            self.ready = False

        def is_set(self):
            return self.ready

        def wait(self, timeout):
            return self.ready

    class CallbackServer:
        def __init__(self):
            self.event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Page:
        url = "https://auth.openai.com/email-verification"

    def otp_callback():
        return "123456"

    def fake_submit_otp(page, code, log, **kwargs):
        captured["code"] = code
        captured["otp_callback"] = kwargs.get("otp_callback")
        page.url = "http://localhost:1455/auth/callback?code=callback-code&state=state-test"
        return {"ok": True, "status": 200, "url": page.url}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: {"page_type": "email_otp_verification"})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._submit_otp_via_page", fake_submit_otp)

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="Secret123!",
        callback_server=CallbackServer(),
        log=lambda message: None,
        otp_callback=otp_callback,
        phone_callback=None,
        timeout=5,
    )

    assert result["code"] == "callback-code"
    assert captured == {"code": "123456", "otp_callback": otp_callback}


def test_codex_oauth_email_otp_account_skips_generated_password(monkeypatch):
    calls = []
    stage = {"value": "password"}

    class Event:
        ready = False

        def is_set(self):
            return self.ready

    class CallbackServer:
        event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Target:
        def __init__(self, selector):
            self.selector = selector

        def is_visible(self, **kwargs):
            return stage["value"] == "password" and "password" in self.selector

    class Locator:
        def __init__(self, selector):
            self.first = Target(selector)

    class Page:
        url = "https://auth.openai.com/log-in"

        def locator(self, selector):
            return Locator(selector)

    def click_passwordless(page, selectors, **kwargs):
        calls.append(("passwordless", tuple(selectors)))
        stage["value"] = "otp"
        page.url = "https://auth.openai.com/email-verification"
        return selectors[0]

    def submit_otp(page, code, log, **kwargs):
        calls.append(("otp", code))
        CallbackServer.event.ready = True
        return {"ok": True, "status": 200, "url": page.url}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._derive_registration_state_from_page",
        lambda page: {"page_type": "login_password" if stage["value"] == "password" else "email_otp_verification"},
    )
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first_no_wait", click_passwordless)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._submit_otp_via_page", submit_otp)
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._submit_oauth_password_direct",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("password must not be submitted")),
    )

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="locally-generated-only",
        callback_server=CallbackServer(),
        log=lambda message: None,
        otp_callback=lambda: "123456",
        phone_callback=None,
        timeout=5,
        registration_auth_mode="email_otp",
    )

    assert result["code"] == "callback-code"
    assert calls[0][0] == "passwordless"
    assert calls[1] == ("otp", "123456")


def test_codex_oauth_incorrect_password_can_recover_with_email_otp(monkeypatch):
    calls = []
    stage = {"value": "password"}

    class Event:
        ready = False
        waits = 0

        def is_set(self):
            return self.ready

        def wait(self, timeout):
            self.waits += 1
            if self.waits >= 2 and stage["value"] == "passwordless_pending":
                stage["value"] = "otp"
                Page.url = "https://auth.openai.com/email-verification"
            return self.ready

    class CallbackServer:
        event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Target:
        def __init__(self, selector):
            self.selector = selector

        def is_visible(self, **kwargs):
            return stage["value"] == "password" and "password" in self.selector

    class Locator:
        def __init__(self, selector):
            self.first = Target(selector)

    class Page:
        url = "https://auth.openai.com/log-in"

        def locator(self, selector):
            return Locator(selector)

    def submit_password(page, password, log):
        calls.append(("password", password))
        return {"ok": False, "status": 400, "url": page.url, "text": "Incorrect email address or password"}

    def click_passwordless(page, selectors, **kwargs):
        calls.append(("passwordless", tuple(selectors)))
        stage["value"] = "passwordless_pending"
        return selectors[0]

    def submit_otp(page, code, log, **kwargs):
        calls.append(("otp", code))
        CallbackServer.event.ready = True
        return {"ok": True, "status": 200, "url": page.url}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._derive_registration_state_from_page",
        lambda page: {"page_type": "login_password" if stage["value"] == "password" else "email_otp_verification"},
    )
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._submit_oauth_password_direct", submit_password)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first_no_wait", click_passwordless)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._submit_otp_via_page", submit_otp)

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="unknown-password",
        callback_server=CallbackServer(),
        log=lambda message: None,
        otp_callback=lambda: "654321",
        phone_callback=None,
        timeout=5,
    )

    assert result["code"] == "callback-code"
    assert calls[0] == ("password", "unknown-password")
    assert calls[1][0] == "passwordless"
    assert calls[2] == ("otp", "654321")
    assert _is_incorrect_password_error("Incorrect email address or password") is True


def test_registration_auth_mode_restores_from_structured_overview_and_profile_amr():
    assert _resolve_registration_auth_mode(
        {"account_overview": {"registration_auth_mode": "email_otp"}}
    ) == "email_otp"
    assert _resolve_registration_auth_mode(
        {"account_overview": {"amr": ["otp", "urn:openai:amr:otp_email"]}}
    ) == "email_otp"
    assert _resolve_registration_auth_mode({"registration_auth_mode": "password"}) == "password"


def test_whatsapp_fallback_prompt_recognizes_observed_japanese_message():
    message = (
        "この電話番号にはテキストメッセージを送信できなかったため、WhatsApp に切り替えました。"
        "続行すると、WhatsApp で認証コードを送信します。"
    )

    assert _is_whatsapp_fallback_prompt(message) is True


def test_codex_oauth_account_chooser_selects_matching_email():
    logs = []
    clicks = []

    class Locator:
        def nth(self, index):
            clicks.append(("nth", index))
            return self

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            clicks.append(("click", kwargs.get("timeout"), kwargs.get("no_wait_after")))

    class Page:
        def evaluate(self, script, expected_email):
            assert expected_email == "user@example.com"
            assert 'button[name="session_id"]' in script
            assert 'a[href="/log-in-or-create-account"]' in script
            assert "requestSubmit" not in script
            assert "__submitPendingForm" not in script
            return {"action": "select", "index": 0, "email": "user@example.com"}

        def locator(self, selector):
            assert selector == 'button[name="session_id"]'
            return Locator()

    assert _handle_account_chooser(Page(), "User@Example.com", logs.append) is True
    assert clicks == [("nth", 0), ("click", 3000, True)]
    assert any("已选择匹配账号 user@example.com" in item for item in logs)


def test_codex_oauth_account_chooser_treats_click_timeout_as_pending_navigation():
    logs = []

    class Locator:
        def nth(self, index):
            return self

        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            raise TimeoutError("Locator.click: Timeout 3000ms exceeded\nCall log with session value")

    class Page:
        def evaluate(self, script, expected_email):
            return {"action": "select", "index": 0, "email": expected_email}

        def locator(self, selector):
            return Locator()

    assert _handle_account_chooser(Page(), "user@example.com", logs.append) is True
    assert any("已进入异步跳转观察期" in item for item in logs)
    assert not any("session value" in item for item in logs)


def test_codex_oauth_account_chooser_switches_when_email_mismatches():
    logs = []
    clicks = []

    class Locator:
        @property
        def first(self):
            clicks.append(("first", None))
            return self

        def click(self, **kwargs):
            clicks.append(("click", kwargs.get("timeout")))

    class Page:
        def evaluate(self, script, expected_email):
            assert expected_email == "target@example.com"
            assert 'button[name="session_id"]' in script
            assert 'a[href="/log-in-or-create-account"]' in script
            assert "window.location.assign" not in script
            return {"action": "switch", "accounts": ["other@example.com"]}

        def locator(self, selector):
            assert 'log-in-or-create-account' in selector
            return Locator()

    assert _handle_account_chooser(Page(), "target@example.com", logs.append) is True
    assert clicks == [("first", None), ("click", 5000)]
    assert any("未匹配预期邮箱 target@example.com" in item for item in logs)
    assert any("other@example.com" in item for item in logs)


def test_account_chooser_submission_pending_detects_busy_matching_button():
    class Page:
        def evaluate(self, script, expected_email):
            assert expected_email == "user@example.com"
            assert "aria-busy" in script
            return True

    assert _account_chooser_submission_pending(Page(), "User@Example.com") is True


def test_detect_codex_next_step_from_dom_identifies_add_phone():
    class Page:
        def evaluate(self, script):
            assert "input[type=\"tel\"]" in script
            return "add_phone"

    assert _detect_codex_next_step_from_dom(Page()) == "add_phone"


def test_codex_oauth_account_chooser_grace_period_observes_without_reclick(monkeypatch):
    calls = []
    logs = []
    fake_now = {"value": 1000.0}

    class Event:
        def is_set(self):
            return fake_now["value"] >= 1000.0 + ACCOUNT_CHOOSER_SUBMIT_GRACE_SECONDS

    class CallbackServer:
        event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Page:
        url = "https://auth.openai.com/choose-an-account"

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", lambda *args, **kwargs: calls.append(("goto", None)))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: {"page_type": "account_chooser"})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._detect_codex_next_step_from_dom", lambda page: "")
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._get_invalid_session_error_page", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._account_chooser_submission_pending", lambda page, email: True)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._handle_account_chooser", lambda page, email, log: calls.append(("select", email)) or True)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: fake_now.__setitem__("value", fake_now["value"] + seconds))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.time", lambda: fake_now["value"])

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="Secret123!",
        callback_server=CallbackServer(),
        log=logs.append,
        otp_callback=None,
        phone_callback=None,
        timeout=5,
    )

    assert result["code"] == "callback-code"
    assert calls == [("goto", None)]
    assert fake_now["value"] >= 1000.0 + ACCOUNT_CHOOSER_SUBMIT_GRACE_SECONDS
    assert sum("Codex OAuth 页面变化" in item for item in logs) == 1
    assert sum("等待页面异步跳转" in item for item in logs) == 1


def test_codex_oauth_email_submit_grace_prevents_repeated_clicks(monkeypatch):
    calls = []
    logs = []
    now = {"value": 1000.0}

    class Event:
        def is_set(self):
            return now["value"] >= 1008.0

        def wait(self, timeout):
            now["value"] += timeout
            return self.is_set()

    class CallbackServer:
        event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Target:
        def is_visible(self, **kwargs):
            return True

    class Locator:
        first = Target()

    class Page:
        url = "https://auth.openai.com/log-in"

        def locator(self, selector):
            return Locator()

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: {"page_type": ""})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._get_invalid_session_error_page", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._fill_input_like_user", lambda *args: calls.append("fill") or True)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first", lambda *args, **kwargs: calls.append("click") or 'button[type="submit"]')
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.time", lambda: now["value"])

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="Secret123!",
        callback_server=CallbackServer(),
        log=logs.append,
        otp_callback=None,
        phone_callback=None,
        timeout=30,
    )

    assert result["code"] == "callback-code"
    assert calls == ["fill", "click"]
    assert EMAIL_SUBMIT_GRACE_SECONDS >= 45
    assert sum("邮箱页: 已提交，等待页面异步跳转" in item for item in logs) == 1


def test_codex_oauth_email_submit_uses_input_enter_when_button_is_not_clickable(monkeypatch):
    calls = []
    logs = []
    now = {"value": 1000.0}

    class Event:
        def is_set(self):
            return now["value"] >= 1008.0

        def wait(self, timeout):
            now["value"] += timeout
            return self.is_set()

    class CallbackServer:
        event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Target:
        def is_visible(self, **kwargs):
            return True

    class Locator:
        first = Target()

    class Page:
        url = "https://auth.openai.com/log-in"

        def locator(self, selector):
            return Locator()

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: {"page_type": ""})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._get_invalid_session_error_page", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._fill_input_like_user", lambda *args: calls.append("fill") or True)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first", lambda *args, **kwargs: calls.append("click") or None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._press_enter_on_input", lambda *args: calls.append("enter") or True)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.time", lambda: now["value"])

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="Secret123!",
        callback_server=CallbackServer(),
        log=logs.append,
        otp_callback=None,
        phone_callback=None,
        timeout=30,
    )

    assert result["code"] == "callback-code"
    assert calls == ["fill", "click", "enter"]
    assert sum("邮箱页按钮不可点击" in item for item in logs) == 1
    assert sum("邮箱页: 已提交，等待页面异步跳转" in item for item in logs) == 1


def test_codex_oauth_recovers_stalled_consent_page_with_reload(monkeypatch):
    calls = []
    now = {"value": 1000.0}

    class Event:
        def __init__(self):
            self.ready = False

        def is_set(self):
            return self.ready

        def wait(self, timeout):
            now["value"] += timeout
            return self.ready

    class CallbackServer:
        def __init__(self):
            self.event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Page:
        url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        def reload(self, **kwargs):
            calls.append(("reload", kwargs.get("timeout")))
            self.url = "http://localhost:1455/auth/callback?code=callback-code&state=state-test"

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", lambda *args, **kwargs: calls.append(("goto", None)))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: {"page_type": "consent"})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._get_invalid_session_error_page", lambda page: {})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_continue_like_button", lambda *args, **kwargs: False)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._extract_auth_error_text", lambda page: "")
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.time", lambda: now["value"])

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="Secret123!",
        callback_server=CallbackServer(),
        log=lambda message: None,
        otp_callback=None,
        phone_callback=None,
        timeout=30,
    )

    assert result["code"] == "callback-code"
    assert calls == [("goto", None), ("reload", 30000)]


def test_codex_oauth_invalid_session_clicks_try_again_then_reselects_account(monkeypatch):
    calls = []

    class Event:
        def is_set(self):
            return False

    class CallbackServer:
        event = Event()

        def wait(self, timeout):
            return {"code": "callback-code", "state": "state-test"}

    class Page:
        url = "https://auth.openai.com/choose-an-account"

        def goto(self, url, **kwargs):
            calls.append(("goto-method", url))
            self.url = "http://localhost:1455/auth/callback?code=callback-code&state=state-test"

    def fake_goto(page, url, **kwargs):
        calls.append(("goto", url))
        if len(calls) >= 2:
            page.url = "http://localhost:1455/auth/callback?code=callback-code&state=state-test"

    invalid_states = iter([{"invalidSession": True}, {"invalidSession": False}])

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._goto_with_retry", fake_goto)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: {"page_type": "account_chooser"})
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._detect_codex_next_step_from_dom", lambda page: "")
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._get_invalid_session_error_page", lambda page: next(invalid_states))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_invalid_session_try_again", lambda page: calls.append(("try-again", None)) or True)
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._handle_account_chooser",
        lambda page, email, log: calls.append(("select-account", email)) or setattr(page, "url", "http://localhost:1455/auth/callback?code=callback-code&state=state-test") or True,
    )
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)

    result = _drive_codex_oauth_page(
        Page(),
        auth_url="https://auth.openai.com/oauth/authorize?state=state-test",
        email="user@example.com",
        password="Secret123!",
        callback_server=CallbackServer(),
        log=lambda message: None,
        otp_callback=None,
        phone_callback=None,
        timeout=5,
    )

    assert result["code"] == "callback-code"
    assert calls == [
        ("goto", "https://auth.openai.com/oauth/authorize?state=state-test"),
        ("try-again", None),
        ("select-account", "user@example.com"),
    ]


def test_get_invalid_session_error_page_detects_invalid_session():
    class Page:
        def evaluate(self, script):
            assert "invalidSession" in script
            assert "invalid\\s+session\\s+id" in script
            return {"invalidSession": True, "retryVisible": True, "text": "Oops Invalid session ID Try again"}

    result = _get_invalid_session_error_page(Page())

    assert result["invalidSession"] is True
    assert result["retryVisible"] is True


def test_phone_input_contains_accepts_formatted_local_number():
    class Page:
        def evaluate(self, script, selector):
            assert selector == 'input[type="tel"]'
            return "+56 9 7123 4527"

    ok, actual = _phone_input_contains(Page(), 'input[type="tel"]', "971234527")

    assert ok is True
    assert actual == "+56 9 7123 4527"


def test_submit_add_phone_number_clicks_structural_submit_in_phone_form(monkeypatch):
    events = []

    class Target:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            events.append(("click", kwargs.get("no_wait_after")))

    class Locator:
        def nth(self, index):
            events.append(("nth", index))
            return Target()

    class Page:
        def locator(self, selector):
            events.append(("locator", selector))
            return Locator()

    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._inspect_add_phone_submission",
        lambda *args: {"targetIndex": 2},
    )
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._capture_add_phone_structure", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._add_phone_submit_progress_state",
        lambda *args: {"url": "https://auth.openai.com/add-phone", "phoneVisible": True},
    )

    result = _submit_add_phone_number(
        Page(),
        'input[type="tel"]',
        log=lambda message: None,
    )

    assert result and "nth=2" in result
    assert ("nth", 2) in events
    assert ("click", True) in events


def test_submit_add_phone_number_does_not_repeat_after_timeout_with_progress(monkeypatch):
    events = []
    states = iter(
        [
            {"url": "https://auth.openai.com/add-phone", "phoneVisible": True, "busy": False},
            {"url": "https://auth.openai.com/add-phone", "phoneVisible": True, "busy": True},
        ]
    )

    class Target:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            events.append("click")
            raise RuntimeError("click timeout")

    class Locator:
        def nth(self, index):
            return Target()

    class Page:
        def locator(self, selector):
            events.append(("locator", selector))
            return Locator()

    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._inspect_add_phone_submission",
        lambda *args: {"targetIndex": 0},
    )
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._capture_add_phone_structure", lambda *args, **kwargs: "")
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._add_phone_submit_progress_state", lambda *args: next(states))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._request_submit_add_phone_form",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not submit twice")),
    )

    result = _submit_add_phone_number(
        Page(),
        'input[type="tel"]',
        log=lambda message: None,
    )

    assert result and "progress-after-click" in result
    assert events.count("click") == 1


def test_submit_add_phone_number_uses_enter_after_intercepted_click(monkeypatch):
    events = []
    base = {"url": "https://auth.openai.com/add-phone", "phoneVisible": True, "busy": False}
    states = iter([base, dict(base), dict(base), {**base, "busy": True}])

    class SubmitTarget:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            events.append("click")
            raise RuntimeError("another element intercepts pointer events")

    class PhoneTarget:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def focus(self, **kwargs):
            events.append("focus")

        def press(self, key, **kwargs):
            events.append(("press", key))

    class Locator:
        def __init__(self, target):
            self.target = target

        @property
        def first(self):
            return self.target

        def nth(self, index):
            return self.target

    class Page:
        def locator(self, selector):
            if selector == 'input[type="tel"]':
                return Locator(PhoneTarget())
            return Locator(SubmitTarget())

    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._inspect_add_phone_submission",
        lambda *args: {"targetIndex": 0},
    )
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._capture_add_phone_structure", lambda *args, **kwargs: "")
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._add_phone_submit_progress_state", lambda *args: next(states))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._request_submit_add_phone_form",
        lambda *args: (_ for _ in ()).throw(AssertionError("requestSubmit should not be needed")),
    )

    result = _submit_add_phone_number(
        Page(),
        'input[type="tel"]',
        log=lambda message: None,
    )

    assert result == 'input[type="tel"] press=Enter'
    assert events == ["click", "focus", ("press", "Enter")]


def test_submit_add_phone_number_uses_request_submit_as_last_fallback(monkeypatch):
    events = []
    base = {"url": "https://auth.openai.com/add-phone", "phoneVisible": True, "busy": False}

    class Target:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def click(self, **kwargs):
            events.append("click")
            raise RuntimeError("click timeout")

        def focus(self, **kwargs):
            events.append("focus")

        def press(self, key, **kwargs):
            events.append(("press", key))

    class Locator:
        @property
        def first(self):
            return Target()

        def nth(self, index):
            return Target()

    class Page:
        def locator(self, selector):
            return Locator()

    monkeypatch.setattr(
        "platforms.chatgpt.codex_oauth._inspect_add_phone_submission",
        lambda *args: {"targetIndex": 0},
    )
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._capture_add_phone_structure", lambda *args, **kwargs: "")
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._add_phone_submit_progress_state", lambda *args: dict(base))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)

    def request_submit(*args):
        events.append("requestSubmit")
        return {"ok": True, "method": "form.requestSubmit"}

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._request_submit_add_phone_form", request_submit)

    result = _submit_add_phone_number(
        Page(),
        'input[type="tel"]',
        log=lambda message: None,
    )

    assert result == "form.requestSubmit"
    assert events == ["click", "focus", ("press", "Enter"), "requestSubmit"]


def test_select_text_message_delivery_prefers_sms_over_whatsapp(monkeypatch):
    class Page:
        def evaluate(self, script, keyword):
            assert keyword == "sms"
            assert "checked" in script
            return {
                "selected": True,
                "candidates": [{"tag": "input", "text": "Text Message", "visible": True, "selected": True}],
            }

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first_no_wait", lambda *args, **kwargs: None)

    assert _select_text_message_delivery(Page(), lambda message: None) is True


def test_select_text_message_delivery_does_not_dom_click_non_actionable_text(monkeypatch):
    logs = []

    class Page:
        def evaluate(self, script, keyword):
            assert ".click()" not in script
            return {
                "selected": False,
                "candidates": [{"tag": "label", "text": "Text Message", "visible": True, "selected": False}],
            }

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first_no_wait", lambda *args, **kwargs: None)

    assert _select_text_message_delivery(Page(), logs.append) is False
    assert any("未找到可点击" in item for item in logs)


def test_select_text_message_delivery_uses_keyboard_for_visible_radio(monkeypatch):
    actions = []
    states = iter(
        [
            {
                "selected": False,
                "radioIndices": [1],
                "candidates": [{"tag": "input", "text": "Text Message", "visible": True}],
            },
            {"selected": True, "radioIndices": [1], "candidates": []},
        ]
    )

    class Target:
        def is_visible(self, **kwargs):
            return True

        def is_enabled(self, **kwargs):
            return True

        def focus(self, **kwargs):
            actions.append("focus")

        def press(self, key, **kwargs):
            actions.append(("press", key))

    class Locator:
        def nth(self, index):
            assert index == 1
            return Target()

    class Page:
        def evaluate(self, script, keyword):
            return next(states)

        def locator(self, selector):
            assert selector == 'input[type="radio"]'
            return Locator()

    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first_no_wait", lambda *args, **kwargs: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)

    logs = []
    assert _select_text_message_delivery(Page(), logs.append) is True
    assert actions == ["focus", ("press", "Space")]
    assert any("键盘 Space" in item for item in logs)


def test_add_phone_retries_send_via_whatsapp_on_fallback_prompt(monkeypatch):
    events = []
    error_messages = iter(
        [
            "We couldn't send a text message to this phone number, so we switched to WhatsApp. "
            "Continue to send a verification code on WhatsApp.",
            "",
        ]
    )

    class Page:
        url = "https://auth.openai.com/add-phone"

    class PhoneCallback:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 1:
                return "+56967181019"
            return "123456"

        def mark_send_succeeded(self):
            events.append("send_succeeded")

        def mark_send_failed(self, reason=""):
            events.append(("send_failed", reason))

    def fake_click_first_no_wait(page, selectors, timeout=0):
        if any("WhatsApp" in selector for selector in selectors):
            events.append("select_whatsapp")
            return 'button:has-text("WhatsApp")'
        events.append("send")
        return 'button[type="submit"]'

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._select_phone_country_ui", lambda *args: True)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._wait_for_any_selector", lambda *args, **kwargs: 'input[type="tel"]')
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._fill_input_like_user", lambda *args, **kwargs: True)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._select_text_message_delivery", lambda *args, **kwargs: True)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._click_first_no_wait", fake_click_first_no_wait)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._extract_auth_error_text", lambda page: next(error_messages))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._submit_otp_via_page", lambda *args, **kwargs: {"ok": True, "status": 200})

    _do_add_phone_attempt(Page(), PhoneCallback(), log=lambda message: None, resume_url="")

    assert events == ["send", "select_whatsapp", "send", "send_succeeded"]


def test_codex_phone_callback_treats_bad_sent_status_as_nonfatal():
    logs = []

    class Provider:
        def mark_sms_sent(self, activation_id):
            assert activation_id == "activation-1"
            raise RuntimeError("SMSBower setStatus失败: 状态码无效 (BAD_STATUS)")

    callback = _CodexSmsPhoneCallback(Provider(), log_fn=logs.append)
    callback.activation = SmsActivation(
        activation_id="activation-1",
        phone_number="+573171000000",
        provider="smsbower",
    )

    callback.mark_send_succeeded()

    assert callback.sent is True
    assert any("继续轮询验证码" in message for message in logs)


def test_add_phone_retries_new_number_when_chinese_in_use_error(monkeypatch):
    attempts = []

    class Page:
        url = "https://auth.openai.com/add-phone"

        def goto(self, url, **kwargs):
            attempts.append(("goto", url))

    class PhoneCallback:
        def __init__(self):
            self.reset_count = 0

        def __call__(self):
            return "+15555550123"

        def cleanup(self):
            attempts.append("cleanup")

        def reset(self):
            self.reset_count += 1
            attempts.append("reset")

    def fake_do_attempt(page, phone_callback, **kwargs):
        attempts.append("attempt")
        if len([item for item in attempts if item == "attempt"]) == 1:
            raise RuntimeError("手机号提交失败: 该电话号码已被使用。请使用其他电话号码。")

    callback = PhoneCallback()
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._do_add_phone_attempt", fake_do_attempt)

    _handle_add_phone_challenge(
        Page(),
        callback,
        log=lambda message: None,
        resume_url="https://auth.openai.com/oauth/authorize?state=state-test",
    )

    assert attempts == ["attempt", "cleanup", "reset", ("goto", "https://auth.openai.com/add-phone"), "attempt"]


def test_add_phone_uses_configured_phone_attempt_limit(monkeypatch):
    attempts = []
    logs = []

    class Page:
        url = "https://auth.openai.com/add-phone"

        def goto(self, url, **kwargs):
            attempts.append(("goto", url))

    class PhoneCallback:
        phone_max_attempts = 5

        def __call__(self):
            return "+15555550123"

        def cleanup(self):
            attempts.append("cleanup")

        def reset(self):
            attempts.append("reset")

    def fake_do_attempt(page, phone_callback, **kwargs):
        attempts.append("attempt")
        raise RuntimeError("手机号提交失败: 该电话号码已被使用。请使用其他电话号码。")

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._do_add_phone_attempt", fake_do_attempt)

    try:
        _handle_add_phone_challenge(
            Page(),
            PhoneCallback(),
            log=logs.append,
            resume_url="https://auth.openai.com/oauth/authorize?state=state-test",
        )
    except RuntimeError as exc:
        assert "手机验证失败" in str(exc)
    else:
        raise AssertionError("configured phone attempt limit should eventually be exhausted")

    assert attempts.count("attempt") == 5
    assert attempts.count("reset") == 4
    assert any("换号重试 5/5" in message for message in logs)




def test_add_phone_retries_new_number_when_japanese_in_use_error(monkeypatch):
    attempts = []

    class Page:
        url = "https://auth.openai.com/add-phone"

        def goto(self, url, **kwargs):
            attempts.append(("goto", url))

    class PhoneCallback:
        def __call__(self):
            return "+15555550123"

        def cleanup(self):
            attempts.append("cleanup")

        def reset(self):
            attempts.append("reset")

    def fake_do_attempt(page, phone_callback, **kwargs):
        attempts.append("attempt")
        if len([item for item in attempts if item == "attempt"]) == 1:
            raise RuntimeError("手机号提交失败: この電話番号はすでに使用されています。別の電話番号を使用してください。")

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._do_add_phone_attempt", fake_do_attempt)

    _handle_add_phone_challenge(
        Page(),
        PhoneCallback(),
        log=lambda message: None,
        resume_url="https://auth.openai.com/oauth/authorize?state=state-test",
    )

    assert attempts == ["attempt", "cleanup", "reset", ("goto", "https://auth.openai.com/add-phone"), "attempt"]


def test_add_phone_retries_new_number_when_japanese_invalid_number_error(monkeypatch):
    attempts = []

    class Page:
        url = "https://auth.openai.com/add-phone"

        def goto(self, url, **kwargs):
            attempts.append(("goto", url))

    class PhoneCallback:
        def __call__(self):
            return "+15555550123"

        def cleanup(self):
            attempts.append("cleanup")

        def reset(self):
            attempts.append("reset")

    def fake_do_attempt(page, phone_callback, **kwargs):
        attempts.append("attempt")
        if len([item for item in attempts if item == "attempt"]) == 1:
            raise RuntimeError("手机号提交失败: 電話番号が無効です。")

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._do_add_phone_attempt", fake_do_attempt)

    _handle_add_phone_challenge(
        Page(),
        PhoneCallback(),
        log=lambda message: None,
        resume_url="https://auth.openai.com/oauth/authorize?state=state-test",
    )

    assert attempts == ["attempt", "cleanup", "reset", ("goto", "https://auth.openai.com/add-phone"), "attempt"]


def test_add_phone_retries_when_japanese_account_link_limit_is_reached(monkeypatch):
    attempts = []
    logs = []

    class Page:
        url = "https://auth.openai.com/add-phone"

        def goto(self, url, **kwargs):
            attempts.append(("goto", url))

    class PhoneCallback:
        phone_max_attempts = 3

        def __call__(self):
            return "+15555550123"

        def cleanup(self):
            attempts.append("cleanup")

        def reset(self):
            attempts.append("reset")

    def fake_do_attempt(page, phone_callback, **kwargs):
        attempts.append("attempt")
        if attempts.count("attempt") == 1:
            raise RuntimeError(
                "手机号提交失败: この電話番号は、すでに上限数のアカウントに関連付けられています。"
            )

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._do_add_phone_attempt", fake_do_attempt)

    _handle_add_phone_challenge(
        Page(),
        PhoneCallback(),
        log=logs.append,
        resume_url="https://auth.openai.com/oauth/authorize?state=state-test",
    )

    assert attempts == [
        "attempt",
        "cleanup",
        "reset",
        ("goto", "https://auth.openai.com/add-phone"),
        "attempt",
    ]
    assert any("准备换号" in message for message in logs)
    assert any("换号重试 2/3" in message for message in logs)


def test_add_phone_retries_when_japanese_recently_used_error_follows_otp(monkeypatch):
    attempts = []
    logs = []

    class Page:
        url = "https://auth.openai.com/add-phone"

        def goto(self, url, **kwargs):
            attempts.append(("goto", url))

    class PhoneCallback:
        phone_max_attempts = 8

        def __call__(self):
            return "+15555550123"

        def cleanup(self):
            attempts.append("cleanup")

        def reset(self):
            attempts.append("reset")

    def fake_do_attempt(page, phone_callback, **kwargs):
        attempts.append("attempt")
        if attempts.count("attempt") == 1:
            raise RuntimeError(
                "短信验证码校验失败: この電話番号は最近使用されたため、"
                "しばらくしてからもう一度お試しください。"
            )

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._do_add_phone_attempt", fake_do_attempt)

    _handle_add_phone_challenge(
        Page(),
        PhoneCallback(),
        log=logs.append,
        resume_url="https://auth.openai.com/oauth/authorize?state=state-test",
    )

    assert attempts == [
        "attempt",
        "cleanup",
        "reset",
        ("goto", "https://auth.openai.com/add-phone"),
        "attempt",
    ]
    assert any("准备换号" in message for message in logs)
    assert any("换号重试 2/8" in message for message in logs)


def test_resume_oauth_after_add_phone_success_keeps_natural_consent(monkeypatch):
    gotos = []

    class Page:
        url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        def goto(self, url, **kwargs):
            gotos.append(url)

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.time", lambda: 1000.0)

    _resume_oauth_after_add_phone_success(
        Page(),
        resume_url="https://auth.openai.com/oauth/authorize?state=state-test",
        log=lambda message: None,
    )

    assert gotos == []


def test_resume_oauth_after_add_phone_success_falls_back_when_still_on_add_phone(monkeypatch):
    gotos = []
    now = {"value": 1000.0}

    class Page:
        url = "https://auth.openai.com/add-phone"

        def goto(self, url, **kwargs):
            gotos.append(url)

    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.time", lambda: now["value"])
    monkeypatch.setattr("platforms.chatgpt.codex_oauth.time.sleep", lambda seconds: now.__setitem__("value", now["value"] + seconds))
    monkeypatch.setattr("platforms.chatgpt.codex_oauth._derive_registration_state_from_page", lambda page: {"page_type": "add_phone"})

    _resume_oauth_after_add_phone_success(
        Page(),
        resume_url="https://auth.openai.com/oauth/authorize?state=state-test",
        log=lambda message: None,
        observe_seconds=1,
    )

    assert gotos == ["https://auth.openai.com/oauth/authorize?state=state-test"]


def test_codex_sms_phone_callback_retries_when_no_numbers(monkeypatch):
    sleeps = []

    class Provider:
        request_timeout = 1
        poll_interval = 0

        def __init__(self):
            self.calls = 0

        def get_number(self, service="", country=""):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("SMSBower getNumber失败: 当前服务/国家暂无号码 (NO_NUMBERS)")
            return SmsActivation(
                activation_id="activation-1",
                phone_number="+15555550123",
                provider="smsbower",
                service=service,
                country=country,
            )

    monkeypatch.setattr("platforms.chatgpt.plugin.time.sleep", lambda seconds: sleeps.append(seconds))
    logs = []
    provider = Provider()
    callback = _CodexSmsPhoneCallback(
        provider,
        service="go",
        country="0",
        log_fn=logs.append,
        buy_max_attempts=5,
        buy_retry_interval=2,
    )

    assert callback() == "+15555550123"
    assert provider.calls == 3
    assert sleeps == [2, 2]
    assert any("接码暂无号码" in item for item in logs)


def test_codex_sms_phone_callback_keeps_polling_after_proxy_error(monkeypatch):
    class Provider:
        poll_interval = 0

        def __init__(self):
            self.calls = 0

        def get_status(self, activation_id):
            assert activation_id == "activation-1"
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "SMSBower 请求失败: HTTPSConnectionPool: Max retries exceeded "
                    "(Caused by ProxyError('Unable to connect to proxy', "
                    "ConnectionResetError(10054, 'remote reset')))"
                )
            return SmsStatus(status="ok", code="654321", raw="STATUS_OK:654321")

    sleeps = []
    logs = []
    monkeypatch.setattr("platforms.chatgpt.plugin.time.sleep", lambda seconds: sleeps.append(seconds))
    provider = Provider()
    callback = _CodexSmsPhoneCallback(provider, log_fn=logs.append, otp_timeout_seconds=30)
    callback.activation = SmsActivation(
        activation_id="activation-1",
        phone_number="+15555550123",
        provider="smsbower",
    )

    assert callback() == "654321"
    assert provider.calls == 2
    assert sleeps == [1]
    assert any("继续重试" in item for item in logs)


def test_codex_sms_phone_callback_ignores_finish_network_failure():
    logs = []

    class Provider:
        def finish(self, activation_id):
            assert activation_id == "activation-1"
            raise RuntimeError("ProxyError: Unable to connect to proxy")

    callback = _CodexSmsPhoneCallback(Provider(), log_fn=logs.append)
    callback.activation = SmsActivation(
        activation_id="activation-1",
        phone_number="+15555550123",
        provider="smsbower",
    )

    callback.report_success()

    assert callback.completed is True
    assert any("回写失败" in item for item in logs)


def test_codex_sms_phone_callback_retries_transient_connection_reset(monkeypatch):
    class Provider:
        def __init__(self):
            self.calls = 0

        def get_number(self, service="", country=""):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError(
                    "SMSBower 请求失败: ('Connection aborted.', "
                    "ConnectionResetError(10054, 'remote reset'))"
                )
            return SmsActivation(
                activation_id="activation-3",
                phone_number="+15555550123",
                provider="smsbower",
                service=service,
                country=country,
            )

    sleeps = []
    monkeypatch.setattr("platforms.chatgpt.plugin.time.sleep", lambda seconds: sleeps.append(seconds))
    provider = Provider()
    callback = _CodexSmsPhoneCallback(
        provider,
        buy_max_attempts=5,
        buy_retry_interval=1,
    )

    assert callback() == "+15555550123"
    assert provider.calls == 3
    assert sleeps == [1, 1]


def test_codex_sms_phone_callback_sparsifies_large_no_number_retry_logs(monkeypatch):
    class Provider:
        request_timeout = 1
        poll_interval = 0

        def __init__(self):
            self.calls = 0

        def get_number(self, service="", country=""):
            self.calls += 1
            if self.calls <= 26:
                raise RuntimeError("SMSBower getNumber失败: 当前服务/国家暂无号码 (NO_NUMBERS)")
            return SmsActivation(
                activation_id="activation-27",
                phone_number="+15555550123",
                provider="smsbower",
            )

    monkeypatch.setattr("platforms.chatgpt.plugin.time.sleep", lambda seconds: None)
    logs = []
    provider = Provider()
    callback = _CodexSmsPhoneCallback(
        provider,
        log_fn=logs.append,
        buy_max_attempts=30,
        buy_retry_interval=0,
    )

    assert callback() == "+15555550123"
    retry_logs = [item for item in logs if "接码暂无号码" in item]
    assert len(retry_logs) == 4
    assert any("(25/30)" in item for item in retry_logs)


def test_codex_sms_phone_callback_cleanup_is_idempotent():
    cancels = []

    class Provider:
        def cancel(self, activation_id):
            cancels.append(activation_id)

    callback = _CodexSmsPhoneCallback(Provider())
    callback.activation = SmsActivation(
        activation_id="activation-1",
        phone_number="+15555550123",
        provider="smsbower",
    )

    callback.cleanup()
    callback.cleanup()

    assert cancels == ["activation-1"]
    assert callback.activation is None


def test_codex_sms_phone_callback_stops_when_cancelled(monkeypatch):
    class Provider:
        def get_number(self, service="", country=""):
            raise RuntimeError("SMSBower getNumber失败: 当前服务/国家暂无号码 (NO_NUMBERS)")

    monkeypatch.setattr("platforms.chatgpt.plugin.time.sleep", lambda seconds: None)
    checks = {"count": 0}

    def cancel_check():
        checks["count"] += 1
        return checks["count"] >= 2

    callback = _CodexSmsPhoneCallback(
        Provider(),
        buy_max_attempts=30,
        buy_retry_interval=1,
        cancel_check=cancel_check,
    )

    try:
        callback()
    except RuntimeError as exc:
        assert str(exc) == "任务已取消"
    else:
        raise AssertionError("cancelled phone callback should stop")


def test_codex_phone_callback_uses_smsbower_retry_settings(monkeypatch):
    captured = {}

    class Repo:
        def get_default_provider_key(self, provider_type):
            assert provider_type == "sms"
            return "smsbower"

        def resolve_runtime_settings(self, provider_type, provider_key, extra):
            assert provider_type == "sms"
            assert provider_key == "smsbower"
            return {
                "smsbower_api_key": "secret-key",
                "smsbower_default_service": "go",
                "smsbower_default_country": "0",
                "smsbower_buy_max_attempts": "7",
                "smsbower_buy_retry_interval": "1.5",
                "smsbower_otp_timeout_seconds": "180",
                "smsbower_phone_max_attempts": "6",
                "proxy": "http://browser-proxy.example:8080",
                "sms_proxy": "socks5://configured-sms-proxy.example:1080",
            }

    class Client:
        api_key = "secret-key"
        default_service = "go"
        default_country = "0"

    monkeypatch.setattr("infrastructure.provider_settings_repository.ProviderSettingsRepository", Repo)
    def fake_from_config(cls, config):
        captured.update(config)
        return Client()

    monkeypatch.setattr("core.smsbower_sms.SMSBowerClient.from_config", classmethod(fake_from_config))

    callback = ChatGPTPlatform(config=RegisterConfig())._build_codex_phone_callback(
        proxy="http://browser-proxy.example:8080"
    )

    assert isinstance(callback, _CodexSmsPhoneCallback)
    assert callback.buy_max_attempts == 7
    assert callback.buy_retry_interval == 1.5
    assert callback.otp_timeout_seconds == 180
    assert callback.phone_max_attempts == 6
    assert "proxy" not in captured
    assert "sms_proxy" not in captured


def test_codex_phone_callback_supports_non_smsbower_provider(monkeypatch):
    class Repo:
        def get_default_provider_key(self, provider_type):
            assert provider_type == "sms"
            return "herosms"

        def resolve_runtime_settings(self, provider_type, provider_key, extra):
            return {
                "herosms_api_key": "secret-key",
                "herosms_default_service": "dr",
                "herosms_default_country": "12",
                "herosms_buy_max_attempts": "9",
            }

    class Client:
        default_service = "dr"
        default_country = "12"
        buy_max_attempts = 9
        buy_retry_interval = 2
        otp_timeout_seconds = 150

        def configuration_error(self):
            return ""

    monkeypatch.setattr("infrastructure.provider_settings_repository.ProviderSettingsRepository", Repo)
    monkeypatch.setattr("core.herosms_sms.HeroSMSClient.from_config", classmethod(lambda cls, config: Client()))

    callback = ChatGPTPlatform(config=RegisterConfig())._build_codex_phone_callback(proxy=None)

    assert isinstance(callback, _CodexSmsPhoneCallback)
    assert callback.provider.__class__ is Client
    assert callback.service == "dr"
    assert callback.country == "12"
    assert callback.buy_max_attempts == 9


def test_codex_oauth_result_is_persistable_but_returned_safely():
    assert "codex_access_token" in PERSISTED_ACTION_DATA_KEYS
    assert "codex_refresh_token" in PERSISTED_ACTION_DATA_KEYS
    assert "codex_id_token" in PERSISTED_ACTION_DATA_KEYS
    assert CODEX_CREDENTIAL_TYPES["codex_access_token"] == "token"
    assert CODEX_CREDENTIAL_TYPES["codex_refresh_token"] == "token"
    assert CODEX_CREDENTIAL_TYPES["codex_id_token"] == "token"

    safe = _safe_action_result_data(
        "codex_oauth_authorize",
        {
            "codex_access_token": "access-token-very-secret",
            "codex_refresh_token": "refresh-token-very-secret",
            "codex_id_token": "id-token-very-secret",
            "codex_auth_path": "data/codex_auths/codex-test.json",
        },
    )

    assert "codex_access_token" not in safe
    assert "codex_refresh_token" not in safe
    assert "codex_id_token" not in safe
    assert safe["codex_access_token_preview"] == "access...cret"
    assert safe["codex_auth_path"] == "data/codex_auths/codex-test.json"


def test_chatgpt_registration_result_keeps_post_codex_oauth_data():
    mapped = ChatGPTPlatform(config=RegisterConfig())._map_chatgpt_result(
        {
            "email": "user@example.com",
            "password": "Secret123!",
            "access_token": "chatgpt-access",
            "codex_access_token": "codex-access",
            "codex_refresh_token": "codex-refresh",
            "codex_id_token": "codex-id",
            "codex_auth_path": "data/codex_auths/codex-test.json",
            "cookies": {"oai-did": "device-1"},
            "post_codex_oauth": {"ok": True},
        }
    )

    assert mapped.extra["access_token"] == "chatgpt-access"
    assert mapped.extra["codex_access_token"] == "codex-access"
    assert mapped.extra["codex_refresh_token"] == "codex-refresh"
    assert mapped.extra["codex_id_token"] == "codex-id"
    assert mapped.extra["codex_auth_path"] == "data/codex_auths/codex-test.json"
    assert mapped.extra["cookies"] == '{"oai-did":"device-1"}'
    assert mapped.extra["post_codex_oauth"] == {"ok": True}
