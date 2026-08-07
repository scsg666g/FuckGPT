from __future__ import annotations

import pytest
from sqlmodel import Session

from application import tasks as tasks_module
from core.account_graph import load_account_graphs
from core.base_platform import Account
from core.db import AccountModel, AccountStatusModel, engine, save_account
from domain.actions import ActionExecutionCommand, ActionExecutionResult
from infrastructure import platform_runtime as runtime_module
from platforms.chatgpt.browser_register import (
    ExistingAccountAuthenticationError,
    _browser_registration_flow,
)
from platforms.chatgpt.relogin import (
    ChatGPTReloginError,
    classify_relogin_failure,
    perform_chatgpt_relogin,
    validate_relogin_result,
)


class _FakeLogger:
    def __init__(self):
        self.task_id = "relogin-task"
        self.events = []
        self.result_data = None
        self.finished = None

    def log(self, message, **kwargs):
        self.events.append(("log", message, kwargs))

    def record_error(self, error):
        self.events.append(("error", error, {}))

    def record_success(self):
        self.events.append(("success", "", {}))

    def set_subtask(self, subtask_id, label=""):
        self.events.append(("subtask", subtask_id, {"label": label}))

    def clear_subtask(self):
        self.events.append(("clear_subtask", "", {}))

    def set_result_data(self, data):
        self.result_data = data

    def set_progress(self, current, total):
        self.events.append(("progress", current, {"total": total}))

    def is_cancel_requested(self):
        return False

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def test_validate_relogin_result_normalizes_success():
    result = validate_relogin_result(
        {
            "access_token": "new-access",
            "session_token": "new-session",
            "account_id": "acct-1",
            "profile": {"email": "User@Example.com"},
            "registration_auth_mode": "password",
        },
        expected_email="user@example.com",
        expected_account_id="acct-1",
    )

    assert result["valid"] is True
    assert result["remote_email"] == "User@Example.com"
    assert result["last_login_status"] == "succeeded"
    assert result["registration_auth_mode"] == "password"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"profile": {"email": "user@example.com"}}, "session_missing"),
        (
            {"access_token": "token", "profile": {"email": "other@example.com"}},
            "identity_mismatch",
        ),
        (
            {"access_token": "token", "account_id": "other", "profile": {"email": "user@example.com"}},
            "identity_mismatch",
        ),
    ],
)
def test_validate_relogin_result_rejects_unsafe_output(payload, code):
    with pytest.raises(ChatGPTReloginError) as caught:
        validate_relogin_result(
            payload,
            expected_email="user@example.com",
            expected_account_id="acct-1",
        )

    assert caught.value.failure.code == code


def test_classify_relogin_failure_keeps_machine_readable_reason():
    assert classify_relogin_failure(RuntimeError("密码错误")).code == "credentials_invalid"
    assert classify_relogin_failure(TimeoutError("browser timeout")).code == "timeout"
    assert classify_relogin_failure(RuntimeError("代理网络失败")).code == "network_or_proxy"
    assert classify_relogin_failure(RuntimeError("error_code: account_deactivated")).code == "account_deactivated"


def test_perform_relogin_enables_existing_account_only(monkeypatch):
    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def run_isolated(self, email, password, *, password_provided):
            captured["run"] = (email, password, password_provided)
            return {
                "access_token": "new-access",
                "session_token": "new-session",
                "account_id": "acct-1",
                "profile": {"email": email},
                "registration_auth_mode": "password",
            }

    monkeypatch.setattr("platforms.chatgpt.browser_register.ChatGPTBrowserRegister", FakeWorker)

    result = perform_chatgpt_relogin(
        email="user@example.com",
        password="Secret123!",
        expected_account_id="acct-1",
        headless=True,
    )

    assert captured["init"]["existing_account_only"] is True
    assert captured["run"] == ("user@example.com", "Secret123!", True)
    assert result["access_token"] == "new-access"


