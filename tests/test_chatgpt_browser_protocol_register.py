from __future__ import annotations

import json
from types import SimpleNamespace

from api.task_commands import RegisterTaskRequest
from core.base_platform import RegisterConfig
from core.registration import RegistrationArtifacts, RegistrationContext
from platforms.chatgpt import browser_protocol_register as browser_protocol
from platforms.chatgpt.browser_register import ChatGPTBrowserRegister
from platforms.chatgpt.constants import CHATGPT_APP, OPENAI_API_ENDPOINTS
from platforms.chatgpt.plugin import ChatGPTPlatform


def test_browser_protocol_request_is_accepted_by_api_schema():
    request = RegisterTaskRequest(executor_type="browser_protocol")

    assert request.executor_type == "browser_protocol"


def test_unified_browser_request_is_accepted_by_api_schema():
    request = RegisterTaskRequest(executor_type="browser")

    assert request.executor_type == "browser"


def test_browser_protocol_adapter_builds_headless_fetch_worker():
    platform = ChatGPTPlatform(
        config=RegisterConfig(
            executor_type="browser_protocol",
            extra={"identity_provider": "mailbox"},
        )
    )
    context = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=platform,
        identity=SimpleNamespace(email="user@example.com", metadata={}),
        config=platform.config,
        email="user@example.com",
        password="StrongPass123!",
        log_fn=lambda _message: None,
    )
    artifacts = RegistrationArtifacts(otp_callback=lambda: "123456")

    adapter = platform.build_browser_registration_adapter()
    worker = adapter.browser_worker_builder(context, artifacts)

    assert worker.headless is True
    assert worker.flow_mode == "browser_protocol"


def test_browser_protocol_adapter_can_build_visible_fetch_worker():
    logs = []
    platform = ChatGPTPlatform(
        config=RegisterConfig(
            executor_type="browser_protocol",
            extra={
                "identity_provider": "mailbox",
                "browser_visible": True,
            },
        )
    )
    context = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=platform,
        identity=SimpleNamespace(email="user@example.com", metadata={}),
        config=platform.config,
        email="user@example.com",
        password="StrongPass123!",
        log_fn=logs.append,
    )
    artifacts = RegistrationArtifacts(otp_callback=lambda: "123456")

    adapter = platform.build_browser_registration_adapter()
    worker = adapter.browser_worker_builder(context, artifacts)

    assert worker.headless is False
    assert worker.flow_mode == "browser_protocol"
    assert "Browser Protocol 已启用可视浏览器窗口" in logs


def test_unified_browser_adapter_selects_window_visibility():
    def build_worker(visible: bool):
        logs = []
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                executor_type="browser",
                extra={
                    "identity_provider": "mailbox",
                    "browser_visible": visible,
                },
            )
        )
        context = RegistrationContext(
            platform_name="chatgpt",
            platform_display_name="ChatGPT",
            platform=platform,
            identity=SimpleNamespace(email="user@example.com", metadata={}),
            config=platform.config,
            email="user@example.com",
            password="StrongPass123!",
            log_fn=logs.append,
        )
        artifacts = RegistrationArtifacts(otp_callback=lambda: "123456")
        adapter = platform.build_browser_registration_adapter()
        return adapter.browser_worker_builder(context, artifacts), logs

    hidden_worker, hidden_logs = build_worker(False)
    visible_worker, visible_logs = build_worker(True)

    assert hidden_worker.headless is True
    assert visible_worker.headless is False
    assert hidden_worker.flow_mode == visible_worker.flow_mode == "dom"
    assert "浏览器模式已启用可视浏览器窗口" not in hidden_logs
    assert "浏览器模式已启用可视浏览器窗口" in visible_logs


def test_browser_protocol_flow_mode_survives_isolated_process_config(monkeypatch):
    captured = {}

    def fake_run(call, **kwargs):
        captured["call"] = call
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr("core.isolated_worker.run_isolated_call", fake_run)
    worker = ChatGPTBrowserRegister(
        headless=True,
        flow_mode="browser_protocol",
        otp_callback=lambda: "123456",
    )

    result = worker.run_isolated("user@example.com", "StrongPass123!")

    assert result == {"ok": True}
    child_config = captured["call"].args[0]
    assert child_config["init"]["flow_mode"] == "browser_protocol"


