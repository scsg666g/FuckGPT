from __future__ import annotations

from collections import deque
from unittest.mock import Mock

import pytest
import requests

from core.base_mailbox import MAILBOX_FACTORY_REGISTRY, MailboxAccount
from core.remote_mailboxes import (
    DuckMailMailbox,
    GPTMailMailbox,
    MaliAPIMailbox,
    MoeMailMailbox,
    TempMailLolMailbox,
)
from infrastructure.provider_definitions_repository import (
    ProviderDefinitionsRepository,
    SUPPORTED_MAILBOX_PROVIDER_KEYS,
)
from providers.registry import list_registered, load_all


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str = ""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.popleft()
        if callable(response):
            response = response(self, self.calls[-1])
        return response


def _credentials(account: MailboxAccount) -> dict:
    return account.extra["provider_account"]["credentials"]


def test_tempmail_lol_creates_persistable_mailbox_and_polls_new_code():
    session = FakeSession(
        [
            FakeResponse({"address": "demo@tempmail.lol", "token": "inbox-secret"}),
            FakeResponse(
                {
                    "emails": [
                        {"id": "old", "subject": "verification code 111111"},
                        {
                            "id": "new",
                            "subject": "Your verification code",
                            "body": "demo531498@example.com verification code 654321",
                        },
                    ]
                }
            ),
        ]
    )
    mailbox = TempMailLolMailbox(session=session, poll_interval=0)

    account = mailbox.get_email()
    code = mailbox.wait_for_code(account, timeout=1, before_ids={"old"})

    assert account.email == "demo@tempmail.lol"
    assert account.account_id == "demo@tempmail.lol"
    assert _credentials(account)["inbox_token"] == "inbox-secret"
    assert account.extra["provider_resource"]["provider_name"] == "tempmail_lol"
    assert code == "654321"
    assert session.calls[0]["url"] == "https://api.tempmail.lol/v2/inbox/create"
    assert session.calls[0]["json"] == {}
    assert session.calls[1]["params"] == {"token": "inbox-secret"}


def test_duckmail_direct_mode_creates_account_and_fetches_message_detail():
    session = FakeSession(
        [
            FakeResponse({"address": "duck@custom.example"}),
            FakeResponse({"token": "mail-token"}),
            FakeResponse({"hydra:member": [{"id": "m1", "subject": "OpenAI"}]}),
            FakeResponse({"id": "m1", "text": "verification code 240852"}),
        ]
    )
    mailbox = DuckMailMailbox(
        api_key="direct-api-key",
        domain="custom.example",
        session=session,
        poll_interval=0,
    )

    account = mailbox.get_email()
    code = mailbox.wait_for_code(account, timeout=1)

    assert account.email == "duck@custom.example"
    assert _credentials(account)["access_token"] == "mail-token"
    assert _credentials(account)["password"].startswith("Test")
    assert code == "240852"
    assert [call["url"] for call in session.calls] == [
        "https://api.duckmail.sbs/accounts",
        "https://api.duckmail.sbs/token",
        "https://api.duckmail.sbs/messages?page=1",
        "https://api.duckmail.sbs/messages/m1",
    ]
    assert session.calls[0]["headers"]["Authorization"] == "Bearer direct-api-key"
    assert session.calls[2]["headers"]["Authorization"] == "Bearer mail-token"


def test_duckmail_public_mode_uses_web_proxy_contract():
    session = FakeSession([FakeResponse({"hydra:member": []})])
    mailbox = DuckMailMailbox(session=session)
    account = MailboxAccount(
        email="duck@duckmail.sbs",
        account_id="resource-id",
        extra={
            "provider_account": {
                "credentials": {"access_token": "mail-token"},
            }
        },
    )

    assert mailbox.get_current_ids(account) == set()
    call = session.calls[0]
    assert call["url"] == "https://www.duckmail.sbs/api/mail?endpoint=%2Fmessages%3Fpage%3D1"
    assert call["headers"]["Authorization"] == "Bearer mail-token"
    assert call["headers"]["X-API-Provider-Base-URL"] == "https://api.duckmail.sbs"


def test_gptmail_uses_api_key_and_can_generate_local_domain_address(monkeypatch):
    local_session = FakeSession([])
    local_mailbox = GPTMailMailbox(
        api_key="gpt-secret", domain="known.example", session=local_session
    )
    monkeypatch.setattr("core.remote_mailboxes._random_local_part", lambda *_: "demo1234")

    local_account = local_mailbox.get_email()

    assert local_account.email == "demo1234@known.example"
    assert local_session.calls == []

    api_session = FakeSession([FakeResponse({"success": True, "data": {"email": "api@example.com"}})])
    api_mailbox = GPTMailMailbox(api_key="gpt-secret", session=api_session)
    api_account = api_mailbox.get_email()

    assert api_account.email == "api@example.com"
    assert api_session.calls[0]["headers"]["X-API-Key"] == "gpt-secret"
    assert api_session.calls[0]["url"] == "https://mail.chatgpt.org.uk/api/generate-email"


def test_gptmail_fetches_details_and_filters_before_ids():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "data": {
                        "emails": [
                            {"id": "old", "subject": "verification code 111111"},
                            {"id": "new", "subject": "OpenAI login"},
                        ]
                    }
                }
            ),
            FakeResponse({"data": {"id": "old", "content": "verification code 111111"}}),
            FakeResponse({"data": {"id": "new", "content": "verification code 222222"}}),
        ]
    )
    mailbox = GPTMailMailbox(api_key="gpt-secret", session=session, poll_interval=0)

    code = mailbox.wait_for_code(
        MailboxAccount(email="api@example.com", account_id="api@example.com"),
        timeout=1,
        before_ids={"old"},
    )

    assert code == "222222"
    assert session.calls[0]["params"] == {"email": "api@example.com"}