def test_existing_account_only_flow_refuses_registration_page(monkeypatch):
    class Page:
        url = "https://auth.openai.com/create-account/password"

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args: {
            "page_type": "create_account_password",
            "current_url": Page.url,
        },
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {})

    with pytest.raises(ExistingAccountAuthenticationError, match="已拒绝继续注册"):
        _browser_registration_flow(
            Page(),
            "user@example.com",
            "Secret123!",
            None,
            lambda message: None,
            existing_account_only=True,
        )


def test_relogin_ignores_false_home_after_nextauth_timeout(monkeypatch):
    calls = []
    logs = []

    class Page:
        url = "https://chatgpt.com/"

    monkeypatch.setattr("platforms.chatgpt.browser_register._seed_browser_device_id", lambda *args: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_authorize",
        lambda *args: (_ for _ in ()).throw(TimeoutError("network timeout")),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._derive_registration_state_from_page",
        lambda page: {"page_type": "chatgpt_home", "current_url": page.url},
    )
    monkeypatch.setattr("platforms.chatgpt.browser_register._get_cookies", lambda page: {"oai-did": "device"})
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_browser_signup_via_page",
        lambda page, email, log, **kwargs: calls.append(kwargs) or {
            "page_type": "create_account_password",
            "current_url": "https://auth.openai.com/create-account/password",
        },
    )

    with pytest.raises(ExistingAccountAuthenticationError, match="已拒绝继续注册"):
        _browser_registration_flow(
            Page(),
            "user@example.com",
            "Secret123!",
            None,
            logs.append,
            existing_account_only=True,
        )

    assert calls == [{"flow_label": "重新登录"}]
    assert any("忽略假完成状态" in message for message in logs)


def test_relogin_persistence_replaces_session_data_and_records_failure():
    saved = save_account(
        Account(
            platform="chatgpt",
            email="user@example.com",
            password="Secret123!",
            user_id="old-account",
            extra={
                "access_token": "old-access",
                "refresh_token": "stale-refresh",
                "session_token": "old-session",
                "cookies": "old-cookie",
            },
        )
    )
    command = ActionExecutionCommand(
        platform="chatgpt",
        account_id=int(saved.id),
        action_id="relogin",
    )

    runtime_module._persist_action_result(
        command,
        {
            "ok": True,
            "data": {
                "access_token": "new-access",
                "session_token": "new-session",
                "cookies": "new-cookie",
                "account_id": "new-account",
                "profile": {"email": "user@example.com"},
                "remote_email": "user@example.com",
                "registration_auth_mode": "password",
                "checked_at": "2026-08-02T01:02:03Z",
            },
        },
    )

    with Session(engine) as session:
        model = session.get(AccountModel, int(saved.id))
        graph = load_account_graphs(session, [int(saved.id)])[int(saved.id)]
        credentials = {item["key"]: item["value"] for item in graph["credentials"]}
        assert model.user_id == "new-account"
        assert credentials["access_token"] == "new-access"
        assert credentials["session_token"] == "new-session"
        assert credentials["cookies"] == "new-cookie"
        assert "refresh_token" not in credentials
        assert graph["overview"]["last_login_status"] == "succeeded"

    runtime_module._persist_action_result(
        command,
        {
            "ok": False,
            "error": "重新登录失败 [credentials_invalid]: 密码错误",
            "data": {
                "failure_code": "credentials_invalid",
                "failed_at": "2026-08-02T02:03:04Z",
            },
        },
    )

    with Session(engine) as session:
        graph = load_account_graphs(session, [int(saved.id)])[int(saved.id)]
        credentials = {item["key"]: item["value"] for item in graph["credentials"]}
        assert credentials["access_token"] == "new-access"
        assert graph["overview"]["last_login_status"] == "failed"
        assert graph["overview"]["last_login_failure_code"] == "credentials_invalid"
        assert "密码错误" in graph["overview"]["check_error"]


