from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, select

from application.tasks import _run_single_account_check
from core.account_graph import patch_account_graph
from core.base_platform import RegisterConfig
from core.base_platform import Account
from core.db import (
    AccountModel,
    AccountSecurityProfileModel,
    AccountStatusModel,
    AccountSubscriptionModel,
    AccountUsageSnapshotModel,
    engine,
    save_account,
)
from core.lifecycle import check_accounts_validity, flag_expiring_trials
from core.proxy_pool import proxy_pool
from infrastructure.accounts_repository import AccountsRepository
from infrastructure.platform_runtime import PlatformRuntime
from domain.actions import ActionExecutionCommand
from domain.accounts import AccountImportLine, AccountQuery
from platforms.chatgpt import subscription
from platforms.chatgpt.plugin import ChatGPTPlatform


class _AlwaysValidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return True


class _AlwaysInvalidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return False


def _create_account(*, platform: str = "chatgpt", lifecycle_status: str = "registered") -> int:
    with Session(engine) as session:
        model = AccountModel(platform=platform, email=f"{platform}@example.com", password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates={"valid": lifecycle_status != "invalid"},
        )
        session.commit()
        return int(model.id or 0)


def _status(account_id: int):
    with Session(engine) as session:
        return session.get(AccountStatusModel, account_id)


def test_single_account_check_recovers_previously_invalid_account(monkeypatch):
    account_id = _create_account(lifecycle_status="invalid")
    monkeypatch.setattr("application.tasks.get", lambda _platform: _AlwaysValidPlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is True
    assert result["valid"] is True
    status = _status(account_id)
    assert status.lifecycle_status == "registered"
    assert status.validity_status == "valid"
    assert status.display_status == "registered"
    assert status.checked_at


def test_repository_read_backfills_missing_status_row():
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email="raw-row@test.com", password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        account_id = int(model.id)

    record = AccountsRepository().get(account_id)

    assert record is not None
    assert record.account_view["status"]["lifecycle"] == "registered"
    with Session(engine) as session:
        status = session.get(AccountStatusModel, account_id)
    assert status is not None
    assert status.lifecycle_status == "registered"


def test_reimport_without_status_preserves_existing_lifecycle():
    repository = AccountsRepository()
    repository.import_lines(
        "chatgpt",
        [
            AccountImportLine(
                email="reimport@test.com",
                password="first-secret",
                extra={"lifecycle_status": "subscribed", "plan": "plus"},
            )
        ],
    )
    repository.import_lines(
        "chatgpt",
        [AccountImportLine(email="reimport@test.com", password="updated-secret")],
    )

    _, records = repository.list(AccountQuery(platform="chatgpt", email="reimport@test.com"))
    record = records[0]
    assert record.password == "updated-secret"
    assert record.lifecycle_status == "subscribed"
    assert record.account_view["subscription"]["plan"] == "plus"


def test_lifecycle_validity_check_does_not_overwrite_lifecycle_status(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _AlwaysInvalidPlatform)

    results = check_accounts_validity(platform="chatgpt", limit=10)

    assert results["invalid"] == 1
    status = _status(account_id)
    assert status.lifecycle_status == "registered"
    assert status.validity_status == "invalid"
    assert status.display_status == "invalid"
    assert status.checked_at


def test_chatgpt_subscription_status_falls_back_to_wham_usage(monkeypatch):
    captured_headers: dict[str, str] = {}

    class _Resp:
        def __init__(self, data=None, error: Exception | None = None):
            self._data = data
            self._error = error

        def raise_for_status(self):
            if self._error:
                raise self._error

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(error=RuntimeError("403"))
        captured_headers.update(kwargs.get("headers") or {})
        return _Resp(data={"plan_type": "free"})

    monkeypatch.setattr(subscription.requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {
            "access_token": "token",
            "cookies": "",
            "id_token": json.dumps({"chatgpt_account_id": "acct-123"}),
            "extra": {},
        },
    )()

    status = subscription.check_subscription_status(account)

    assert status == "free"
    assert captured_headers["Authorization"] == "Bearer token"
    assert captured_headers["Chatgpt-Account-Id"] == "acct-123"


def test_chatgpt_subscription_status_prefers_wham_usage_plan(monkeypatch):
    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(data={"plan_type": "free", "orgs": {"data": []}})
        return _Resp(data={"plan_type": "plus"})

    monkeypatch.setattr(subscription.requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {"access_token": "token", "cookies": "", "id_token": "", "extra": {}},
    )()

    details = subscription.fetch_subscription_status_details(account)

    assert details["status"] == "plus"
    assert details["usage"]["plan_type"] == "plus"


def test_chatgpt_check_valid_uses_proxy_pool_before_direct(monkeypatch):
    calls: list[str | None] = []
    proxy_events: list[tuple[str, str]] = []

    def _fake_status(account, proxy=None):
        calls.append(proxy)
        if proxy != "http://127.0.0.1:7890":
            raise RuntimeError("should use proxy first")
        return {
            "status": "free",
            "source": "backend-api/wham/usage",
            "usage": {"plan_type": "free"},
        }

    monkeypatch.setattr(subscription, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "http://127.0.0.1:7890")
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {
                "access_token": "token",
                "id_token": "",
                "cookies": "",
            },
        },
    )()

    assert plugin.check_valid(account) is True
    assert calls == ["http://127.0.0.1:7890"]
    assert proxy_events == [("success", "http://127.0.0.1:7890")]
    assert plugin.get_last_check_overview()["chatgpt_usage"] == {"plan_type": "free"}


