from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.base_mailbox import MAILBOX_FACTORY_REGISTRY, MailboxAccount
from core.hotmail007_mailbox import Hotmail007Mailbox, parse_hotmail007_account
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.payloads:
            raise RuntimeError("no fake payloads left")
        return FakeResponse(self.payloads.pop(0))


class BlockingBuySession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def get(self, url, **kwargs):
        with self._lock:
            self.calls.append((url, kwargs))
            call_count = len(self.calls)
        if call_count == 2:
            self.started.set()
        self.release.wait(timeout=5)
        with self._lock:
            if not self.payloads:
                raise RuntimeError("no fake payloads left")
            payload = self.payloads.pop(0)
        return FakeResponse(payload)


def test_parse_hotmail007_account_accepts_documented_colon_format():
    entry = parse_hotmail007_account("user@outlook.com:mail-pass:refresh-token:client-id")

    assert entry.email == "user@outlook.com"
    assert entry.password == "mail-pass"
    assert entry.refresh_token == "refresh-token"
    assert entry.client_id == "client-id"
    assert entry.api_account == "user@outlook.com:mail-pass:refresh-token:client-id"


def test_hotmail007_buy_loops_until_success_without_sleep(monkeypatch):
    logs = []
    choices = iter(["12", "11", "12"])
    monkeypatch.setattr("core.hotmail007_mailbox.random.choice", lambda items: next(choices))
    session = FakeSession(
        [
            {"code": 1001, "success": False, "message": "库存不足", "data": {}},
            {"code": 1001, "success": False, "message": "库存不足", "data": {}},
            {
                "code": 0,
                "success": True,
                "message": "success",
                "data": {
                    "accounts": [
                        "user@outlook.com:mail-pass:refresh-token:client-id",
                    ]
                },
            },
        ]
    )
    mailbox = Hotmail007Mailbox(
        client_key="client-key",
        product_id="11,12",
        buy_max_attempts=5,
        session=session,
        log_fn=logs.append,
    )

    account = mailbox.get_email()

    assert account.email == "user@outlook.com"
    assert len(session.calls) == 3
    assert [call[0] for call in session.calls] == [
        "https://hotmail007.com/api/open/buy",
        "https://hotmail007.com/api/open/buy",
        "https://hotmail007.com/api/open/buy",
    ]
    assert [call[1]["params"]["productId"] for call in session.calls] == ["12", "11", "12"]
    assert session.calls[-1][1]["params"] == {
        "clientKey": "client-key",
        "productId": "12",
        "quantity": 1,
    }
    assert account.extra["provider_account"]["provider_name"] == "hotmail007"
    assert account.extra["provider_account"]["credentials"]["api_account"] == (
        "user@outlook.com:mail-pass:refresh-token:client-id"
    )
    assert logs[0].startswith("Hotmail007 开始循环购买邮箱")
    assert "request_timeout=8s" in logs[0]
    assert "soft_timeout" not in logs[0]
    assert any("第 1 次购买未成功" in item for item in logs)
    assert any("第 1 次购买未成功，productId=12" in item for item in logs)
    assert any("购买成功：第 3 次尝试，productId=12，获得邮箱 user@outlook.com" in item for item in logs)
    assert not any("client-key" in item or "refresh-token" in item or "mail-pass" in item for item in logs)


def test_hotmail007_max_attempt_error_reports_actual_attempts():
    logs = []
    session = FakeSession(
        [
            {"code": 23002, "success": False, "message": "Insufficient stock", "data": {}},
            {"code": 23002, "success": False, "message": "Insufficient stock", "data": {}},
        ]
    )
    mailbox = Hotmail007Mailbox(
        client_key="client-key",
        product_id="11",
        buy_max_attempts=2,
        session=session,
        log_fn=logs.append,
    )

    try:
        mailbox.get_email()
    except RuntimeError as exc:
        assert "attempts=2" in str(exc)
        assert "soft_timeout" not in str(exc)
    else:
        raise AssertionError("exhausted Hotmail007 buy loop should raise")

    assert len(session.calls) == 2
    assert any("已达到最大尝试次数 2 次" in item for item in logs)


def test_hotmail007_prefetch_buys_with_bounded_parallelism():
    logs = []
    session = BlockingBuySession(
        [
            {
                "code": 0,
                "success": True,
                "message": "success",
                "data": {"accounts": ["first@outlook.com:p:r:c"]},
            },
            {
                "code": 0,
                "success": True,
                "message": "success",
                "data": {"accounts": ["second@outlook.com:p:r:c"]},
            },
        ]
    )
    mailbox = Hotmail007Mailbox(
        client_key="client-key",
        product_id="11",
        buy_quantity=1,
        session=session,
        log_fn=logs.append,
    )

    mailbox.configure_prefetch(total_needed=2, buy_concurrency=2, queue_max=2)
    assert session.started.wait(timeout=2)
    assert len(session.calls) == 2
    session.release.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        emails = sorted(pool.map(lambda _index: mailbox.get_email().email, range(2)))
    mailbox.shutdown_prefetch()

    assert emails == ["first@outlook.com", "second@outlook.com"]
    assert all(call[1]["params"]["quantity"] == 1 for call in session.calls)
    assert any("预取已启用" in item for item in logs)
    assert any("预取入队" in item for item in logs)


