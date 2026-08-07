from __future__ import annotations

import threading

import requests

from application import tasks as tasks_module
from core.base_platform import Account
from domain.actions import ActionExecutionResult
from domain.actions import ActionExecutionCommand
from infrastructure import platform_runtime as runtime_module


class _FakeLogger:
    def __init__(self, task_id="test-task"):
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


def test_platform_action_task_passes_task_logger_to_runtime(monkeypatch):
    seen = {}

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            seen["log_fn"] = log_fn
            seen["cancel_check"] = cancel_check
            if log_fn:
                log_fn("checkout step log")
            return ActionExecutionResult(ok=True, data={"message": "summary"})

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert getattr(seen["log_fn"], "__name__", "") == "log"
    assert getattr(seen["cancel_check"], "__self__", None) is logger
    assert getattr(seen["cancel_check"], "__name__", "") == "is_cancel_requested"
    assert seen["cancel_check"]() is False
    assert ("log", "checkout step log", {}) in logger.events
    assert logger.result_data == {"message": "summary"}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_codex_oauth_action_refreshes_quota_after_success(monkeypatch):
    calls = []

    monkeypatch.setattr(
        tasks_module,
        "_execute_runtime_action_with_worker_proxy",
        lambda **kwargs: ActionExecutionResult(ok=True, data={"message": "OAuth done"}),
    )
    monkeypatch.setattr(
        tasks_module,
        "_refresh_account_after_codex_oauth",
        lambda account_id, logger, **kwargs: calls.append((account_id, kwargs)) or {"ok": True, "valid": True},
    )
    monkeypatch.setattr(tasks_module, "_log_codex_auto_push_enqueue", lambda *args, **kwargs: None)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "codex_oauth_authorize",
            "params": {"platform_proxy_mode": "manual", "platform_proxy_value": "http://proxy"},
        },
        logger,
    )

    assert calls == [(123, {"params": {"platform_proxy_mode": "manual", "platform_proxy_value": "http://proxy"}, "scope_id": "test-task"})]
    assert logger.result_data["quota_refresh"] == {"ok": True, "valid": True}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_codex_oauth_quota_refresh_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(tasks_module, "_run_single_account_check", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("usage unavailable")))
    logger = _FakeLogger()

    result = tasks_module._refresh_account_after_codex_oauth(123, logger)

    assert result == {"ok": False, "error": "usage unavailable"}
    assert any("OAuth 已成功，但额度刷新失败" in event[1] for event in logger.events if event[0] == "log")