def test_chatgpt_check_valid_strict_proxy_does_not_fall_back_to_direct(monkeypatch):
    calls: list[str | None] = []

    def _fake_status(account, proxy=None):
        calls.append(proxy)
        raise RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")

    monkeypatch.setattr(subscription, "fetch_subscription_status_details", _fake_status)
    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig(
        proxy="http://127.0.0.1:7890",
        extra={"strict_proxy": True},
    )
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {"access_token": "token", "id_token": "", "cookies": ""},
        },
    )()

    with pytest.raises(RuntimeError, match="ERR_PROXY_CONNECTION_FAILED"):
        plugin.check_valid(account)
    assert calls == ["http://127.0.0.1:7890"]


def test_chatgpt_check_valid_uses_usage_plan_and_masks_phone(monkeypatch):
    def _fake_status(account, proxy=None):
        return {
            "status": "plus",
            "source": "backend-api/me",
            "me": {"email": "phone@test.com", "phone_number": "+56996830313"},
            "usage": {"plan_type": "plus"},
        }

    monkeypatch.setattr(subscription, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {"access_token": "token", "id_token": "", "cookies": ""},
        },
    )()

    assert plugin.check_valid(account) is True
    overview = plugin.get_last_check_overview()
    assert overview["plan"] == "plus"
    assert overview["plan_state"] == "subscribed"
    assert overview["phone_bound"] is True
    assert overview["phone_number_masked"] == "+569****0313"
    assert "已绑手机" in overview["chips"]