def test_relogin_persistence_marks_account_deactivated():
    saved = save_account(Account(platform="chatgpt", email="deactivated@example.com", password="Secret123!"))
    command = ActionExecutionCommand(
        platform="chatgpt",
        account_id=int(saved.id),
        action_id="relogin",
        params={},
    )

    runtime_module._persist_action_result(
        command,
        {
            "ok": False,
            "error": (
                "重新登录失败 [account_deactivated]: 認証エラー "
                "アカウントは削除または無効化されているため、ご利用いただけません。"
                "誤りと思われる場合は、help.openai.comのへルプセンタ一からお問い合わせください。 "
                "error_code: account_deactivated"
            ),
            "data": {
                "failure_code": "account_deactivated",
                "failed_at": "2026-08-02T02:03:04Z",
            },
        },
    )

    with Session(engine) as session:
        status = session.get(AccountStatusModel, int(saved.id))
        graph = load_account_graphs(session, [int(saved.id)])[int(saved.id)]
        assert status is not None
        assert status.validity_status == "deactivated"
        assert status.display_status == "deactivated"
        assert status.invalid_check_count >= 2
        assert graph["overview"]["valid"] is False
        assert graph["overview"]["last_login_failure_code"] == "account_deactivated"
        assert graph["overview"]["display_status"] == "deactivated"
        assert graph["overview"]["deactivation_reason"] == (
            "アカウントは削除または無効化されているため、ご利用いただけません。"
            "誤りと思われる場合は、help.openai.comのへルプセンタ一からお問い合わせください。"
        )
        assert graph["overview"]["deactivation_detected_at"] == "2026-08-02T02:03:04Z"
        assert "account_deactivated" in graph["overview"]["deactivation_error"]


def test_relogin_task_refreshes_account_after_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks_module,
        "_execute_runtime_action_with_worker_proxy",
        lambda **kwargs: ActionExecutionResult(ok=True, data={"message": "重新登录成功"}),
    )
    monkeypatch.setattr(
        tasks_module,
        "_refresh_account_after_relogin",
        lambda account_id, logger, **kwargs: calls.append((account_id, kwargs)) or {"ok": True},
    )
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 7,
            "action_id": "relogin",
            "params": {"browser_mode": "headless"},
        },
        logger,
    )

    assert calls == [
        (
            7,
            {
                "params": {"browser_mode": "headless"},
                "scope_id": "relogin-task",
            },
        )
    ]
    assert logger.result_data["account_refresh"] == {"ok": True}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_relogin_task_persists_proxy_failure_before_runtime(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        tasks_module,
        "_execute_runtime_action_with_worker_proxy",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("代理预检失败")),
    )
    monkeypatch.setattr(
        tasks_module,
        "persist_action_failure",
        lambda **kwargs: recorded.append(kwargs),
    )
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 7,
            "action_id": "relogin",
            "params": {"platform_proxy_mode": "proxy_service"},
        },
        logger,
    )

    assert recorded[0]["platform"] == "chatgpt"
    assert recorded[0]["account_id"] == 7
    assert recorded[0]["action_id"] == "relogin"
    assert recorded[0]["data"]["failure_code"] == "network_or_proxy"
    assert "代理预检失败" in recorded[0]["error"]
    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED


