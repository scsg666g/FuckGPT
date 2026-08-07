from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import features.kakao_pipeline.reconciler as reconciler_module
from sqlmodel import Session, select

from core.base_platform import Account
from core.db import AccountCodexAuthModel, KakaoPipelineModel, TaskModel, engine, save_account
from features.kakao_pipeline.reconciler import KakaoPipelineReconciler
from features.kakao_pipeline.service import KakaoPipelineService


def _create_account(email: str) -> int:
    account = save_account(
        Account(
            platform="chatgpt",
            email=email,
            password="TestPass123!",
            extra={"access_token": "background-test-token"},
        )
    )
    return int(account.id)


def test_background_work_is_discovered_from_all_persisted_pipelines():
    states = [
        "supplier_processing",
        "scanner_processing",
        "scanner_succeeded",
        "plus_checking",
        "plus_pending",
        "link_ready",
        "completed",
    ]
    account_ids = [_create_account(f"background-{index}@test.com") for index in range(len(states))]
    with Session(engine) as session:
        for account_id, state in zip(account_ids, states, strict=True):
            session.add(KakaoPipelineModel(account_id=account_id, state=state))
        session.commit()

    work = KakaoPipelineService().list_background_work()

    assert {(item["account_id"], item["state"]) for item in work} == {
        (account_ids[0], "supplier_processing"),
        (account_ids[1], "scanner_processing"),
        (account_ids[2], "scanner_succeeded"),
        (account_ids[3], "plus_checking"),
        (account_ids[4], "plus_pending"),
    }


def test_background_advance_skips_stale_selected_state(monkeypatch):
    account_id = _create_account("background-stale@test.com")
    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="link_ready"))
        session.commit()

    service = KakaoPipelineService()
    monkeypatch.setattr(
        service,
        "poll_supplier",
        lambda _account_id: (_ for _ in ()).throw(AssertionError("stale work must not run")),
    )

    result = service.advance_background(account_id, expected_state="supplier_processing")

    assert result["state"] == "link_ready"


def test_background_scanner_success_runs_pipeline_plus_check(monkeypatch):
    account_id = _create_account("background-scanner-success@test.com")
    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_processing"))
        session.commit()

    service = KakaoPipelineService()
    calls: list[tuple[str, int, bool]] = []
    monkeypatch.setattr(service, "poll_scanner", lambda _account_id: {"state": "scanner_succeeded"})

    def check_plus(current_account_id: int, *, advance_pipeline: bool = False):
        calls.append(("plus", current_account_id, advance_pipeline))
        return {"state": "plus_pending"}

    monkeypatch.setattr(service, "check_plus", check_plus)

    result = service.advance_background(account_id, expected_state="scanner_processing")

    assert result["state"] == "plus_pending"
    assert calls == [("plus", account_id, True)]


def test_reconciler_processes_all_work_with_bounded_concurrency():
    class FakeService:
        def __init__(self):
            self.guard = threading.Lock()
            self.states = {
                1: "supplier_processing",
                2: "scanner_processing",
                3: "scanner_succeeded",
                4: "plus_pending",
                5: "supplier_processing",
            }
            self.calls: list[int] = []
            self.active = 0
            self.max_active = 0

        def list_background_work(self, *, limit: int):
            with self.guard:
                return [
                    {"account_id": account_id, "state": state}
                    for account_id, state in list(self.states.items())[:limit]
                ]

        def advance_background(self, account_id: int, *, expected_state: str):
            with self.guard:
                assert self.states[account_id] == expected_state
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls.append(account_id)
            time.sleep(0.03)
            with self.guard:
                self.active -= 1
                self.states.pop(account_id, None)
            return {"state": "completed"}

    service = FakeService()
    reconciler = KakaoPipelineReconciler(
        service=service,  # type: ignore[arg-type]
        scan_interval=0.01,
        max_workers=2,
    )
    reconciler.start()
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            with service.guard:
                if not service.states and service.active == 0:
                    break
            time.sleep(0.01)
    finally:
        reconciler.stop()

    assert sorted(service.calls) == [1, 2, 3, 4, 5]
    assert service.max_active == 2
    assert reconciler.running is False