@pytest.mark.parametrize(
    ("plan_type", "expected_plan_state", "expected_display"),
    [
        ("plus", "subscribed", "subscribed"),
        ("free", "free", "registered"),
    ],
)
def test_account_check_persists_structured_chatgpt_state(
    monkeypatch,
    plan_type,
    expected_plan_state,
    expected_display,
):
    full_phone = "+56996830313"
    checked_usage = {
        "plan_type": plan_type,
        "rate_limit": {
            "limit_reached": False,
            "primary_window": {"used_percent": 25, "reset_at": 1777166030},
        },
        "credits": {"balance": 7},
    }

    monkeypatch.setattr("application.tasks.get", lambda _platform: ChatGPTPlatform)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)
    monkeypatch.setattr(
        subscription,
        "fetch_subscription_status_details",
        lambda account, proxy=None: {
            "status": plan_type,
            "source": "backend-api/wham/usage",
            "me": {
                "email": "structured@test.com",
                "phone_number": full_phone,
                "mfa_enabled": True,
                "amr": ["pwd", "mfa"],
            },
            "usage": checked_usage,
        },
    )
    account_id = _create_account()

    valid, _ = _run_single_account_check(account_id)

    assert valid is True
    with Session(engine) as session:
        status = session.get(AccountStatusModel, account_id)
        subscription_row = session.get(AccountSubscriptionModel, account_id)
        security = session.get(AccountSecurityProfileModel, account_id)
        usage = session.exec(
            select(AccountUsageSnapshotModel)
            .where(AccountUsageSnapshotModel.account_id == account_id)
            .order_by(AccountUsageSnapshotModel.id.desc())
        ).first()

    assert status.validity_status == "valid"
    assert status.display_status == expected_display
    assert subscription_row.plan_type == plan_type
    assert subscription_row.plan_state == expected_plan_state
    assert subscription_row.source == "backend-api/wham/usage"
    assert security.phone_bound is True
    assert security.phone_number_masked == "+569****0313"
    assert security.mfa_enabled is True
    assert full_phone not in security.raw_json
    assert usage.plan_type == plan_type
    assert usage.used_percent == 25
    assert usage.reset_at == 1777166030
    assert usage.get_credits() == {"balance": 7}

    record = AccountsRepository().get(account_id)
    view = record.account_view
    assert view["subscription"]["plan"] == plan_type
    assert view["subscription"]["state"] == expected_plan_state
    assert view["security"]["phone_number_masked"] == "+569****0313"
    assert view["usage"]["plan_type"] == plan_type
    assert full_phone not in json.dumps(view, ensure_ascii=False)


def test_chatgpt_query_state_uses_account_id_for_subscription(monkeypatch):
    captured: dict[str, str] = {}

    def _fake_profile(access_token, proxy=None):
        raise AssertionError("query_state should use the shared subscription detector first")

    def _fake_status(account, proxy=None):
        captured["account_id"] = getattr(account, "chatgpt_account_id", "")
        return {
            "status": "plus",
            "source": "backend-api/wham/usage",
            "usage": {"plan_type": "plus"},
        }

    monkeypatch.setattr("platforms.chatgpt.switch._fetch_profile", _fake_profile)
    monkeypatch.setattr("platforms.chatgpt.switch.read_current_codex_account", lambda: {})
    monkeypatch.setattr("platforms.chatgpt.switch.get_codex_desktop_state", lambda: {"available": True})
    monkeypatch.setattr(subscription, "fetch_subscription_status_details", _fake_status)

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "user_id": "fallback-account-id",
            "extra": {
                "access_token": "token",
                "account_id": "acct-real",
                "id_token": "id-token",
                "session_token": "session-token",
                "cookies": "",
            },
        },
    )()

    result = plugin._handle_query_state(account, {})

    assert result["ok"] is True
    assert captured["account_id"] == "acct-real"
    assert result["data"]["valid"] is True
    assert result["data"]["subscription_status"] == "plus"
    assert result["data"]["chatgpt_usage"]["plan_type"] == "plus"