def test_relogin_batch_refreshes_each_successful_account(monkeypatch):
    first = save_account(AccountModel(platform="chatgpt", email="batch-first@example.com", password="Secret123!"))
    second = save_account(AccountModel(platform="chatgpt", email="batch-second@example.com", password="Secret123!"))
    calls = []

    monkeypatch.setattr(
        tasks_module,
        "_execute_runtime_action_with_worker_proxy",
        lambda **kwargs: ActionExecutionResult(
            ok=True,
            data={
                "message": "重新登录成功",
                "account_id": f"account-{kwargs['account_id']}",
                "remote_email": "batch@example.com",
                "registration_auth_mode": "email_otp",
                "checked_at": "2026-08-02T03:04:05Z",
                "access_token": "must-not-leak",
            },
        ),
    )
    monkeypatch.setattr(
        tasks_module,
        "_refresh_account_after_relogin",
        lambda account_id, logger, **kwargs: calls.append((account_id, kwargs)) or {"ok": True},
    )
    logger = _FakeLogger()

    tasks_module._execute_relogin_batch_task(
        {
            "platform": "chatgpt",
            "account_ids": [int(first.id), int(second.id)],
            "params": {"browser_mode": "headless"},
            "concurrency": 2,
        },
        logger,
    )

    assert sorted(account_id for account_id, _kwargs in calls) == sorted([int(first.id), int(second.id)])
    assert logger.result_data["success"] == 2
    assert logger.result_data["fail"] == 0
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert all("access_token" not in item.get("data", {}) for item in logger.result_data["accounts"])


def test_relogin_batch_persists_pre_runtime_failure(monkeypatch):
    saved = save_account(AccountModel(platform="chatgpt", email="batch-fail@example.com", password="Secret123!"))
    recorded = []

    monkeypatch.setattr(
        tasks_module,
        "_execute_runtime_action_with_worker_proxy",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("代理预检失败")),
    )
    monkeypatch.setattr(
        tasks_module,
        "persist_action_failure",
        lambda **kwargs: recorded.append(kwargs),
    )
    logger = _FakeLogger()

    tasks_module._execute_relogin_batch_task(
        {
            "platform": "chatgpt",
            "account_ids": [int(saved.id)],
            "params": {"platform_proxy_mode": "proxy_service"},
            "concurrency": 1,
        },
        logger,
    )

    assert recorded[0]["platform"] == "chatgpt"
    assert recorded[0]["account_id"] == int(saved.id)
    assert recorded[0]["action_id"] == "relogin"
    assert recorded[0]["data"]["failure_code"] == "network_or_proxy"
    assert logger.result_data["success"] == 0
    assert logger.result_data["fail"] == 1
    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED


def test_account_check_does_not_relogin_invalid_by_default(monkeypatch):
    saved = save_account(AccountModel(platform="chatgpt", email="check-invalid@example.com", password="Secret123!"))
    relogin_calls = []
    settings = type(
        "Settings",
        (),
        {
            "batch_limit": 50,
            "concurrency": 1,
            "request_timeout_seconds": 5,
            "proxy_mode": "direct",
            "proxy_url": "",
        },
    )()

    monkeypatch.setattr("core.account_check_settings.get_account_check_settings", lambda: settings)
    monkeypatch.setattr(
        tasks_module,
        "_run_single_account_check",
        lambda account_id, **kwargs: (
            False,
            {"account_id": account_id, "valid": False, "platform": "chatgpt", "email": "check-invalid@example.com"},
        ),
    )
    monkeypatch.setattr(
        tasks_module,
        "_execute_relogin_for_account",
        lambda **kwargs: relogin_calls.append(kwargs) or {"ok": True, "account_refresh": {"valid": True}},
    )
    logger = _FakeLogger()

    tasks_module._execute_configured_account_check_task(
        {
            "platform": "chatgpt",
            "account_ids": [int(saved.id)],
            "platform_proxy_mode": "direct",
            "concurrency": 1,
            "request_timeout_seconds": 5,
        },
        logger,
    )

    assert relogin_calls == []
    assert logger.result_data["invalid"] == 1
    assert logger.result_data["relogin_success"] == 0
    assert logger.result_data["relogin_failed"] == 0
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_account_check_relogins_invalid_when_enabled(monkeypatch):
    saved = save_account(AccountModel(platform="chatgpt", email="check-relogin@example.com", password="Secret123!"))
    relogin_calls = []
    settings = type(
        "Settings",
        (),
        {
            "batch_limit": 50,
            "concurrency": 1,
            "request_timeout_seconds": 5,
            "proxy_mode": "direct",
            "proxy_url": "",
        },
    )()

    monkeypatch.setattr("core.account_check_settings.get_account_check_settings", lambda: settings)
    monkeypatch.setattr(
        tasks_module,
        "_run_single_account_check",
        lambda account_id, **kwargs: (
            False,
            {"account_id": account_id, "valid": False, "platform": "chatgpt", "email": "check-relogin@example.com"},
        ),
    )
    monkeypatch.setattr(
        tasks_module,
        "_execute_relogin_for_account",
        lambda **kwargs: relogin_calls.append(kwargs) or {"ok": True, "account_refresh": {"valid": True}},
    )
    logger = _FakeLogger()

    tasks_module._execute_configured_account_check_task(
        {
            "platform": "chatgpt",
            "account_ids": [int(saved.id)],
            "platform_proxy_mode": "direct",
            "concurrency": 1,
            "request_timeout_seconds": 5,
            "relogin_invalid": True,
            "relogin_params": {"browser_mode": "headless"},
        },
        logger,
    )

    assert len(relogin_calls) == 1
    assert relogin_calls[0]["account_id"] == int(saved.id)
    assert relogin_calls[0]["params"]["browser_mode"] == "headless"
    assert relogin_calls[0]["params"]["keep_browser_open"] == "false"
    assert relogin_calls[0]["params"]["platform_proxy_mode"] == "direct"
    assert logger.result_data["valid"] == 1
    assert logger.result_data["invalid"] == 0
    assert logger.result_data["relogin_success"] == 1
    assert logger.result_data["relogin_failed"] == 0
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_account_check_logs_deactivated_relogin_failure_as_banned(monkeypatch):
    saved = save_account(AccountModel(platform="chatgpt", email="check-deactivated@example.com", password="Secret123!"))
    settings = type(
        "Settings",
        (),
        {
            "batch_limit": 50,
            "concurrency": 1,
            "request_timeout_seconds": 5,
            "proxy_mode": "direct",
            "proxy_url": "",
        },
    )()

    monkeypatch.setattr("core.account_check_settings.get_account_check_settings", lambda: settings)
    monkeypatch.setattr(
        tasks_module,
        "_run_single_account_check",
        lambda account_id, **kwargs: (
            False,
            {"account_id": account_id, "valid": False, "platform": "chatgpt", "email": "check-deactivated@example.com"},
        ),
    )
    monkeypatch.setattr(
        tasks_module,
        "_execute_relogin_for_account",
        lambda **kwargs: {
            "ok": False,
            "error": "重新登录失败 [account_deactivated]: RuntimeError: 验证码校验失败: error_code: account_deactivated",
            "data": {"failure_code": "account_deactivated"},
        },
    )
    logger = _FakeLogger()

    tasks_module._execute_configured_account_check_task(
        {
            "platform": "chatgpt",
            "account_ids": [int(saved.id)],
            "platform_proxy_mode": "direct",
            "concurrency": 1,
            "request_timeout_seconds": 5,
            "relogin_invalid": True,
            "relogin_params": {"browser_mode": "headless"},
        },
        logger,
    )

    messages = [event[1] for event in logger.events if event[0] == "log"]
    assert any("失效后重登失败: 账号已封号" in message for message in messages)
    assert any("account_deactivated" in message for message in messages)
    assert logger.result_data["invalid"] == 1
    assert logger.result_data["relogin_failed"] == 1
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_actions_expose_relogin():
    actions = runtime_module.PlatformRuntime().list_actions("chatgpt")
    relogin = next(action for action in actions if action.id == "relogin")

    assert relogin.label == "重新登录"
    assert [parameter.key for parameter in relogin.params] == [
        "browser_mode",
        "keep_browser_open",
        "platform_proxy_mode",
        "platform_proxy_value",
    ]