def test_chatgpt_register_task_succeeds_after_successful_registration(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account, **kwargs: type("SavedAccount", (), {"id": 123})(),
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "mailbox",
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data == {
        "success": 1,
        "fail": 0,
        "account_ids": [123],
        "accounts": [
            {
                "account_id": 123,
                "email": "registered@example.com",
            }
        ],
    }
    assert any(event[0] == "success" for event in logger.events)
    assert not any(
        "cannot access local variable 'extra'" in str(event)
        for event in logger.events
    )


def test_register_task_honors_twenty_worker_concurrency_limit():
    assert tasks_module._registration_concurrency(20, 50) == 20
    assert tasks_module._registration_concurrency(99, 50) == 20
    assert tasks_module._registration_concurrency(20, 6) == 6


def test_codex_oauth_batch_concurrency_is_capped():
    assert tasks_module._codex_oauth_batch_concurrency(1, 10) == 1
    assert tasks_module._codex_oauth_batch_concurrency(3, 10) == 3
    assert tasks_module._codex_oauth_batch_concurrency(10, 30) == 10
    assert tasks_module._codex_oauth_batch_concurrency(20, 30) == 20
    assert tasks_module._codex_oauth_batch_concurrency(99, 30) == 20
    assert tasks_module._codex_oauth_batch_concurrency(5, 2) == 2


def test_relogin_batch_concurrency_is_capped():
    assert tasks_module._relogin_batch_concurrency(1, 10) == 1
    assert tasks_module._relogin_batch_concurrency(3, 10) == 3
    assert tasks_module._relogin_batch_concurrency(10, 30) == 10
    assert tasks_module._relogin_batch_concurrency(20, 30) == 10
    assert tasks_module._relogin_batch_concurrency(99, 30) == 10
    assert tasks_module._relogin_batch_concurrency(5, 2) == 2


def test_register_task_enables_hotmail007_prefetch(monkeypatch):
    events = []

    class FakeMailbox:
        def configure_prefetch(self, **kwargs):
            events.append(("configure_prefetch", kwargs))

        def shutdown_prefetch(self):
            events.append(("shutdown_prefetch", {}))

        def get_email(self):
            return type("MailboxAccount", (), {"email": "mailbox@example.com", "account_id": "mailbox@example.com", "extra": {}})()

        def get_current_ids(self, account):
            return set()

    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    fake_mailbox = FakeMailbox()
    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        "core.base_mailbox.create_mailbox",
        lambda *args, **kwargs: fake_mailbox,
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account, **kwargs: type("SavedAccount", (), {"id": 123})(),
    )
    monkeypatch.setattr(
        "core.mailbox_store.MailboxStore",
        lambda: type("Store", (), {"record_registration_link": lambda self, **kwargs: None})(),
    )

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 4,
            "concurrency": 3,
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "hotmail007",
            },
        },
        logger,
    )

    assert ("configure_prefetch", {"total_needed": 4, "buy_concurrency": 3, "queue_max": 4}) in events
    assert ("shutdown_prefetch", {}) in events
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_register_task_cancel_does_not_wait_for_blocked_worker(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class FakePlatform:
        def register(self, email=None, password=None):
            started.set()
            release.wait(timeout=10)
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account, **kwargs: type("SavedAccount", (), {"id": 123})(),
    )
    monkeypatch.setattr(
        "core.mailbox_store.MailboxStore",
        lambda: type("Store", (), {"record_registration_link": lambda self, **kwargs: None})(),
    )

    logger = _FakeLogger()
    runner = threading.Thread(
        target=tasks_module._execute_register_task,
        args=(
            {
                "platform": "chatgpt",
                "count": 1,
                "concurrency": 1,
                "extra": {"identity_provider": "mailbox", "mail_provider": "api_mailbox"},
            },
            logger,
        ),
    )
    runner.start()
    assert started.wait(timeout=2)
    logger.cancel_requested = True
    runner.join(timeout=3)
    release.set()
    assert not runner.is_alive()
    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_register_task_cancel_stops_scheduling_new_workers(monkeypatch):
    started_count = 0
    started_lock = threading.Lock()
    first_window_started = threading.Event()
    release = threading.Event()

    class FakePlatform:
        def register(self, email=None, password=None):
            nonlocal started_count
            with started_lock:
                started_count += 1
                if started_count == 2:
                    first_window_started.set()
            release.wait(timeout=10)
            return Account(
                platform="chatgpt",
                email=email or f"registered{started_count}@example.com",
                password=password or "Secret123!",
                user_id=f"acct_{started_count}",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account, **kwargs: type("SavedAccount", (), {"id": 123})(),
    )
    monkeypatch.setattr(
        "core.mailbox_store.MailboxStore",
        lambda: type("Store", (), {"record_registration_link": lambda self, **kwargs: None})(),
    )

    logger = _FakeLogger()
    runner = threading.Thread(
        target=tasks_module._execute_register_task,
        args=(
            {
                "platform": "chatgpt",
                "count": 5,
                "concurrency": 2,
                "extra": {"identity_provider": "mailbox", "mail_provider": "api_mailbox"},
            },
            logger,
        ),
    )
    runner.start()
    assert first_window_started.wait(timeout=2)
    logger.cancel_requested = True
    runner.join(timeout=3)
    with started_lock:
        observed_started = started_count
    release.set()

    assert not runner.is_alive()
    assert observed_started == 2
    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_register_task_forces_mailbox_api_direct_and_hides_proxy_log(monkeypatch):
    captured = {}

    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    def fake_create_mailbox(*, provider, extra, proxy):
        captured["mailbox_factory_proxy"] = proxy
        return object()

    def fake_build_platform_instance(*args, **kwargs):
        captured["platform_proxy"] = kwargs.get("platform_proxy")
        captured["mailbox_proxy"] = kwargs.get("mailbox_proxy")
        return FakePlatform()

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build_platform_instance)
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account, **kwargs: type("SavedAccount", (), {"id": 123})(),
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", fake_create_mailbox)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "platform_proxy_mode": "manual",
            "platform_proxy_value": "socks5://user:pass@proxy.example:1080",
            "mailbox_proxy_mode": "follow_platform",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert captured["platform_proxy"] == "socks5://user:pass@proxy.example:1080"
    assert captured["mailbox_proxy"] is None
    assert captured["mailbox_factory_proxy"] is None
    assert not any(
        "邮箱 API 代理" in str(event[1])
        for event in logger.events
        if event[0] == "log"
    )