def test_query_state_action_persists_the_same_structured_detection_result(monkeypatch):
    full_phone = "+56996830313"
    nested_access_token = "nested-query-access-token"
    nested_refresh_token = "nested-query-refresh-token"
    account = save_account(
        Account(
            platform="chatgpt",
            email="query-state@test.com",
            password="secret",
            extra={"access_token": "access-token"},
        )
    )
    with Session(engine) as session:
        model = session.get(AccountModel, int(account.id))
        patch_account_graph(
            session,
            model,
            lifecycle_status="invalid",
            summary_updates={"valid": False},
        )
        session.commit()

    class _QueryStatePlatform:
        def __init__(self, config=None):
            self.config = config

        def execute_action(self, action_id, account, params):
            assert action_id == "query_state"
            return {
                "ok": True,
                "data": {
                    "valid": True,
                    "profile": {
                        "email": "remote-query@test.com",
                        "phone_number": full_phone,
                        "mfa_enabled": True,
                        "amr": ["pwd", "mfa"],
                        "access_token": nested_access_token,
                    },
                    "diagnostics": {
                        "refreshToken": nested_refresh_token,
                        "input_token_count": 17,
                    },
                    # A stale profile signal must lose to explicit wham usage.
                    "subscription_status": "plus",
                    "subscription_source": "backend-api/me",
                    "chatgpt_usage": {
                        "plan_type": "free",
                        "rate_limit": {
                            "limit_reached": False,
                            "primary_window": {"used_percent": 10, "reset_at": 1777166030},
                        },
                    },
                },
            }

    monkeypatch.setattr("infrastructure.platform_runtime.load_all", lambda: None)
    monkeypatch.setattr("infrastructure.platform_runtime.get", lambda _platform: _QueryStatePlatform)

    result = PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=int(account.id),
            action_id="query_state",
            params={},
        )
    )

    assert result.ok is True
    assert result.data["profile"]["phone_number"] == "+569****0313"
    assert full_phone not in json.dumps(result.data, ensure_ascii=False)
    assert nested_access_token not in json.dumps(result.data, ensure_ascii=False)
    assert nested_refresh_token not in json.dumps(result.data, ensure_ascii=False)
    assert result.data["diagnostics"]["input_token_count"] == 17
    with Session(engine) as session:
        status = session.get(AccountStatusModel, int(account.id))
        subscription_row = session.get(AccountSubscriptionModel, int(account.id))
        security = session.get(AccountSecurityProfileModel, int(account.id))
        usage = session.exec(
            select(AccountUsageSnapshotModel)
            .where(AccountUsageSnapshotModel.account_id == int(account.id))
        ).one()
    assert status.validity_status == "valid"
    assert status.lifecycle_status == "registered"
    assert status.display_status == "registered"
    assert status.remote_email == "remote-query@test.com"
    assert subscription_row.plan_type == "free"
    assert subscription_row.plan_state == "free"
    assert subscription_row.source == "backend-api/wham/usage"
    assert security.phone_number_masked == "+569****0313"
    assert full_phone not in security.raw_json
    assert usage.plan_type == "free"
    assert usage.used_percent == 10
    assert AccountsRepository().get(int(account.id)).account_view["subscription"]["plan"] == "free"


def test_cached_wham_free_plan_is_not_overwritten_by_me_plus():
    account_id = _create_account()

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "check_source": "backend-api/wham/usage",
                "chatgpt_usage": {"plan_type": "free", "used_percent": 12},
            },
        )
        session.commit()

    # A later /me response may still claim Plus while wham is unavailable.
    # The last explicit wham plan remains authoritative in that case.
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "plan": "plus",
                "plan_state": "subscribed",
                "check_source": "backend-api/me",
            },
        )
        session.commit()

        subscription_row = session.get(AccountSubscriptionModel, account_id)
        usage_rows = session.exec(
            select(AccountUsageSnapshotModel).where(AccountUsageSnapshotModel.account_id == account_id)
        ).all()

    assert subscription_row.plan_type == "free"
    assert subscription_row.plan_state == "free"
    assert subscription_row.source == "backend-api/wham/usage"
    assert len(usage_rows) == 1
    assert usage_rows[0].plan_type == "free"


def test_query_state_indeterminate_error_preserves_last_validity(monkeypatch):
    account = save_account(
        Account(
            platform="chatgpt",
            email="query-indeterminate@test.com",
            password="secret",
            extra={"access_token": "access-token"},
        )
    )
    with Session(engine) as session:
        model = session.get(AccountModel, int(account.id))
        patch_account_graph(session, model, summary_updates={"valid": True})
        session.commit()

    class _IndeterminatePlatform:
        def __init__(self, config=None):
            self.config = config

        def execute_action(self, action_id, account, params):
            return {
                "ok": True,
                "data": {"valid": None, "last_error": "upstream timeout"},
            }

    monkeypatch.setattr("infrastructure.platform_runtime.load_all", lambda: None)
    monkeypatch.setattr("infrastructure.platform_runtime.get", lambda _platform: _IndeterminatePlatform)

    result = PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=int(account.id),
            action_id="query_state",
            params={},
        )
    )

    assert result.ok is True
    with Session(engine) as session:
        status = session.get(AccountStatusModel, int(account.id))
    assert status.validity_status == "valid"
    assert status.last_error == "upstream timeout"
    assert status.checked_at is not None


