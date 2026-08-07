from __future__ import annotations

import threading

from sqlmodel import Session, select

from application import tasks as tasks_module
from core.base_platform import Account
from core.db import TaskModel, engine, save_account
from domain.actions import ActionExecutionResult
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
from infrastructure.provider_settings_repository import ProviderSettingsRepository


class _FakeLogger:
    def __init__(self, task_id: str = "oauth-task"):
        self.task_id = task_id
        self.events = []
        self.result_data = None
        self.finished = None
        self.cancel_requested = False

    def log(self, message, **kwargs):
        self.events.append(("log", message, kwargs))

    def record_error(self, error):
        self.events.append(("error", error, {}))

    def record_success(self):
        self.events.append(("success", "", {}))

    def set_result_data(self, data):
        self.result_data = data

    def set_progress(self, current, total):
        self.events.append(("progress", current, {"total": total}))

    def is_cancel_requested(self):
        return self.cancel_requested

    def set_subtask(self, subtask_id, label=""):
        self.events.append(("subtask", subtask_id, {"label": label}))

    def clear_subtask(self):
        self.events.append(("clear_subtask", "", {}))

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def _configure_nvtokens(*, auto_push: bool, enabled: bool = True) -> None:
    ProviderDefinitionsRepository().ensure_seeded()
    ProviderSettingsRepository().save(
        setting_id=None,
        provider_type="push",
        provider_key="nvtokens",
        display_name="NexusVault",
        auth_mode="apikey",
        enabled=enabled,
        is_default=True,
        config={
            "nvtokens_endpoint": "https://nvtokens.test/api/inventory/cards/import",
            "nvtokens_payload_format": "sub2api",
            "nvtokens_auto_push_after_codex_oauth": "true" if auto_push else "false",
        },
        auth={"nvtokens_api_key": "test-key"},
        metadata={},
    )


def _configure_cpa(*, auto_push: bool, enabled: bool = True) -> None:
    ProviderDefinitionsRepository().ensure_seeded()
    ProviderSettingsRepository().save(
        setting_id=None,
        provider_type="push",
        provider_key="cpa",
        display_name="CPA（CLIProxyAPI）",
        auth_mode="bearer",
        enabled=enabled,
        is_default=True,
        config={
            "cpa_api_url": "http://127.0.0.1:8317",
            "cpa_auto_push_after_codex_oauth": "true" if auto_push else "false",
        },
        auth={"cpa_api_key": "management-secret"},
        metadata={},
    )


def _create_account(email: str = "oauth@example.com"):
    return save_account(Account(
        platform="chatgpt",
        email=email,
        password="Password123!",
        extra={
            "codex_access_token": "codex-access",
            "codex_refresh_token": "codex-refresh",
        },
    ))


def test_nvtokens_auto_push_toggle_defaults_to_false():
    ProviderDefinitionsRepository().ensure_seeded()
    definition = ProviderDefinitionsRepository().get_by_key("push", "nvtokens")
    field = next(
        item
        for item in definition.get_fields()
        if item.get("key") == "nvtokens_auto_push_after_codex_oauth"
    )
    assert field["type"] == "toggle"
    assert field["default_value"] == "false"


def test_cpa_auto_push_toggle_and_task_payload():
    ProviderDefinitionsRepository().ensure_seeded()
    definition = ProviderDefinitionsRepository().get_by_key("push", "cpa")
    field = next(
        item
        for item in definition.get_fields()
        if item.get("key") == "cpa_auto_push_after_codex_oauth"
    )
    assert field["type"] == "toggle"
    assert field["default_value"] == "false"
    assert tasks_module.enqueue_cpa_push_after_codex_oauth(1)["reason"] == "target_disabled"

    _configure_cpa(auto_push=False)
    assert tasks_module.enqueue_cpa_push_after_codex_oauth(1)["reason"] == "auto_push_disabled"

    _configure_cpa(auto_push=True)
    outcome = tasks_module.enqueue_cpa_push_after_codex_oauth(42)
    assert outcome["enqueued"] is True

    with Session(engine) as session:
        queued = session.exec(select(TaskModel)).one()
        assert queued.type == tasks_module.TASK_TYPE_ACCOUNT_PUSH
        assert queued.get_payload() == {
            "platform": "chatgpt",
            "account_ids": [42],
            "target_key": "cpa",
            "payload_format": "codex",
            "source": "codex_oauth",
        }