def test_reconciler_retries_one_failure_without_stopping(monkeypatch):
    monkeypatch.setattr(reconciler_module, "_ERROR_RETRY_DELAYS_SECONDS", (0.01,))

    class FlakyService:
        def __init__(self):
            self.guard = threading.Lock()
            self.pending = True
            self.calls = 0

        def list_background_work(self, *, limit: int):
            with self.guard:
                return [{"account_id": 7, "state": "supplier_processing"}] if self.pending else []

        def advance_background(self, account_id: int, *, expected_state: str):
            assert account_id == 7
            assert expected_state == "supplier_processing"
            with self.guard:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary supplier failure")
                self.pending = False
            return {"state": "completed"}

    service = FlakyService()
    reconciler = KakaoPipelineReconciler(
        service=service,  # type: ignore[arg-type]
        scan_interval=0.01,
        max_workers=1,
    )
    reconciler.start()
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            with service.guard:
                if service.calls >= 2 and not service.pending:
                    break
            time.sleep(0.01)
    finally:
        reconciler.stop()

    assert service.calls == 2


def test_plus_pending_is_discovered_only_when_persisted_next_check_is_due():
    account_id = _create_account("plus-next-check@test.com")
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="plus_pending",
                plus_next_check_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        session.commit()

    service = KakaoPipelineService()
    assert service.list_background_work() == []

    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        pipeline.plus_next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(pipeline)
        session.commit()

    assert service.list_background_work() == [{"account_id": account_id, "state": "plus_pending"}]


def test_untracked_plus_is_discovered_only_when_persisted_next_check_is_due():
    account_id = _create_account("untracked-next-check@test.com")
    started_at = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_accepted_untracked",
                scanner_recovery_started_at=started_at,
                scanner_recovery_next_check_at=started_at + timedelta(minutes=5),
                scanner_recovery_deadline_at=started_at + timedelta(minutes=30),
            )
        )
        session.commit()

    service = KakaoPipelineService()
    assert service.list_background_work() == []

    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        pipeline.scanner_recovery_next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(pipeline)
        session.commit()

    assert service.list_background_work() == [
        {"account_id": account_id, "state": "scanner_accepted_untracked"}
    ]


def test_only_explicitly_armed_completed_pipeline_gets_codex_background_work():
    legacy_id = _create_account("legacy-completed@test.com")
    armed_id = _create_account("armed-completed@test.com")
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=legacy_id,
                state="completed",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
            )
        )
        session.add(
            KakaoPipelineModel(
                account_id=armed_id,
                state="completed",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
                codex_post_action_armed=True,
            )
        )
        session.commit()

    work = KakaoPipelineService().list_background_work()

    assert work == [{"account_id": armed_id, "state": "codex_post_action"}]


def test_reconciler_requeues_never_started_interrupted_codex_once():
    account_id = _create_account("interrupted-codex@test.com")
    old_task_id = "old-never-started-codex"
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
                codex_post_action_armed=True,
                codex_task_id=old_task_id,
                codex_attempt_count=1,
            )
        )
        session.add(
            TaskModel(
                id=old_task_id,
                type="codex_oauth_batch",
                platform="chatgpt",
                status="interrupted",
            )
        )
        session.commit()

    result = KakaoPipelineService().advance_background(
        account_id,
        expected_state="codex_post_action",
    )

    assert result["state"] == "completed"
    assert result["post_actions"]["codex"]["status"] == "pending"
    new_task_id = result["post_actions"]["codex"]["task_id"]
    assert new_task_id != old_task_id
    assert new_task_id.endswith("_2")
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert pipeline.codex_interrupted_retry_count == 1
        assert session.get(TaskModel, new_task_id).status == "pending"


def test_reconciler_does_not_auto_retry_started_interrupted_codex():
    account_id = _create_account("started-interrupted-codex@test.com")
    task_id = "started-interrupted-codex"
    started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=started_at,
                codex_post_action_armed=True,
                codex_task_id=task_id,
                codex_attempt_count=1,
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                type="codex_oauth_batch",
                platform="chatgpt",
                status="interrupted",
                started_at=started_at,
            )
        )
        session.commit()

    service = KakaoPipelineService()
    result = service.advance_background(account_id, expected_state="codex_post_action")

    assert result["post_actions"]["codex"]["status"] == "paused"
    assert result["post_actions"]["codex"]["task_id"] == task_id
    assert service.list_background_work() == []


def test_interrupted_codex_does_not_reuse_credentials_from_an_older_attempt(monkeypatch):
    account_id = _create_account("stale-auth-interrupted@test.com")
    now = datetime.now(timezone.utc)
    task_id = "interrupted-with-stale-auth"
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=now - timedelta(minutes=3),
                codex_post_action_armed=True,
                codex_task_id=task_id,
                codex_attempt_count=2,
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                type="codex_oauth_batch",
                platform="chatgpt",
                status="interrupted",
                created_at=now - timedelta(minutes=2),
                started_at=now - timedelta(minutes=1),
            )
        )
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=True,
                last_refresh=now - timedelta(minutes=10),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.get_nvtokens_auto_push_state",
        lambda: {"enabled": True, "reason": ""},
    )
    service = KakaoPipelineService()
    result = service.advance_background(account_id, expected_state="codex_post_action")

    assert result["post_actions"]["codex"]["authorized"] is True
    assert result["post_actions"]["codex"]["status"] == "paused"
    assert result["post_actions"]["push"]["status"] == "waiting"
    assert service.list_background_work() == []