def test_chatgpt_usage_without_source_defaults_to_wham_and_remains_authoritative():
    account_id = _create_account()

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "chatgpt_usage": {"plan_type": "free", "used_percent": 12},
            },
        )
        session.commit()

        subscription_row = session.get(AccountSubscriptionModel, account_id)
        assert subscription_row.plan_type == "free"
        assert subscription_row.source == "backend-api/wham/usage"

        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "plan": "plus",
                "plan_state": "subscribed",
                "check_source": "backend-api/me",
            },
        )
        session.commit()

        subscription_row = session.get(AccountSubscriptionModel, account_id)

    assert subscription_row.plan_type == "free"
    assert subscription_row.plan_state == "free"
    assert subscription_row.source == "backend-api/wham/usage"


def test_usage_without_plan_inherits_reconciled_subscription_plan():
    account_id = _create_account()

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "chatgpt_usage": {"plan_type": "free", "used_percent": 12},
            },
        )
        session.commit()

        # Wham may still return a usable quota window while omitting its plan.
        # A weaker /me signal must not make subscription and usage disagree.
        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "plan": "plus",
                "check_source": "backend-api/me",
                "chatgpt_usage": {
                    "rate_limit": {"primary_window": {"used_percent": 25}},
                },
            },
        )
        session.commit()

        subscription_row = session.get(AccountSubscriptionModel, account_id)
        latest_usage = session.exec(
            select(AccountUsageSnapshotModel)
            .where(AccountUsageSnapshotModel.account_id == account_id)
            .order_by(AccountUsageSnapshotModel.id.desc())
        ).first()

    assert subscription_row.plan_type == "free"
    assert subscription_row.plan_state == "free"
    assert latest_usage.plan_type == "free"
    assert latest_usage.used_percent == 25


def test_authoritative_free_usage_recovers_subscribed_lifecycle():
    account_id = _create_account(lifecycle_status="subscribed")

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "chatgpt_usage": {"plan_type": "plus"},
                "check_source": "backend-api/wham/usage",
            },
        )
        session.commit()

        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "chatgpt_usage": {"plan_type": "free"},
                "check_source": "backend-api/wham/usage",
            },
        )
        session.commit()

        status = session.get(AccountStatusModel, account_id)
        subscription_row = session.get(AccountSubscriptionModel, account_id)

    assert subscription_row.plan_type == "free"
    assert subscription_row.plan_state == "free"
    assert status.lifecycle_status == "registered"
    assert status.display_status == "registered"


def test_usage_snapshot_sanitizes_nested_secrets_and_phone_lists():
    account_id = _create_account()
    full_phone = "+56996830313"
    access_token = "usage-access-token-secret"
    refresh_token = "usage-refresh-token-secret"

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "chatgpt_usage": {
                    "plan_type": "free",
                    "access_token": access_token,
                    "nested": {"refresh_token": refresh_token},
                    "phone_numbers": [full_phone],
                    "credits": {
                        "balance": 7,
                        "access_token": access_token,
                        "phone_numbers": [full_phone],
                    },
                },
            },
        )
        session.commit()

        usage = session.exec(
            select(AccountUsageSnapshotModel)
            .where(AccountUsageSnapshotModel.account_id == account_id)
            .order_by(AccountUsageSnapshotModel.id.desc())
        ).first()

    raw_json = json.dumps(usage.get_raw(), ensure_ascii=False)
    credits_json = json.dumps(usage.get_credits(), ensure_ascii=False)
    view_json = json.dumps(AccountsRepository().get(account_id).account_view, ensure_ascii=False)
    for serialized in (raw_json, credits_json, view_json):
        assert access_token not in serialized
        assert refresh_token not in serialized
        assert full_phone not in serialized
    assert "+569****0313" in raw_json
    assert "+569****0313" in credits_json


def test_usage_only_refresh_preserves_observed_security_profile():
    account_id = _create_account()

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "phone_bound": True,
                "phone_number": "+56996830313",
                "mfa_enabled": True,
                "amr": ["pwd", "mfa"],
            },
        )
        session.commit()

        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "chatgpt_usage": {"plan_type": "free", "used_percent": 1},
                "check_source": "backend-api/wham/usage",
            },
        )
        session.commit()

        security = session.get(AccountSecurityProfileModel, account_id)

    assert security.phone_bound is True
    assert security.phone_number_masked == "+569****0313"
    assert security.mfa_enabled is True
    assert security.get_amr() == ["pwd", "mfa"]