def test_codex_auto_push_only_enqueues_when_enabled_and_configured():
    assert tasks_module.enqueue_nvtokens_push_after_codex_oauth(1)["reason"] == "target_disabled"

    _configure_nvtokens(auto_push=False)
    assert tasks_module.enqueue_nvtokens_push_after_codex_oauth(1)["reason"] == "auto_push_disabled"

    _configure_nvtokens(auto_push=True)
    outcome = tasks_module.enqueue_nvtokens_push_after_codex_oauth(42)
    assert outcome["enqueued"] is True

    with Session(engine) as session:
        queued = session.exec(select(TaskModel)).one()
        assert queued.type == tasks_module.TASK_TYPE_ACCOUNT_PUSH
        assert queued.status == tasks_module.TASK_STATUS_PENDING
        assert queued.get_payload() == {
            "platform": "chatgpt",
            "account_ids": [42],
            "target_key": "nvtokens",
            "payload_format": "codex",
            "source": "codex_oauth",
        }
    assert tasks_module._task_account_keys(
        tasks_module.TASK_TYPE_ACCOUNT_PUSH,
        {"account_ids": [42], "source": "codex_oauth"},
    ) == []
    assert tasks_module._task_account_keys(
        tasks_module.TASK_TYPE_ACCOUNT_PUSH,
        {"account_ids": [42], "source": "manual"},
    ) == ["account:42"]