def test_register_task_gives_each_worker_an_independent_proxy(monkeypatch):
    captured_proxies = []
    acquired = []
    lock = threading.Lock()

    class Lease:
        def __init__(self, url):
            self.url = url

        def report_success(self):
            pass

        def report_failure(self):
            pass

        def release(self):
            pass

    class FakePlatform:
        def __init__(self, proxy):
            self.proxy = proxy

        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=f"{self.proxy.rsplit(':', 1)[-1]}@example.com",
                password=password or "Secret123!",
                user_id=self.proxy,
                extra={"access_token": "access-token"},
            )

    def fake_acquire(**_kwargs):
        with lock:
            index = len(acquired) + 1
            lease = Lease(f"http://10.0.0.{index}:800{index}")
            acquired.append(lease)
            return lease

    def fake_build_platform_instance(*args, **kwargs):
        proxy = kwargs.get("platform_proxy")
        with lock:
            captured_proxies.append(proxy)
        return FakePlatform(proxy)

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build_platform_instance)
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account, **kwargs: type("SavedAccount", (), {"id": len(captured_proxies)})(),
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    monkeypatch.setattr("core.worker_proxy.worker_proxy_manager.acquire", fake_acquire)
    monkeypatch.setattr("core.worker_proxy.worker_proxy_manager.clear_scope", lambda scope_id: None)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "count": 3,
            "concurrency": 3,
            "platform_proxy_mode": "proxy_service",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert len(acquired) == 3
    assert len(set(captured_proxies)) == 3


def test_register_worker_replaces_proxy_after_network_failure(monkeypatch):
    acquired = []

    class Lease:
        def __init__(self, url):
            self.url = url
            self.failed = False

        def report_success(self):
            pass

        def report_failure(self):
            self.failed = True

        def release(self):
            pass

    class FakePlatform:
        def __init__(self, proxy):
            self.proxy = proxy

        def register(self, email=None, password=None):
            if self.proxy.endswith(":8001"):
                raise requests.exceptions.ProxyError("Unable to connect to proxy")
            return Account(
                platform="chatgpt",
                email="ok@example.com",
                password="Secret123!",
                user_id="acct-ok",
                extra={"access_token": "access-token"},
            )

    def fake_acquire(**_kwargs):
        lease = Lease(f"http://10.0.0.{len(acquired) + 1}:800{len(acquired) + 1}")
        acquired.append(lease)
        return lease

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(kwargs.get("platform_proxy")),
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account, **kwargs: type("SavedAccount", (), {"id": 123})(),
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    monkeypatch.setattr("core.worker_proxy.worker_proxy_manager.acquire", fake_acquire)
    monkeypatch.setattr("core.worker_proxy.worker_proxy_manager.clear_scope", lambda scope_id: None)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "count": 1,
            "concurrency": 1,
            "platform_proxy_mode": "proxy_service",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert len(acquired) == 2
    assert acquired[0].failed is True
    assert any("换 IP 重试 (2/3)" in str(event[1]) for event in logger.events)