def test_security_partial_updates_preserve_auth_mode_and_treat_empty_amr_as_observed():
    account_id = _create_account()

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "registration_auth_mode": "email_otp",
                "mfa_enabled": True,
                "amr": ["pwd", "mfa"],
                "profile": {"email": "initial@example.com", "name": "Initial"},
            },
        )
        session.commit()

        # Omitting both fields is a partial profile update and must preserve
        # the previously observed MFA state and registration auth metadata.
        patch_account_graph(
            session,
            model,
            summary_updates={"profile": {"email": "updated@example.com"}},
        )
        session.commit()
        security = session.get(AccountSecurityProfileModel, account_id)
        assert security.mfa_enabled is True
        assert security.get_amr() == ["pwd", "mfa"]
        assert security.get_raw()["registration_auth_mode"] == "email_otp"
        assert security.get_raw()["profile"] == {
            "email": "updated@example.com",
            "name": "Initial",
        }

        # An explicitly observed empty AMR is different from omission.  With
        # no explicit mfa_enabled value it clears the derived MFA flag.
        patch_account_graph(
            session,
            model,
            summary_updates={"profile": {"amr": []}},
        )
        session.commit()
        security = session.get(AccountSecurityProfileModel, account_id)

    assert security.mfa_enabled is False
    assert security.get_amr() == []
    assert security.get_raw()["registration_auth_mode"] == "email_otp"


def test_raw_sanitizer_masks_phone_aliases_and_preserves_token_accounting():
    account_id = _create_account()
    raw_numbers = {
        "mobile": "+15555550123*",
        "mobile_number": "+15555550124",
        "msisdn": "+15555550125",
        "tel": "+15555550126",
    }

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "security_raw": {"contacts": raw_numbers},
                "chatgpt_usage": {
                    "plan_type": "free",
                    **raw_numbers,
                    "input_token_count": 17,
                    "output_token_usage": 9,
                    "access_token": "must-not-survive",
                },
            },
        )
        session.commit()
        security = session.get(AccountSecurityProfileModel, account_id)
        usage = session.exec(
            select(AccountUsageSnapshotModel)
            .where(AccountUsageSnapshotModel.account_id == account_id)
            .order_by(AccountUsageSnapshotModel.id.desc())
        ).first()

    security_json = json.dumps(security.get_raw(), ensure_ascii=False)
    usage_raw = usage.get_raw()
    usage_json = json.dumps(usage_raw, ensure_ascii=False)
    for raw_number in raw_numbers.values():
        assert raw_number.rstrip("*") not in security_json
        assert raw_number.rstrip("*") not in usage_json
    assert usage_raw["mobile"] == "+155****0123"
    assert usage_raw["mobile_number"] == "+155****0124"
    assert usage_raw["msisdn"] == "+155****0125"
    assert usage_raw["tel"] == "+155****0126"
    assert usage_raw["input_token_count"] == 17
    assert usage_raw["output_token_usage"] == 9
    assert "access_token" not in usage_raw


def test_expired_trial_is_transitioned_once():
    account_id = _create_account(lifecycle_status="trial")
    expired_at = int(datetime.now(timezone.utc).timestamp()) - 60

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "valid": True,
                "plan": "trial",
                "plan_state": "trial",
                "trial_end_time": expired_at,
            },
        )
        session.commit()

    first = flag_expiring_trials()
    second = flag_expiring_trials()

    assert first == {"warned": 0, "expired": 1, "skipped": 0}
    assert second == {"warned": 0, "expired": 0, "skipped": 0}
    with Session(engine) as session:
        subscription_row = session.get(AccountSubscriptionModel, account_id)
        status = session.get(AccountStatusModel, account_id)
    assert subscription_row.plan_state == "expired"
    assert status.lifecycle_status == "expired"
    assert status.display_status == "expired"