def test_maliapi_validates_key_and_passes_domain_strategy_and_temp_bearer():
    with pytest.raises(RuntimeError, match="API Key 未配置"):
        MaliAPIMailbox(session=FakeSession([])).get_email()

    session = FakeSession(
        [
            FakeResponse(
                {
                    "success": True,
                    "data": {
                        "address": "mali@example.com",
                        "tempToken": "temp-token",
                        "id": "inbox-1",
                    },
                }
            ),
            FakeResponse({"data": {"messages": [{"id": "m1", "subject": "OpenAI"}]}}),
            FakeResponse({"data": {"message": {"id": "m1", "html": "验证码：876543"}}}),
        ]
    )
    mailbox = MaliAPIMailbox(
        api_key="mali-secret",
        domain="example.com",
        auto_domain_strategy="prefer_owned",
        session=session,
        poll_interval=0,
    )

    account = mailbox.get_email()
    code = mailbox.wait_for_code(account, timeout=1)

    assert code == "876543"
    assert _credentials(account) == {
        "email": "mali@example.com",
        "temp_token": "temp-token",
        "inbox_id": "inbox-1",
    }
    assert session.calls[0]["json"] == {
        "domain": "example.com",
        "autoDomainStrategy": "prefer_owned",
    }
    assert session.calls[0]["headers"]["X-API-Key"] == "mali-secret"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer temp-token"


def test_moemail_registers_logs_in_and_persists_session(monkeypatch):
    def login_response(session: FakeSession, _call):
        session.cookies.set("__Secure-next-auth.session-token", "session-secret")
        return FakeResponse({})

    session = FakeSession(
        [
            FakeResponse({"ok": True}),
            FakeResponse({"csrfToken": "csrf-token"}),
            login_response,
            FakeResponse({"emailDomains": "sall.cc,mail.example"}),
            FakeResponse({"id": "email-1", "email": "demo@sall.cc"}),
            FakeResponse(
                {
                    "messages": [
                        {"id": "old", "subject": "verification code 111111"},
                        {"id": "new", "body": "OpenAI verification code 333333"},
                    ]
                }
            ),
        ]
    )
    mailbox = MoeMailMailbox(api_key="moe-secret", session=session, poll_interval=0)
    monkeypatch.setattr("random.choice", lambda values: values[0])

    account = mailbox.get_email()
    code = mailbox.wait_for_code(account, timeout=1, before_ids={"old"})

    credentials = _credentials(account)
    assert account.email == "demo@sall.cc"
    assert credentials["email_id"] == "email-1"
    assert credentials["session_token"] == "session-secret"
    assert credentials["cookies"]["__Secure-next-auth.session-token"] == "session-secret"
    assert code == "333333"
    assert [call["url"] for call in session.calls] == [
        "https://sall.cc/api/auth/register",
        "https://sall.cc/api/auth/csrf",
        "https://sall.cc/api/auth/callback/credentials",
        "https://sall.cc/api/config",
        "https://sall.cc/api/emails/generate",
        "https://sall.cc/api/emails/email-1",
    ]
    assert session.calls[0]["headers"]["X-API-Key"] == "moe-secret"


def test_remote_mailboxes_are_in_factory_plugin_registry_and_catalog():
    expected = {"tempmail_lol", "moemail", "duckmail", "gptmail", "maliapi"}
    assert expected.issubset(MAILBOX_FACTORY_REGISTRY)
    assert expected.issubset(SUPPORTED_MAILBOX_PROVIDER_KEYS)

    load_all()
    assert expected.issubset(list_registered("mailbox"))

    repository = ProviderDefinitionsRepository()
    repository.ensure_seeded()
    definitions = {item.provider_key: item for item in repository.list_by_type("mailbox")}
    assert expected.issubset(definitions)
    assert definitions["maliapi"].get_fields()[3]["options"][0] == {
        "value": "balanced",
        "label": "均衡选择（balanced）",
    }
    for provider_key in expected:
        assert definitions[provider_key].get_fields()


def test_mailbox_test_endpoint_uses_remote_factory_without_exposing_secret(client, monkeypatch):
    account = MailboxAccount(email="generated@example.com")
    class StubMailbox:
        def get_email(self):
            return account

    mailbox = StubMailbox()
    factory = Mock(return_value=mailbox)
    monkeypatch.setitem(MAILBOX_FACTORY_REGISTRY, "maliapi", factory)

    response = client.post(
        "/api/provider-settings/test",
        json={
            "provider_type": "mailbox",
            "provider_key": "maliapi",
            "config": {"maliapi_base_url": "https://mail.example/v1"},
            "auth": {"maliapi_api_key": "never-show-this-secret"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "message": "测试成功！生成邮箱: generated@example.com",
        "email": "generated@example.com",
    }
    assert "never-show-this-secret" not in response.text
    factory.assert_called_once_with(
        {
            "maliapi_base_url": "https://mail.example/v1",
            "maliapi_api_key": "never-show-this-secret",
        },
        None,
    )


def test_mailbox_test_endpoint_redacts_secret_from_failure(client, monkeypatch):
    secret = "never-show-this-secret"

    def fail_factory(_extra, _proxy):
        raise RuntimeError(f"upstream echoed key={secret}")

    monkeypatch.setitem(MAILBOX_FACTORY_REGISTRY, "maliapi", fail_factory)
    response = client.post(
        "/api/provider-settings/test",
        json={
            "provider_type": "mailbox",
            "provider_key": "maliapi",
            "config": {},
            "auth": {"maliapi_api_key": secret},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "测试失败: upstream echoed key=***"}
    assert secret not in response.text