def test_hotmail007_buy_loop_stops_when_cancelled():
    calls = {"count": 0}

    class CancelAfterFirstFailureSession:
        def get(self, url, **kwargs):
            calls["count"] += 1
            return FakeResponse({"code": 1001, "success": False, "message": "库存不足", "data": {}})

    mailbox = Hotmail007Mailbox(
        client_key="client-key",
        product_id="11",
        buy_max_attempts=200,
        session=CancelAfterFirstFailureSession(),
    )
    mailbox.set_cancel_checker(lambda: calls["count"] >= 1)

    try:
        mailbox.get_email()
    except RuntimeError as exc:
        assert str(exc) == "任务已取消"
    else:
        raise AssertionError("cancelled Hotmail007 buy loop should raise")

    assert calls["count"] == 1


def test_hotmail007_wait_for_code_reads_latest_mail_from_inbox():
    logs = []
    session = FakeSession(
        [
            {
                "code": 0,
                "success": True,
                "message": "success",
                "data": {
                    "from": "noreply@example.com",
                    "subject": "Your temporary ChatGPT login code",
                    "text": "Your verification code is 654321.",
                    "html": "",
                    "receivedAt": "2026-07-25T00:00:00Z",
                },
            }
        ]
    )
    mailbox = Hotmail007Mailbox(
        client_key="client-key",
        product_id="11",
        folders="inbox",
        session=session,
        log_fn=logs.append,
    )
    mail_account = MailboxAccount(
        email="user@outlook.com",
        account_id="user@outlook.com",
        extra={
            "provider_account": {
                "credentials": {
                    "api_account": "user@outlook.com:mail-pass:refresh-token:client-id",
                },
                "metadata": {"issued_at": 1784937600},
            }
        },
    )

    assert mailbox.wait_for_code(mail_account, timeout=1) == "654321"
    assert session.calls[0][0] == "https://hotmail007.com/api/open/mail/latest"
    assert session.calls[0][1]["params"] == {
        "clientKey": "client-key",
        "account": "user@outlook.com:mail-pass:refresh-token:client-id",
        "folder": "inbox",
        "start_timestamp": 1784937600,
    }
    assert any("开始等待验证码：user@outlook.com" in item for item in logs)
    assert any("正在取件：user@outlook.com / inbox" in item for item in logs)
    assert any("获取验证码成功：user@outlook.com" in item for item in logs)
    assert not any("654321" in item for item in logs)
    assert not any("client-key" in item or "refresh-token" in item or "mail-pass" in item for item in logs)


def test_hotmail007_wait_for_code_fails_fast_when_mail_authentication_is_invalid():
    logs = []
    session = FakeSession(
        [
            {"code": 27006, "success": False, "message": "Mail authentication failed", "data": {}},
            {"code": 27006, "success": False, "message": "Mail authentication failed", "data": {}},
        ]
    )
    mailbox = Hotmail007Mailbox(
        client_key="client-key",
        product_id="11",
        session=session,
        log_fn=logs.append,
    )
    mail_account = MailboxAccount(
        email="expired@outlook.com",
        account_id="expired@outlook.com",
        extra={
            "provider_account": {
                "credentials": {
                    "api_account": "expired@outlook.com:mail-pass:refresh-token:client-id",
                }
            }
        },
    )

    with pytest.raises(RuntimeError, match="邮箱认证失败"):
        mailbox.wait_for_code(mail_account, timeout=120)

    assert len(session.calls) == 2
    assert any("邮箱认证已失效，停止等待验证码" in item for item in logs)
    assert not any("client-key" in item or "refresh-token" in item or "mail-pass" in item for item in logs)


def test_hotmail007_is_exposed_in_mailbox_catalog_and_factory():
    repository = ProviderDefinitionsRepository()
    repository.ensure_seeded()

    definitions = repository.list_by_type("mailbox", enabled_only=True)
    drivers = repository.list_driver_templates("mailbox")

    assert "hotmail007" in {item.provider_key for item in definitions}
    assert "hotmail007" in {item["driver_type"] for item in drivers}
    assert "hotmail007" in MAILBOX_FACTORY_REGISTRY