def test_browser_sentinel_headers_use_public_sdk_in_same_page():
    calls = []

    class Page:
        def wait_for_function(self, predicate, **kwargs):
            calls.append(("wait", predicate, kwargs["timeout"]))

        def evaluate(self, expression, flow):
            calls.append(("evaluate", flow))
            assert "window.SentinelSDK" in expression
            return {
                "token": {
                    "p": "proof",
                    "t": "turnstile",
                    "c": "challenge",
                    "id": "device",
                    "flow": flow,
                },
                "so": {"so": "observer", "c": "challenge"},
            }

    headers = browser_protocol._browser_sentinel_headers(
        Page(), "oauth_create_account", lambda _message: None
    )

    token = json.loads(headers["openai-sentinel-token"])
    observer = json.loads(headers["openai-sentinel-so-token"])
    assert token["flow"] == "oauth_create_account"
    assert token["t"] == "turnstile"
    assert observer["so"] == "observer"
    assert calls[0][0] == "wait"


def test_browser_protocol_flow_uses_fetch_for_business_api_and_navigation_for_redirects(monkeypatch):
    logs = []
    fetch_calls = []
    navigations = []

    class Page:
        url = f"{CHATGPT_APP}/"

    page = Page()

    def start_authorize(target, email, device_id, log):
        assert target is page
        assert email == "user@example.com"
        assert device_id
        page.url = "https://auth.openai.com/email-verification"
        return {"page_type": "email_otp_verification", "current_url": page.url}

    def navigate(target, url, log):
        assert target is page
        navigations.append(url)
        if "email-otp/send" in url:
            page.url = "https://auth.openai.com/email-verification"
        elif "about-you" in url:
            page.url = "https://auth.openai.com/about-you"
        elif "callback" in url:
            page.url = f"{CHATGPT_APP}/"
        else:
            page.url = url
        return page.url

    def fetch(target, url, *, method, payload, headers=None, timeout_ms=45_000):
        assert target is page
        fetch_calls.append((url, method, payload, dict(headers or {}), timeout_ms))
        if url == OPENAI_API_ENDPOINTS["register"]:
            return {
                "ok": True,
                "status": 200,
                "data": {"continue_url": "/api/accounts/email-otp/send"},
            }
        if url == OPENAI_API_ENDPOINTS["validate_otp"]:
            return {
                "ok": True,
                "status": 200,
                "data": {"continue_url": "/about-you"},
            }
        if url == OPENAI_API_ENDPOINTS["create_account"]:
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "continue_url": f"{CHATGPT_APP}/api/auth/callback/openai?code=ok"
                },
            }
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._seed_browser_device_id",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        start_authorize,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda current: {
            "page_type": "chatgpt_home" if "chatgpt.com" in current.url else "about_you",
            "current_url": current.url,
        },
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._handle_post_signup_onboarding",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(browser_protocol, "_navigate", navigate)
    monkeypatch.setattr(browser_protocol, "_auth_json_fetch", fetch)
    monkeypatch.setattr(
        browser_protocol,
        "_browser_sentinel_headers",
        lambda _page, flow, _log: {"openai-sentinel-token": f"token:{flow}"},
    )
    monkeypatch.setattr(
        browser_protocol,
        "_random_profile",
        lambda: ("Test User", "1990-01-02"),
    )

    result = browser_protocol.browser_protocol_registration_flow(
        page,
        "user@example.com",
        "StrongPass123!",
        lambda: "123456",
        logs.append,
    )

    assert result["page_type"] == "chatgpt_home"
    assert result["browser_protocol"] is True
    assert result["registration_auth_mode"] == "password"
    assert [call[0] for call in fetch_calls] == [
        OPENAI_API_ENDPOINTS["register"],
        OPENAI_API_ENDPOINTS["validate_otp"],
        OPENAI_API_ENDPOINTS["create_account"],
    ]
    assert fetch_calls[0][2] == {
        "username": "user@example.com",
        "password": "StrongPass123!",
    }
    assert fetch_calls[1][2] == {"code": "123456"}
    assert fetch_calls[2][2] == {
        "name": "Test User",
        "birthdate": "1990-01-02",
    }
    assert any("create-account/password" in url for url in navigations)
    assert any("email-otp/send" in url for url in navigations)
    assert any("callback" in url for url in navigations)