def test_interrupted_codex_recovers_credentials_saved_by_that_attempt(monkeypatch):
    account_id = _create_account("fresh-auth-interrupted@test.com")
    now = datetime.now(timezone.utc)
    task_id = "interrupted-with-fresh-auth"
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=now - timedelta(minutes=3),
                codex_post_action_armed=True,
                codex_task_id=task_id,
                codex_attempt_count=1,
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                type="codex_oauth_batch",
                platform="chatgpt",
                status="interrupted",
                created_at=now - timedelta(minutes=2),
                started_at=now - timedelta(minutes=1),
            )
        )
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=True,
                last_refresh=now,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.get_nvtokens_auto_push_state",
        lambda: {"enabled": False, "reason": "auto_push_disabled"},
    )

    result = KakaoPipelineService().advance_background(
        account_id,
        expected_state="codex_post_action",
    )

    assert result["post_actions"]["codex"]["status"] == "skipped"
    assert result["post_actions"]["push"]["status"] == "skipped"
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert pipeline.codex_skipped_at is not None


def test_succeeded_codex_with_only_old_credentials_stops_as_failed(monkeypatch):
    account_id = _create_account("succeeded-with-stale-auth@test.com")
    now = datetime.now(timezone.utc)
    task_id = "succeeded-with-stale-auth"
    task = TaskModel(
        id=task_id,
        type="codex_oauth_batch",
        platform="chatgpt",
        status="succeeded",
        created_at=now - timedelta(minutes=2),
        started_at=now - timedelta(minutes=1),
    )
    task.set_result({"data": {"accounts": [{"account_id": account_id, "ok": True}]}})
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=now - timedelta(minutes=3),
                codex_post_action_armed=True,
                codex_task_id=task_id,
                codex_attempt_count=1,
            )
        )
        session.add(task)
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=True,
                last_refresh=now - timedelta(minutes=10),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.get_nvtokens_auto_push_state",
        lambda: {"enabled": True, "reason": ""},
    )
    service = KakaoPipelineService()
    result = service.advance_background(account_id, expected_state="codex_post_action")

    assert result["post_actions"]["codex"]["status"] == "failed"
    assert result["post_actions"]["push"]["status"] == "waiting"
    assert service.list_background_work() == []


def test_reconciler_enqueues_linked_push_after_verified_codex(monkeypatch):
    from application.tasks import create_account_push_task

    account_id = _create_account("verified-codex-push@test.com")
    codex_task_id = "verified-codex-task"
    codex_task = TaskModel(
        id=codex_task_id,
        type="codex_oauth_batch",
        platform="chatgpt",
        status="succeeded",
    )
    codex_task.set_result(
        {
            "data": {
                "accounts": [
                    {"account_id": account_id, "ok": True},
                ]
            }
        }
    )
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
                codex_post_action_armed=True,
                codex_task_id=codex_task_id,
                codex_attempt_count=1,
            )
        )
        session.add(codex_task)
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=True,
                last_refresh=datetime.now(timezone.utc),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.get_nvtokens_auto_push_state",
        lambda: {"enabled": True, "reason": ""},
    )

    def enqueue(current_account_id: int, *, platform: str, task_id: str, source: str):
        assert source == "kakao_pipeline"
        task = create_account_push_task(
            platform=platform,
            account_ids=[current_account_id],
            target_key="nvtokens",
            payload_format="codex",
            source=source,
            task_id=task_id,
        )
        return {"enqueued": True, "task_id": task["id"]}

    monkeypatch.setattr(
        "features.kakao_pipeline.service.enqueue_nvtokens_push_after_codex_oauth",
        enqueue,
    )

    result = KakaoPipelineService().advance_background(
        account_id,
        expected_state="codex_post_action",
    )

    push = result["post_actions"]["push"]
    assert result["state"] == "completed"
    assert result["post_actions"]["codex"]["status"] == "success"
    assert push["status"] == "pending"
    assert push["enabled"] is True
    assert push["task_id"].startswith("kakao_push_")
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        push_task = session.get(TaskModel, push["task_id"])
        assert pipeline.state == "completed"
        assert pipeline.codex_push_task_id == push["task_id"]
        assert push_task.get_payload()["target_key"] == "nvtokens"
        assert push_task.get_payload()["source"] == "kakao_pipeline"