def test_register_api_preserves_protocol_outlook_pool(client, monkeypatch):
    captured = {}

    def fake_create(payload, **_kwargs):
        captured.update(payload)
        return {"task_id": "task_protocol"}

    monkeypatch.setattr("api.task_commands.command_service.create_register_task", fake_create)
    pool_text = "user@outlook.com----mail-pass----client-id----refresh-token"

    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "concurrency": 1,
            "executor_type": "protocol",
            "extra": {
                "local_ms_pool_text": pool_text,
            },
        },
    )

    assert response.status_code == 200
    assert captured["executor_type"] == "protocol"
    assert captured["extra"]["mail_provider"] == "local_ms_pool"
    assert captured["extra"]["local_ms_pool_text"] == pool_text
    assert captured["extra"]["local_ms_pool_alias_count"] == 6
    assert captured["mailbox_proxy_mode"] == "direct"
    assert captured["mailbox_proxy_value"] == ""


def test_register_api_allows_six_outlook_child_addresses_per_parent(client, monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "api.task_commands.command_service.create_register_task",
        lambda payload: captured.update(payload) or {"task_id": "task_protocol"},
    )
    pool_text = "user@outlook.com----mail-pass----client-id----refresh-token"

    accepted = client.post(
        "/api/tasks/register",
        json={
            "count": 6,
            "executor_type": "protocol",
            "extra": {"local_ms_pool_text": pool_text},
        },
    )
    rejected = client.post(
        "/api/tasks/register",
        json={
            "count": 7,
            "executor_type": "protocol",
            "extra": {"local_ms_pool_text": pool_text},
        },
    )

    assert accepted.status_code == 200
    assert captured["extra"]["local_ms_pool_alias_count"] == 6
    assert rejected.status_code == 400
    assert "子邮箱容量 6" in rejected.json()["detail"]


def test_register_api_rejects_fixed_mailbox_batch(client):
    response = client.post(
        "/api/tasks/register",
        json={
            "count": 2,
            "concurrency": 1,
            "executor_type": "headless",
            "extra": {"mailbox_address_id": "addr_1"},
        },
    )

    assert response.status_code == 400
    assert "单账号" in response.json()["detail"]


def test_register_api_rejects_fixed_mailbox_protocol(client):
    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "concurrency": 1,
            "executor_type": "protocol",
            "extra": {
                "mailbox_address_id": "addr_1",
                "local_ms_pool_text": "user@outlook.com----mail-pass----client-id----refresh-token",
            },
        },
    )

    assert response.status_code == 400
    assert "指定邮箱注册" in response.json()["detail"]


def test_register_task_uses_fixed_mailbox_address(monkeypatch):
    captured = {}

    class FakeMailbox:
        def get_current_ids(self, account):
            captured["current_ids_account"] = account
            return set()

    class FakeStore:
        def resolve_mailbox_for_address(self, *, mailbox_address_id, proxy=None, extra=None):
            captured["mailbox_address_id"] = mailbox_address_id
            captured["proxy"] = proxy
            captured["extra"] = dict(extra or {})
            mailbox_account = type(
                "MailboxAccountStub",
                (),
                {"email": "fixed@example.com", "account_id": mailbox_address_id},
            )()
            return FakeMailbox(), mailbox_account, {"account": {"provider": "api_mailbox"}}

        def record_registration_link(self, *, account_id, platform_account):
            captured["linked"] = (account_id, platform_account.email)

    class FakePlatform:
        def __init__(self, *args, **kwargs):
            captured["platform_mailbox"] = kwargs.get("mailbox")

        def set_logger(self, logger):
            self.logger = logger

        def register(self, email=None, password=None):
            captured["register_email"] = email
            captured["provided_mailbox_email"] = captured["platform_mailbox"].get_email().email
            return Account(
                platform="chatgpt",
                email=email or "fixed@example.com",
                password=password or "Secret123!",
                extra={
                    "access_token": "access-token",
                    "provider_accounts": [
                        {
                            "provider_type": "mailbox",
                            "provider_name": "api_mailbox",
                            "login_identifier": "fixed@example.com",
                        }
                    ],
                },
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: FakePlatform)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.mailbox_store.MailboxStore",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account, **kwargs: type("SavedAccount", (), {"id": 456})(),
    )

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "executor_type": "headless",
            "extra": {
                "identity_provider": "mailbox",
                "mailbox_address_id": "addr_fixed",
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert captured["mailbox_address_id"] == "addr_fixed"
    assert captured["register_email"] == "fixed@example.com"
    assert captured["provided_mailbox_email"] == "fixed@example.com"
    assert "linked" not in captured


def test_register_task_failure_releases_canonical_mailbox_allocation(monkeypatch):
    captured = {}

    class FakePlatform:
        _last_identity = type(
            "Identity",
            (),
            {"metadata": {"mailbox_allocation_id": "mba_failure"}},
        )()

        def register(self, email=None, password=None):
            raise RuntimeError("registration failed")

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())

    def fake_release(self, allocation_id, *, outcome, reason=""):
        captured.update(
            allocation_id=allocation_id,
            outcome=outcome,
            reason=reason,
        )
        return True

    monkeypatch.setattr("core.mailbox_lifecycle.MailboxAllocationLifecycle.release", fake_release)

    logger = _FakeLogger(task_id="task-release-failure")
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "executor_type": "headless",
            "extra": {"identity_provider": "mailbox", "mail_provider": "api_mailbox"},
        },
        logger,
    )

    assert captured == {
        "allocation_id": "mba_failure",
        "outcome": "failed",
        "reason": "registration failed",
    }


