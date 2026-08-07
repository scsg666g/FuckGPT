from __future__ import annotations

from sqlmodel import Session

from application.tasks import _run_single_account_check
from core.account_check_settings import INVALID_CHECK_LIMIT, get_account_check_settings
from core.account_graph import patch_account_graph
from core.base_platform import Account
from core.db import AccountModel, AccountStatusModel, engine, save_account


def _account_id() -> int:
    model = save_account(Account(platform="chatgpt", email="validity-settings@test.com", password="secret"))
    return int(model.id or 0)


def test_account_check_settings_are_safe_by_default():
    settings = get_account_check_settings()
    assert settings.enabled is False
    assert settings.concurrency == 2
    assert settings.proxy_mode == "direct"
    assert settings.invalid_check_limit == 2


def test_invalid_attempts_stop_at_two_and_valid_result_resets(monkeypatch):
    account_id = _account_id()

    class FakePlatform:
        def __init__(self, config=None):
            self.config = config

        def check_valid(self, account):
            return False

        def get_last_check_overview(self):
            return {}

    monkeypatch.setattr("application.tasks.get", lambda _platform: FakePlatform)
    for expected in (1, INVALID_CHECK_LIMIT):
        _run_single_account_check(account_id, track_invalid_attempt=True)
        with Session(engine) as session:
            status = session.get(AccountStatusModel, account_id)
            assert status.invalid_check_count == expected

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(session, model, summary_updates={"valid": True})
        session.commit()
        status = session.get(AccountStatusModel, account_id)
        assert status.invalid_check_count == 0


def test_manual_invalid_check_does_not_consume_automatic_budget(monkeypatch):
    account_id = _account_id()

    class FakePlatform:
        def __init__(self, config=None):
            self.config = config

        def check_valid(self, account):
            return False

        def get_last_check_overview(self):
            return {}

    monkeypatch.setattr("application.tasks.get", lambda _platform: FakePlatform)
    _run_single_account_check(account_id, track_invalid_attempt=False)
    with Session(engine) as session:
        assert session.get(AccountStatusModel, account_id).invalid_check_count == 0


def test_network_error_does_not_mark_account_invalid(monkeypatch):
    account_id = _account_id()

    class FakePlatform:
        def __init__(self, config=None):
            self.config = config

        def check_valid(self, account):
            raise TimeoutError("network timeout")

    monkeypatch.setattr("application.tasks.get", lambda _platform: FakePlatform)
    try:
        _run_single_account_check(account_id, track_invalid_attempt=True)
    except TimeoutError:
        pass
    else:
        raise AssertionError("timeout should propagate as an error")
    with Session(engine) as session:
        status = session.get(AccountStatusModel, account_id)
        assert status.invalid_check_count == 0
        assert status.validity_status == "unknown"


def test_authentication_rejection_is_an_explicit_invalid_result(monkeypatch):
    from core.base_platform import RegisterConfig
    from platforms.chatgpt.plugin import ChatGPTPlatform
    from platforms.chatgpt import subscription

    class Response:
        status_code = 401

    error = RuntimeError("unauthorized")
    error.response = Response()
    monkeypatch.setattr(subscription, "fetch_subscription_status_details", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig(extra={"disable_proxy_pool": True, "raise_check_errors": True})
    plugin.mailbox = None
    account = type("AccountStub", (), {"token": "bad", "region": "", "user_id": "", "extra": {}})()

    assert plugin.check_valid(account) is False
    assert plugin.get_last_check_overview()["check_source"] == "authentication"