def test_account_push_task_reuses_service_with_strict_codex_format(monkeypatch):
    captured = {}

    class FakePushService:
        def push_accounts(self, selection, *, target_key, payload_format):
            captured.update(
                platform=selection.platform,
                ids=selection.ids,
                target_key=target_key,
                payload_format=payload_format,
            )
            return {
                "ok": True,
                "succeeded": 1,
                "failed": 0,
                "results": [{"account_id": 7, "ok": True, "error": ""}],
            }

    monkeypatch.setattr("application.account_pushes.AccountPushService", FakePushService)
    logger = _FakeLogger()
    tasks_module._execute_account_push_task(
        {
            "platform": "chatgpt",
            "account_ids": [7],
            "target_key": "nvtokens",
            "payload_format": "codex",
            "source": "codex_oauth",
        },
        logger,
    )

    assert captured == {
        "platform": "chatgpt",
        "ids": [7],
        "target_key": "nvtokens",
        "payload_format": "codex",
    }
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_account_push_task_does_not_start_after_cancellation(monkeypatch):
    service_created = []

    class FakePushService:
        def __init__(self):
            service_created.append(True)

    monkeypatch.setattr("application.account_pushes.AccountPushService", FakePushService)
    logger = _FakeLogger()
    logger.cancel_requested = True

    tasks_module._execute_account_push_task(
        {
            "platform": "chatgpt",
            "account_ids": [7],
            "target_key": "nvtokens",
            "payload_format": "codex",
        },
        logger,
    )

    assert service_created == []
    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_account_push_task_stays_cancelled_when_cancelled_during_remote_call(monkeypatch):
    logger = _FakeLogger()

    class FakePushService:
        def push_accounts(self, selection, *, target_key, payload_format):
            logger.cancel_requested = True
            return {
                "ok": True,
                "succeeded": 1,
                "failed": 0,
                "results": [{"account_id": 7, "ok": True, "error": ""}],
            }

    monkeypatch.setattr("application.account_pushes.AccountPushService", FakePushService)

    tasks_module._execute_account_push_task(
        {
            "platform": "chatgpt",
            "account_ids": [7],
            "target_key": "nvtokens",
            "payload_format": "codex",
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")
    assert logger.result_data is None
    assert not any(event[0] == "success" for event in logger.events)


def test_account_push_task_stays_cancelled_when_remote_call_raises_after_cancellation(monkeypatch):
    logger = _FakeLogger()

    class FakePushService:
        def push_accounts(self, selection, *, target_key, payload_format):
            logger.cancel_requested = True
            raise RuntimeError("remote disconnected")

    monkeypatch.setattr("application.account_pushes.AccountPushService", FakePushService)

    tasks_module._execute_account_push_task(
        {
            "platform": "chatgpt",
            "account_ids": [7],
            "target_key": "nvtokens",
            "payload_format": "codex",
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")
    assert not any(event[0] == "error" for event in logger.events)


def test_platform_oauth_stays_successful_when_auto_push_enqueue_fails(monkeypatch):
    _configure_nvtokens(auto_push=True)
    monkeypatch.setattr(
        tasks_module,
        "_execute_runtime_action_with_worker_proxy",
        lambda **kwargs: ActionExecutionResult(ok=True, data={"message": "OAuth 完成"}),
    )
    monkeypatch.setattr(
        tasks_module,
        "create_account_push_task",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("queue unavailable")),
    )
    monkeypatch.setattr(
        tasks_module,
        "_refresh_account_after_codex_oauth",
        lambda *args, **kwargs: {"ok": True, "valid": True},
    )
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 9,
            "action_id": "codex_oauth_authorize",
            "params": {},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert any("不影响 Codex OAuth" in str(event[1]) for event in logger.events)


def test_registration_inline_oauth_enqueues_once_after_account_save(monkeypatch):
    calls = []
    refresh_calls = []

    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Password123!",
                extra={
                    "codex_access_token": "codex-access",
                    "codex_refresh_token": "codex-refresh",
                    "post_codex_oauth": {"ok": True},
                },
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(tasks_module, "_build_platform_instance", lambda *args, **kwargs: FakePlatform())
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        tasks_module,
        "_log_codex_auto_push_enqueue",
        lambda account_id, logger, *, platform: calls.append((account_id, platform)),
    )
    monkeypatch.setattr(
        tasks_module,
        "_refresh_account_after_codex_oauth",
        lambda account_id, logger, **kwargs: refresh_calls.append(account_id) or {"ok": True, "valid": True},
    )
    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Password123!",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert len(calls) == 1
    account_id, platform = calls[0]
    assert account_id > 0
    assert platform == "chatgpt"
    assert refresh_calls == [account_id]


def test_batch_oauth_enqueues_once_per_success(monkeypatch):
    account = _create_account("batch@example.com")
    calls = []
    refresh_calls = []
    monkeypatch.setattr(
        tasks_module,
        "_execute_runtime_action_with_worker_proxy",
        lambda **kwargs: ActionExecutionResult(ok=True, data={"message": "OAuth 完成"}),
    )
    monkeypatch.setattr(
        tasks_module,
        "_log_codex_auto_push_enqueue",
        lambda account_id, logger, *, platform: calls.append((account_id, platform)),
    )
    monkeypatch.setattr(
        tasks_module,
        "_refresh_account_after_codex_oauth",
        lambda account_id, logger, **kwargs: refresh_calls.append(account_id) or {"ok": True, "valid": True},
    )
    logger = _FakeLogger()
    tasks_module._execute_codex_oauth_batch_task(
        {
            "platform": "chatgpt",
            "account_ids": [account.id],
            "params": {},
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert calls == [(account.id, "chatgpt")]
    assert refresh_calls == [account.id]


def test_batch_oauth_refreshes_each_account_immediately_after_its_authorization(monkeypatch):
    first = _create_account("first-immediate@example.com")
    second = _create_account("second-blocked@example.com")
    second_started = threading.Event()
    release_second = threading.Event()
    first_refreshed = threading.Event()
    events = []

    def execute_action(**kwargs):
        account_id = int(kwargs["account_id"])
        events.append(("oauth", account_id))
        if account_id == int(second.id):
            second_started.set()
            release_second.wait(timeout=10)
        return ActionExecutionResult(ok=True, data={"message": "OAuth done"})

    def refresh(account_id, logger, **kwargs):
        events.append(("refresh", int(account_id)))
        if int(account_id) == int(first.id):
            first_refreshed.set()
        return {"ok": True, "valid": True}

    monkeypatch.setattr(tasks_module, "_execute_runtime_action_with_worker_proxy", execute_action)
    monkeypatch.setattr(tasks_module, "_refresh_account_after_codex_oauth", refresh)
    monkeypatch.setattr(tasks_module, "_log_codex_auto_push_enqueue", lambda *args, **kwargs: None)
    logger = _FakeLogger()
    runner = threading.Thread(
        target=tasks_module._execute_codex_oauth_batch_task,
        args=(
            {
                "platform": "chatgpt",
                "account_ids": [first.id, second.id],
                "params": {},
                "concurrency": 2,
            },
            logger,
        ),
    )

    runner.start()
    assert second_started.wait(timeout=3)
    assert first_refreshed.wait(timeout=3)
    assert runner.is_alive()
    assert ("refresh", int(first.id)) in events
    assert ("refresh", int(second.id)) not in events

    release_second.set()
    runner.join(timeout=5)

    assert not runner.is_alive()
    assert ("refresh", int(second.id)) in events
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