def test_register_api_allows_protocol_with_global_mailbox_provider(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "api.task_commands.command_service.create_register_task",
        lambda payload: captured.update(payload) or {"task_id": "task_protocol"},
    )

    response = client.post(
        "/api/tasks/register",
        json={
            "executor_type": "protocol",
            "count": 1,
            "extra": {"mail_provider": "api_mailbox"},
        },
    )

    assert response.status_code == 200
    assert captured["extra"]["mail_provider"] == "api_mailbox"
    assert "local_ms_pool_alias_count" not in captured["extra"]


def test_platform_action_task_finishes_cancelled_without_starting_runtime(monkeypatch):
    class FakeRuntime:
        def execute_action(self, *args, **kwargs):
            raise AssertionError("runtime should not start after cancellation")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()
    logger.cancel_requested = True

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_action_task_marks_cancelled_after_runtime_cancel(monkeypatch):
    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            assert cancel_check() is False
            logger.cancel_requested = True
            return ActionExecutionResult(ok=False, error="任务已取消")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_runtime_wires_log_fn_to_platform(monkeypatch):
    logs = []
    seen = {}

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt"})()

        def add(self, model):
            pass

        def commit(self):
            pass

    class FakePlatform:
        def __init__(self, config=None):
            self._log_fn = print

        def set_logger(self, logger):
            self._log_fn = logger

        def set_cancel_checker(self, checker):
            seen["cancel_check"] = checker

        def execute_action(self, action_id, account, params):
            self._log_fn("runtime platform log")
            assert self.is_cancel_requested() is False
            return {"ok": True, "data": {"message": "ok"}}

        def is_cancel_requested(self):
            return seen["cancel_check"]()

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", lambda *args, **kwargs: None)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="query_state",
            params={},
        ),
        log_fn=logs.append,
        cancel_check=lambda: False,
    )

    assert result.ok is True
    assert logs == ["runtime platform log"]
    assert seen["cancel_check"]() is False


def test_platform_runtime_resolves_action_proxy_service(monkeypatch):
    seen = {}

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt"})()

        def add(self, model):
            pass

        def commit(self):
            pass

    class FakePlatform:
        def __init__(self, config=None):
            seen["proxy"] = config.proxy
            seen["extra"] = dict(config.extra or {})

        def execute_action(self, action_id, account, params):
            return {"ok": True, "data": {"message": "ok"}}

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.proxy_pool.proxy_pool.get_next", lambda: "http://pool-proxy:8080")

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="query_state",
            params={"platform_proxy_mode": "proxy_service"},
        )
    )

    assert result.ok is True
    assert seen["proxy"] == "http://pool-proxy:8080"
    assert seen["extra"]["disable_proxy_pool"] is False


def test_platform_runtime_proxy_service_never_silently_uses_direct(monkeypatch):
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: object)
    monkeypatch.setattr("core.proxy_pool.proxy_pool.get_next", lambda: None)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="query_state",
            params={"platform_proxy_mode": "proxy_service"},
        )
    )

    assert result.ok is False
    assert result.error == "代理服务未返回可用代理"
