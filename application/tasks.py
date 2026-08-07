"""Task orchestration and persistence helpers."""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from application.account_recovery import (
    AccountReloginResult,
    AccountStateSnapshot,
    check_and_recover_account,
    execute_runtime_action_with_worker_proxy as _execute_shared_runtime_action_with_worker_proxy,
)
from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.base_platform import AccountStatus, RegisterConfig
from core.datetime_utils import format_local_clock, serialize_datetime
from core.db import AccountModel, AccountStatusModel, TaskEventModel, TaskModel, engine, save_account
from core.network_retry import is_retryable_network_error
from core.platform_accounts import build_platform_account
from core.proxy_resolution import (
    PROXY_MODE_DIRECT,
    PROXY_MODE_MANUAL,
    PROXY_MODE_PROXY_SERVICE,
    mask_proxy_url,
    normalize_proxy_mode,
    resolve_proxy_by_mode,
)
from core.registry import get
from infrastructure.platform_runtime import PlatformRuntime, persist_action_failure

TASK_TYPE_REGISTER = "register"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"
TASK_TYPE_PLATFORM_ACTION = "platform_action"
TASK_TYPE_CODEX_OAUTH_BATCH = "codex_oauth_batch"
TASK_TYPE_RELOGIN_BATCH = "relogin_batch"
TASK_TYPE_ACCOUNT_PUSH = "account_push"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"
TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"
TASK_STATUS_CANCELLED = "cancelled"

# A single registration task may run many workers.  Twenty is the UI and
# backend contract; anything larger is deliberately bounded to avoid an
# accidental unbounded thread pool.
MAX_REGISTER_CONCURRENCY = 20
MAX_CODEX_OAUTH_BATCH_CONCURRENCY = 20
MAX_RELOGIN_BATCH_CONCURRENCY = 10

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_CANCELLED,
}
ACTIVE_TASK_STATUSES = {
    TASK_STATUS_CLAIMED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_CANCEL_REQUESTED,
}
STOP_REQUEST_TASK_STATUSES = TERMINAL_TASK_STATUSES | {TASK_STATUS_CANCEL_REQUESTED}

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()


def _shutdown_task_pool(
    pool: ThreadPoolExecutor,
    *,
    cancel_futures: bool = False,
) -> None:
    """Keep the parent task alive until every started child worker exits.

    The runtime accounts concurrency and account locks by the parent task
    thread.  ``wait=False`` would let that thread disappear while already
    running futures continue in the background, prematurely releasing both
    protections after a cancellation request.
    """
    pool.shutdown(wait=True, cancel_futures=cancel_futures)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _serialize_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _task_lock(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _task_locks[task_id] = lock
        return lock


def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:
    with _task_lock(task_id):
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            if not task:
                return None
            fn(task)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task


def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"errors": [], "cashier_urls": [], "data": None}
    if result:
        base.update(result)
    return base


def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    if task_type == TASK_TYPE_PLATFORM_ACTION:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    if task_type in {
        TASK_TYPE_ACCOUNT_CHECK_ALL,
        TASK_TYPE_CODEX_OAUTH_BATCH,
        TASK_TYPE_RELOGIN_BATCH,
    }:
        return [
            f"account:{int(item)}"
            for item in payload.get("account_ids", [])
            if int(item or 0) > 0
        ]
    if task_type == TASK_TYPE_ACCOUNT_PUSH:
        # OAuth-created pushes are only enqueued after that account's Codex
        # credentials have been committed.  The enclosing batch task keeps
        # every selected account key busy until the whole batch finishes, so
        # retaining the key here would unnecessarily delay completed accounts.
        if str(payload.get("source") or "") == "codex_oauth":
            return []
        return [
            f"account:{int(item)}"
            for item in payload.get("account_ids", [])
            if int(item or 0) > 0
        ]
    return []


def _is_workflow_child_task(payload: dict[str, Any]) -> bool:
    return str(payload.get("source") or "").strip().lower() == "workflow"


def _workflow_child_scope(task_type: str, platform: str, payload: dict[str, Any], *, task_id: str = "") -> str:
    run_id = str(payload.get("workflow_run_id") or "").strip()
    step_id = str(payload.get("workflow_step_id") or "").strip()
    idempotency_key = str(payload.get("workflow_idempotency_key") or "").strip()
    scope_key = ":".join(item for item in (run_id, step_id, idempotency_key, task_id) if item)
    return f"{platform}:{task_type}:workflow:{scope_key or 'child'}"


def _task_scope(task_type: str, platform: str, payload: dict[str, Any], *, task_id: str = "") -> str:
    if task_type == TASK_TYPE_CODEX_OAUTH_BATCH:
        account_ids = sorted(
            {
                int(item)
                for item in payload.get("account_ids", [])
                if int(item or 0) > 0
            }
        )
        if len(account_ids) == 1:
            return f"{platform}:{task_type}:account:{account_ids[0]}"
        if account_ids:
            # One scope cannot represent several independent accounts.  Give
            # a multi-account batch its own scope; account keys below still
            # prevent any overlapping account from running concurrently.
            return f"{platform}:{task_type}:batch:{task_id or 'pending'}"
    if _is_workflow_child_task(payload):
        return _workflow_child_scope(task_type, platform, payload, task_id=task_id)
    if task_type == TASK_TYPE_PLATFORM_ACTION and str(payload.get("action_id") or "") == "codex_oauth_authorize":
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return f"{platform}:{task_type}:codex_oauth_authorize:{account_id}"
    if task_type == TASK_TYPE_ACCOUNT_PUSH and str(payload.get("source") or "") == "codex_oauth":
        account_ids = [
            int(item)
            for item in payload.get("account_ids", [])
            if int(item or 0) > 0
        ]
        if len(account_ids) == 1:
            target_key = str(payload.get("target_key") or "default")
            return f"{platform}:{task_type}:{target_key}:{account_ids[0]}"
    return f"{platform}:{task_type}"


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING, TASK_STATUS_CANCEL_REQUESTED},
        "progress": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        "progress_detail": {
            "current": progress_current,
            "total": progress_total,
            "label": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        },
        "success": int(task.success_count or 0),
        "error_count": int(task.error_count or 0),
        "errors": list(result.get("errors", [])),
        "cashier_urls": list(result.get("cashier_urls", [])),
        "data": result.get("data"),
        "result": result,
        "error": task.error,
        "created_at": _serialize_datetime(task.created_at),
        "started_at": _serialize_datetime(task.started_at),
        "finished_at": _serialize_datetime(task.finished_at),
        "updated_at": _serialize_datetime(task.updated_at),
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
        "detail": event.get_detail(),
        "created_at": _serialize_datetime(event.created_at),
    }


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
    task_id: str = "",
) -> dict[str, Any]:
    task_id = str(task_id or f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}")
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_PENDING,
        payload_json=_dump_json(payload),
        result_json=_dump_json(_task_result_seed(result_seed)),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
    )
    with Session(engine) as session:
        existing = session.get(TaskModel, task_id)
        if existing:
            return serialize_task(existing)
        session.add(task)
        try:
            session.commit()
            session.refresh(task)
        except IntegrityError:
            session.rollback()
            existing = session.get(TaskModel, task_id)
            if existing:
                return serialize_task(existing)
            raise
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def create_register_task(payload: dict[str, Any], *, task_id: str = "") -> dict[str, Any]:
    count = max(int(payload.get("count", 1) or 1), 1)
    payload = {**payload, "platform": "chatgpt"}
    return create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload=payload,
        progress_total=count,
        task_id=task_id,
    )


def create_account_check_all_task(
    platform: str = "",
    limit: int = 50,
    account_ids: list[int] | None = None,
    platform_proxy_mode: str = "",
    platform_proxy_value: str = "",
    concurrency: int = 0,
    request_timeout_seconds: int = 0,
    automatic: bool = False,
    relogin_invalid: bool = False,
    relogin_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_ids = [int(item) for item in account_ids or [] if int(item or 0) > 0]
    payload: dict[str, Any] = {"platform": platform, "limit": int(limit or 50)}
    if platform_proxy_mode:
        payload["platform_proxy_mode"] = platform_proxy_mode
    if platform_proxy_value:
        payload["platform_proxy_value"] = platform_proxy_value
    if concurrency:
        payload["concurrency"] = int(concurrency)
    if request_timeout_seconds:
        payload["request_timeout_seconds"] = int(request_timeout_seconds)
    if automatic:
        payload["automatic"] = True
    if relogin_invalid:
        payload["relogin_invalid"] = True
        payload["relogin_params"] = dict(relogin_params or {})
    progress_total = max(int(limit or 50), 1)
    if account_ids is not None:
        payload["account_ids"] = normalized_ids
        progress_total = len(normalized_ids)
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform=platform,
        payload=payload,
        progress_total=progress_total,
    )


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,
    )


def create_codex_oauth_batch_task(
    *,
    platform: str,
    account_ids: list[int],
    params: dict[str, Any] | None = None,
    concurrency: int = 1,
    auto_push_after_oauth: bool = True,
    task_id: str = "",
    source: str = "manual",
    workflow_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_ids = [int(item) for item in account_ids or [] if int(item or 0) > 0]
    source = str(source or "manual")
    normalized_params = dict(params or {})
    if not normalized_params.get("oauth_mode"):
        normalized_params["oauth_mode"] = "browser"
    return create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform=platform or "chatgpt",
        payload={
            "platform": platform or "chatgpt",
            "account_ids": normalized_ids,
            "action_id": "codex_oauth_authorize",
            "params": normalized_params,
            "concurrency": int(concurrency or 1),
            "auto_push_after_oauth": bool(auto_push_after_oauth),
            "source": source,
            **dict(workflow_context or {}),
        },
        progress_total=len(normalized_ids),
        task_id=task_id,
    )


def create_relogin_batch_task(
    *,
    platform: str,
    account_ids: list[int],
    params: dict[str, Any] | None = None,
    concurrency: int = 1,
) -> dict[str, Any]:
    normalized_ids = [int(item) for item in account_ids or [] if int(item or 0) > 0]
    return create_task(
        task_type=TASK_TYPE_RELOGIN_BATCH,
        platform=platform or "chatgpt",
        payload={
            "platform": platform or "chatgpt",
            "account_ids": normalized_ids,
            "action_id": "relogin",
            "params": dict(params or {}),
            "concurrency": int(concurrency or 1),
        },
        progress_total=len(normalized_ids),
    )


def create_account_push_task(
    *,
    platform: str,
    account_ids: list[int],
    target_key: str,
    payload_format: str,
    source: str = "manual",
    task_id: str = "",
    workflow_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_ids = [int(item) for item in account_ids or [] if int(item or 0) > 0]
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_PUSH,
        platform=platform,
        payload={
            "platform": platform,
            "account_ids": normalized_ids,
            "target_key": str(target_key or ""),
            "payload_format": str(payload_format or ""),
            "source": str(source or "manual"),
            **dict(workflow_context or {}),
        },
        progress_total=len(normalized_ids),
        task_id=task_id,
    )


def enqueue_nvtokens_push_after_codex_oauth(
    account_id: int,
    *,
    platform: str = "chatgpt",
    task_id: str = "",
    source: str = "codex_oauth",
) -> dict[str, Any]:
    """Best-effort enqueue for the optional OAuth post-action.

    This helper deliberately absorbs configuration and persistence failures so
    a completed OAuth flow can never be changed into a failed authorization.
    """
    try:
        normalized_account_id = int(account_id or 0)
        if normalized_account_id <= 0:
            return {"enqueued": False, "reason": "invalid_account"}

        state = get_nvtokens_auto_push_state()
        if not state.get("enabled"):
            return {"enqueued": False, "reason": str(state.get("reason") or "target_disabled")}

        task = create_account_push_task(
            platform=platform or "chatgpt",
            account_ids=[normalized_account_id],
            target_key="nvtokens",
            payload_format="codex",
            source=str(source or "codex_oauth"),
            task_id=str(task_id or ""),
        )
        return {"enqueued": True, "task_id": task["id"]}
    except Exception as exc:  # noqa: BLE001 - OAuth success must remain isolated.
        return {"enqueued": False, "reason": "enqueue_failed", "error": str(exc)}


def enqueue_cpa_push_after_codex_oauth(
    account_id: int,
    *,
    platform: str = "chatgpt",
    task_id: str = "",
    source: str = "codex_oauth",
) -> dict[str, Any]:
    """Best-effort enqueue for the optional CLIProxyAPI post-action."""
    try:
        normalized_account_id = int(account_id or 0)
        if normalized_account_id <= 0:
            return {"enqueued": False, "reason": "invalid_account"}

        state = get_cpa_auto_push_state()
        if not state.get("enabled"):
            return {"enqueued": False, "reason": str(state.get("reason") or "target_disabled")}

        task = create_account_push_task(
            platform=platform or "chatgpt",
            account_ids=[normalized_account_id],
            target_key="cpa",
            payload_format="codex",
            source=str(source or "codex_oauth"),
            task_id=str(task_id or ""),
        )
        return {"enqueued": True, "task_id": task["id"]}
    except Exception as exc:  # noqa: BLE001 - OAuth success must remain isolated.
        return {"enqueued": False, "reason": "enqueue_failed", "error": str(exc)}


def get_cpa_auto_push_state() -> dict[str, Any]:
    """Return the safe, effective CLIProxyAPI OAuth auto-push setting."""
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository
        from providers.push.cpa import CPAPushProvider

        settings = ProviderSettingsRepository()
        setting = settings.get_by_key("push", "cpa")
        if not setting or not bool(setting.enabled):
            return {"enabled": False, "reason": "target_disabled"}
        runtime = settings.resolve_runtime_settings("push", "cpa")
        auto_push = str(runtime.get("cpa_auto_push_after_codex_oauth") or "").strip().lower()
        if auto_push not in {"1", "true", "yes", "on", "是", "开启", "启用"}:
            return {"enabled": False, "reason": "auto_push_disabled"}
        if CPAPushProvider.from_config(runtime).configuration_error():
            return {"enabled": False, "reason": "target_not_configured"}
        return {"enabled": True, "reason": ""}
    except Exception as exc:  # noqa: BLE001 - status reads must remain safe.
        return {"enabled": False, "reason": "settings_unavailable", "error": str(exc)}


def get_nvtokens_auto_push_state() -> dict[str, Any]:
    """Return the safe, effective NexusVault OAuth auto-push setting."""
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository

        settings = ProviderSettingsRepository()
        setting = settings.get_by_key("push", "nvtokens")
        if not setting or not bool(setting.enabled):
            return {"enabled": False, "reason": "target_disabled"}
        runtime = settings.resolve_runtime_settings("push", "nvtokens")
        auto_push = str(runtime.get("nvtokens_auto_push_after_codex_oauth") or "").strip().lower()
        if auto_push not in {"1", "true", "yes", "on", "是", "开启", "启用"}:
            return {"enabled": False, "reason": "auto_push_disabled"}

        from providers.push.nvtokens import NVTokensPushProvider

        if NVTokensPushProvider.from_config(runtime).configuration_error():
            return {"enabled": False, "reason": "target_not_configured"}
        return {"enabled": True, "reason": ""}
    except Exception as exc:  # noqa: BLE001 - status reads must remain safe.
        return {"enabled": False, "reason": "settings_unavailable", "error": str(exc)}


def _log_codex_auto_push_enqueue(
    account_id: int,
    logger: "TaskLogger",
    *,
    platform: str = "chatgpt",
    task_id: str = "",
) -> dict[str, Any]:
    outcome = enqueue_nvtokens_push_after_codex_oauth(
        account_id,
        platform=platform,
        task_id=task_id,
    )
    if outcome.get("enqueued"):
        logger.log(f"账号 {account_id}: 已创建 NexusVault 后台推送任务")
    elif outcome.get("error"):
        logger.log(
            f"账号 {account_id}: NexusVault 自动推送任务创建失败，不影响 Codex OAuth: {outcome['error']}",
            level="warning",
        )
    cpa_outcome = enqueue_cpa_push_after_codex_oauth(
        account_id,
        platform=platform,
        task_id="",
    )
    if cpa_outcome.get("enqueued"):
        logger.log(f"账号 {account_id}: 已创建 CPA 后台推送任务")
    elif cpa_outcome.get("error"):
        logger.log(
            f"账号 {account_id}: CPA 自动推送任务创建失败，不影响 Codex OAuth: {cpa_outcome['error']}",
            level="warning",
        )
    return {
        "enqueued": bool(outcome.get("enqueued") or cpa_outcome.get("enqueued")),
        "task_id": str(outcome.get("task_id") or cpa_outcome.get("task_id") or ""),
        "nvtokens": outcome,
        "cpa": cpa_outcome,
    }


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


def list_tasks(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str = "",
    platform: str = "",
    task_type: str = "",
) -> dict[str, Any]:
    limit = min(max(int(limit or 50), 1), 100)
    offset = max(int(offset or 0), 0)
    status = str(status or "").strip()
    platform = str(platform or "").strip()
    task_type = str(task_type or "").strip()

    conditions = []
    if status:
        conditions.append(TaskModel.status == status)
    if platform:
        conditions.append(TaskModel.platform == platform)
    if task_type:
        conditions.append(TaskModel.type == task_type)

    with Session(engine) as session:
        q = select(TaskModel)
        count_q = select(func.count()).select_from(TaskModel)
        for condition in conditions:
            q = q.where(condition)
            count_q = count_q.where(condition)
        items = session.exec(
            q.order_by(TaskModel.created_at.desc(), TaskModel.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        total = int(session.exec(count_q).one() or 0)
        running = int(
            session.exec(
                select(func.count())
                .select_from(TaskModel)
                .where(TaskModel.status.in_(list(ACTIVE_TASK_STATUSES)))
            ).one()
            or 0
        )

    return {
        "items": [serialize_task(item) for item in items],
        "total": total,
        "running": running,
        "limit": limit,
        "offset": offset,
    }


def list_task_events(task_id: str, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    return list_task_events_page(task_id, since=since, limit=limit)["items"]


def list_task_events_page(
    task_id: str,
    *,
    since: int = 0,
    before: int = 0,
    limit: int = 200,
    latest: bool = False,
) -> dict[str, Any]:
    limit = min(max(limit, 1), 1000)
    since = max(int(since or 0), 0)
    before = max(int(before or 0), 0)
    with Session(engine) as session:
        if before:
            q = (
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .where(TaskEventModel.id < before)
                .order_by(TaskEventModel.id.desc())
                .limit(limit + 1)
            )
            raw_items = session.exec(q).all()
            has_more_before = len(raw_items) > limit
            items = list(reversed(raw_items[:limit]))
        elif latest:
            q = (
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .order_by(TaskEventModel.id.desc())
                .limit(limit + 1)
            )
            raw_items = session.exec(q).all()
            has_more_before = len(raw_items) > limit
            items = list(reversed(raw_items[:limit]))
        else:
            q = (
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .where(TaskEventModel.id > since)
                .order_by(TaskEventModel.id)
                .limit(limit)
            )
            items = session.exec(q).all()
            has_more_before = False
        serialized = [serialize_event(item) for item in items]
    return {
        "items": serialized,
        "cursor": max([int(item["id"] or 0) for item in serialized] or [since]),
        "before": min([int(item["id"] or 0) for item in serialized] or [before]),
        "has_more_before": has_more_before,
    }


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
    return serialize_event(event)


def mark_incomplete_tasks_interrupted() -> None:
    interrupted_ids: list[str] = []
    with Session(engine) as session:
        non_terminal = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)
        tasks = session.exec(
            select(TaskModel).where(TaskModel.status.in_(non_terminal))
        ).all()
        for task in tasks:
            task.status = TASK_STATUS_INTERRUPTED
            task.error = task.error or "任务在服务重启后被中断"
            task.finished_at = _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            interrupted_ids.append(task.id)
        session.commit()
    for task_id in interrupted_ids:
        append_task_event(
            task_id,
            "任务在服务重启后被标记为中断",
            event_type="state",
            level="warning",
        )


def request_cancel(task_id: str) -> Optional[dict[str, Any]]:
    task = _mutate_task(
        task_id,
        lambda model: _request_cancel_mutation(model),
    )
    if not task:
        return None
    append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
    return serialize_task(task)


def _request_cancel_mutation(task: TaskModel) -> None:
    if task.status in TERMINAL_TASK_STATUSES:
        return
    if task.status == TASK_STATUS_PENDING:
        task.status = TASK_STATUS_CANCELLED
        task.finished_at = _utcnow()
        task.error = task.error or "任务在开始前被取消"
    else:
        task.status = TASK_STATUS_CANCEL_REQUESTED


def claim_next_runnable_task(
    *,
    running_type_counts: dict[str, int] | None = None,
    running_scope_counts: dict[str, int] | None = None,
    busy_account_keys: set[str] | None = None,
    max_parallel_per_type: int = 10,
    max_parallel_per_scope: int = 10,
) -> Optional[dict[str, Any]]:
    running_type_counts = dict(running_type_counts or {})
    running_scope_counts = dict(running_scope_counts or {})
    busy_account_keys = set(busy_account_keys or set())
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel)
            .where(TaskModel.status == TASK_STATUS_PENDING)
            .order_by(TaskModel.created_at)
        ).all()
        for task in tasks:
            payload = task.get_payload()
            task_type = str(task.type or "")
            platform = task.platform or str(payload.get("platform", "") or "")
            account_keys = _task_account_keys(task_type, payload)
            scope = _task_scope(task_type, platform, payload, task_id=task.id)
            if running_type_counts.get(task_type, 0) >= max_parallel_per_type:
                continue
            if scope and running_scope_counts.get(scope, 0) >= max_parallel_per_scope:
                continue
            if account_keys and busy_account_keys.intersection(account_keys):
                continue
            task.status = TASK_STATUS_CLAIMED
            task.started_at = task.started_at or _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            return {"id": task.id, "platform": platform, "type": task_type, "scope": scope, "account_keys": account_keys}
    return None


class TaskLogger:
    def __init__(self, task_id: str):
        self.task_id = task_id
        # 并发任务里每个 worker 通过 ``set_subtask`` 把自己的 subtask_id
        # 绑到 thread-local，之后 ``log()`` 自动把 ``subtask_id`` 注入
        # 事件 detail，前端按这个分组折叠展示。
        self._tlocal = threading.local()

    def set_subtask(self, subtask_id: str, label: str = "") -> None:
        """绑定当前线程的子任务标签。子任务结束后调 ``clear_subtask`` 解绑。

        ``subtask_id`` 是稳定标识（如 ``worker_1``）；``label`` 是给前端
        展示的人类可读标题（如"账号 #1"）。
        """
        self._tlocal.subtask_id = str(subtask_id or "")
        self._tlocal.subtask_label = str(label or "")

    def clear_subtask(self) -> None:
        try:
            del self._tlocal.subtask_id
        except AttributeError:
            pass
        try:
            del self._tlocal.subtask_label
        except AttributeError:
            pass

    def _current_subtask(self) -> tuple[str, str]:
        sid = getattr(self._tlocal, "subtask_id", "") or ""
        label = getattr(self._tlocal, "subtask_label", "") or ""
        return sid, label

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        if event_type != "state" and self.is_cancel_requested():
            return
        # 自动给当前线程绑定的 subtask 加 detail，用于前端按 worker 分组折叠
        merged_detail = dict(detail or {})
        sid, slabel = self._current_subtask()
        if sid and "subtask_id" not in merged_detail:
            merged_detail["subtask_id"] = sid
        if slabel and "subtask_label" not in merged_detail:
            merged_detail["subtask_label"] = slabel
        append_task_event(
            self.task_id,
            message,
            event_type=event_type,
            level=level,
            detail=merged_detail or None,
        )
        prefix = f"[task:{self.task_id}]"
        if sid:
            prefix += f"[{sid}]"
        print(f"{prefix} {message}")

    def mark_running(self) -> bool:
        started = {"ok": False}

        def _update(task: TaskModel) -> None:
            if task.status in STOP_REQUEST_TASK_STATUSES:
                return
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()
            started["ok"] = True

        _mutate_task(self.task_id, _update)
        if started["ok"]:
            self.log("任务已开始执行", event_type="state")
        return started["ok"]

    def is_cancel_requested(self) -> bool:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return bool((not task) or task.status in STOP_REQUEST_TASK_STATUSES)

    def _status(self) -> str:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return str(task.status or "") if task else ""

    def _is_terminal(self) -> bool:
        return self._status() in TERMINAL_TASK_STATUSES

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        if self.is_cancel_requested():
            return
        current = max(int(current), 0)

        def _update(task: TaskModel) -> None:
            task.progress_current = current
            if total is not None:
                task.progress_total = max(int(total), 0)

        _mutate_task(self.task_id, _update)

    def record_success(self) -> None:
        if self.is_cancel_requested():
            return
        def _update(task: TaskModel) -> None:
            task.success_count += 1

        _mutate_task(self.task_id, _update)

    def record_error(self, error: str) -> None:
        if self.is_cancel_requested():
            return
        def _update(task: TaskModel) -> None:
            task.error_count += 1
            result = task.get_result()
            errors = list(result.get("errors", []))
            errors.append(error)
            result["errors"] = errors
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def add_cashier_url(self, url: str) -> None:
        if self.is_cancel_requested():
            return
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            urls = list(result.get("cashier_urls", []))
            urls.append(url)
            result["cashier_urls"] = urls
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_result_data(self, data: Any) -> None:
        if self.is_cancel_requested():
            return
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        outcome: dict[str, Any] = {
            "applied": False,
            "status": status,
            "error": error,
        }

        def _update(task: TaskModel) -> None:
            current_status = str(task.status or "")
            if current_status in TERMINAL_TASK_STATUSES and current_status != status:
                return

            final_status = status
            final_error = error
            if current_status == TASK_STATUS_CANCEL_REQUESTED:
                final_status = TASK_STATUS_CANCELLED
                if status != TASK_STATUS_CANCELLED or not final_error:
                    final_error = str(task.error or "任务已取消")

            task.status = final_status
            task.finished_at = _utcnow()
            if final_error:
                task.error = final_error
            outcome.update(
                applied=True,
                status=final_status,
                error=final_error,
            )

        _mutate_task(self.task_id, _update)
        if not outcome["applied"]:
            return

        final_status = str(outcome["status"])
        final_error = str(outcome["error"] or "")
        event_level = "error" if final_status == TASK_STATUS_FAILED else ("warning" if final_status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {final_status}",
            level=event_level,
            event_type="state",
            detail={"status": final_status, "error": final_error},
        )


def _build_platform_instance(
    platform_name: str,
    payload: dict[str, Any],
    logger: TaskLogger,
    platform_proxy: str | None = None,
    mailbox_proxy: str | None = None,
    shared_mailbox=None,
    task_id: str = "",
    subtask_id: str = "",
):
    from core.base_identity import normalize_identity_provider
    from core.base_mailbox import create_mailbox

    executor_type = str(payload.get("executor_type", "browser") or "browser")
    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")
    extra = dict(payload.get("extra") or {})
    extra["_log_fn"] = logger.log
    extra["mailbox_proxy"] = mailbox_proxy or ""
    extra["_task_id"] = str(task_id or "")
    extra["_subtask_id"] = str(subtask_id or "")
    extra["_registration_attempt_id"] = (
        f"{task_id}:{subtask_id}" if task_id and subtask_id else ""
    )
    config = RegisterConfig(
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=platform_proxy,
        extra=extra,
    )
    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
    mailbox = shared_mailbox
    if mailbox is None and identity_provider == "mailbox":
        if not extra.get("mail_provider"):
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=mailbox_proxy,
        )

    platform_cls = get(platform_name)
    platform = platform_cls(config=config, mailbox=mailbox)
    if hasattr(platform, "set_logger"):
        platform.set_logger(logger.log)
    else:
        platform._log_fn = logger.log
    if hasattr(platform, "set_cancel_checker"):
        platform.set_cancel_checker(logger.is_cancel_requested)
    elif hasattr(platform, "_cancel_check_fn"):
        platform._cancel_check_fn = logger.is_cancel_requested
    return platform


class _FixedMailbox:
    def __init__(self, mailbox, mailbox_account):
        self._mailbox = mailbox
        self._mailbox_account = mailbox_account

    def get_email(self):
        return self._mailbox_account

    def set_cancel_checker(self, checker):
        if hasattr(self._mailbox, "set_cancel_checker"):
            self._mailbox.set_cancel_checker(checker)

    def get_current_ids(self, account):
        return self._mailbox.get_current_ids(account)

    def wait_for_code(self, account, **kwargs):
        return self._mailbox.wait_for_code(account, **kwargs)

    def wait_for_link(self, account, **kwargs):
        return self._mailbox.wait_for_link(account, **kwargs)


def _run_single_account_check(
    account_id: int,
    logger: TaskLogger | None = None,
    *,
    proxy: str | None = None,
    disable_proxy_pool: bool = False,
    strict_proxy: bool = False,
    request_timeout_seconds: int = 20,
    track_invalid_attempt: bool = False,
) -> tuple[bool, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        plugin = get(model.platform)(
            config=RegisterConfig(
                proxy=proxy,
                extra={
                    "disable_proxy_pool": bool(disable_proxy_pool),
                    "strict_proxy": bool(strict_proxy),
                    "request_timeout_seconds": max(int(request_timeout_seconds or 20), 5),
                    "raise_check_errors": True,
                },
            )
        )
        account = build_platform_account(session, model)

    valid = plugin.check_valid(account)
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            summary_updates = {
                "checked_at": _utcnow_iso(),
                "valid": bool(valid),
                "_track_invalid_attempt": bool(track_invalid_attempt),
            }
            if hasattr(plugin, "get_last_check_overview"):
                summary_updates.update(plugin.get_last_check_overview() or {})
            lifecycle_status = None
            if valid:
                # **bug 修复**：原实现 ``recover_lifecycle_status_for_valid_account``
                # 直接读 ``current_graph`` 老快照——但 plugin 刚拉到的新
                # ``plan_state`` 在 ``summary_updates`` 里、还没写回 graph，
                # 导致 free → 重新刷新仍然被认成 subscribed。这里把
                # ``summary_updates`` merge 到 graph 里再算 lifecycle。
                merged_graph = dict(current_graph)
                merged_overview = dict(merged_graph.get("overview") or {})
                merged_overview.update(summary_updates)
                merged_graph["overview"] = merged_overview
                lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)
            patch_account_graph(
                session,
                model,
                lifecycle_status=lifecycle_status,
                summary_updates=summary_updates,
            )
            session.add(model)
            session.commit()

    result = {"account_id": account_id, "valid": bool(valid), "platform": account.platform, "email": account.email}
    if logger:
        logger.log(f"{account.email}: {'有效' if valid else '失效'}")
    return valid, result


def _refresh_account_after_codex_oauth(
    account_id: int,
    logger: TaskLogger,
    *,
    params: dict[str, Any] | None = None,
    scope_id: str = "",
    event_label: str = "Codex OAuth",
) -> dict[str, Any]:
    """Refresh account usage after OAuth without changing OAuth success."""
    from core.worker_proxy import WorkerProxyPolicy, worker_proxy_manager

    if logger.is_cancel_requested():
        return {"ok": False, "cancelled": True}

    refresh_params = dict(params or {})
    proxy_mode = normalize_proxy_mode(
        str(refresh_params.get("platform_proxy_mode") or "").strip(),
        default=PROXY_MODE_DIRECT,
    )
    manual_proxy = str(refresh_params.get("platform_proxy_value") or "").strip() or None
    policy = WorkerProxyPolicy.load()
    attempts = policy.replace_max_attempts if proxy_mode == PROXY_MODE_PROXY_SERVICE else 1

    logger.log(f"{event_label} 完成，正在刷新账号状态与额度")
    for proxy_attempt in range(1, attempts + 1):
        lease = None
        account_proxy = manual_proxy if proxy_mode == PROXY_MODE_MANUAL else None
        try:
            if proxy_mode == PROXY_MODE_PROXY_SERVICE:
                lease = worker_proxy_manager.acquire(
                    scope_id=scope_id,
                    log_fn=logger.log,
                    cancel_check=logger.is_cancel_requested,
                    policy=policy,
                )
                account_proxy = lease.url
            valid, result = _run_single_account_check(
                account_id,
                proxy=account_proxy,
                disable_proxy_pool=True,
                strict_proxy=proxy_mode != PROXY_MODE_DIRECT,
            )
            if lease is not None:
                lease.report_success()
            logger.log(f"{event_label} 后账号刷新完成: {'有效' if valid else '失效'}")
            return {"ok": True, "valid": bool(valid), **result}
        except Exception as exc:
            if lease is not None and is_retryable_network_error(exc):
                lease.report_failure()
            if (
                proxy_mode == PROXY_MODE_PROXY_SERVICE
                and proxy_attempt < attempts
                and is_retryable_network_error(exc)
            ):
                logger.log(
                    f"{event_label} 后账号刷新代理异常，换 IP 重试 "
                    f"({proxy_attempt + 1}/{attempts}): {exc}",
                    level="warning",
                )
                continue
            logger.log(f"{event_label} 已成功，但额度刷新失败: {exc}", level="warning")
            return {"ok": False, "error": str(exc)}
        finally:
            if lease is not None:
                lease.release()

    return {"ok": False, "error": "额度刷新未执行"}


def _refresh_account_after_relogin(
    account_id: int,
    logger: TaskLogger,
    *,
    params: dict[str, Any] | None = None,
    scope_id: str = "",
) -> dict[str, Any]:
    return _refresh_account_after_codex_oauth(
        account_id,
        logger,
        params=params,
        scope_id=scope_id,
        event_label="重新登录",
    )


def _safe_relogin_result_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    allowed_keys = {
        "message",
        "remote_email",
        "account_id",
        "registration_auth_mode",
        "checked_at",
        "last_login_status",
        "failure_code",
        "failed_at",
    }
    return {key: data.get(key) for key in allowed_keys if data.get(key) not in (None, "")}


def _relogin_failure_code(data: Any, error: Any = "") -> str:
    if isinstance(data, dict):
        code = str(data.get("failure_code") or "").strip()
        if code:
            return code
    text = str(error or "").casefold()
    if "account_deactivated" in text or "deleted or disabled" in text:
        return "account_deactivated"
    return ""


def _relogin_failure_log_message(email: str, prefix: str, error: Any, data: Any = None) -> str:
    text = str(error or "重新登录失败")
    if _relogin_failure_code(data, text) == "account_deactivated":
        return f"{email}: {prefix}: 账号已封号（account_deactivated），已记录为封号状态。原因: {text}"
    return f"{email}: {prefix}: {text}"


def _persist_relogin_action_failure(
    *,
    platform: str,
    account_id: int,
    exc: Exception,
) -> str:
    from platforms.chatgpt.relogin import classify_relogin_failure, utcnow_iso

    failure = classify_relogin_failure(exc)
    persist_action_failure(
        platform=platform,
        account_id=account_id,
        action_id="relogin",
        error=failure.message,
        data={"failure_code": failure.code, "failed_at": utcnow_iso()},
    )
    return failure.message


def _execute_relogin_for_account(
    *,
    platform: str,
    account_id: int,
    email: str,
    params: dict[str, Any],
    logger: TaskLogger,
    scope_id: str,
) -> dict[str, Any]:
    if logger.is_cancel_requested():
        return {"account_id": account_id, "email": email, "cancelled": True}
    try:
        logger.log(f"{email}: 开始重新登录")
        result = _execute_runtime_action_with_worker_proxy(
            platform=platform,
            account_id=account_id,
            action_id="relogin",
            params=params,
            logger=logger,
            scope_id=scope_id,
        )
        if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
            return {"account_id": account_id, "email": email, "cancelled": True}
        if not result.ok:
            error = str(result.error or "重新登录失败")
            data = result.data if isinstance(result.data, dict) else {}
            if not data.get("failure_code"):
                error = _persist_relogin_action_failure(
                    platform=platform,
                    account_id=account_id,
                    exc=RuntimeError(error),
                )
            logger.record_error(error)
            logger.log(_relogin_failure_log_message(email, "重新登录失败", error, data), level="error")
            return {
                "account_id": account_id,
                "email": email,
                "ok": False,
                "error": error,
                "data": _safe_relogin_result_data(data),
            }
        account_refresh = _refresh_account_after_relogin(
            account_id,
            logger,
            params=params,
            scope_id=scope_id,
        )
        logger.record_success()
        logger.log(f"{email}: 重新登录完成")
        return {
            "account_id": account_id,
            "email": email,
            "ok": True,
            "data": _safe_relogin_result_data(result.data),
            "account_refresh": account_refresh,
        }
    except Exception as exc:
        error = _persist_relogin_action_failure(
            platform=platform,
            account_id=account_id,
            exc=exc,
        )
        logger.record_error(error)
        logger.log(_relogin_failure_log_message(email, "重新登录异常", error), level="error")
        return {"account_id": account_id, "email": email, "ok": False, "error": error}


def _execute_configured_account_check_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """Run account checks with persisted defaults and bounded concurrency."""
    from core.account_check_settings import INVALID_CHECK_LIMIT, get_account_check_settings
    from core.worker_proxy import WorkerProxyPolicy, worker_proxy_manager

    settings = get_account_check_settings()
    platform = str(payload.get("platform") or "")
    limit = max(int(payload.get("limit") or settings.batch_limit), 1)
    automatic = bool(payload.get("automatic"))
    concurrency = min(max(int(payload.get("concurrency") or settings.concurrency), 1), 20)
    request_timeout = min(max(int(payload.get("request_timeout_seconds") or settings.request_timeout_seconds), 5), 300)
    account_ids = [int(item) for item in payload.get("account_ids", []) if int(item or 0) > 0]
    relogin_invalid = bool(payload.get("relogin_invalid"))

    with Session(engine) as session:
        q = select(AccountModel)
        if account_ids:
            q = q.where(AccountModel.id.in_(account_ids))
        elif "account_ids" in payload:
            q = q.where(AccountModel.id == -1)
        if platform:
            q = q.where(AccountModel.platform == platform)
        if automatic:
            q = q.join(AccountStatusModel, AccountStatusModel.account_id == AccountModel.id)
            q = q.where(AccountStatusModel.invalid_check_count < INVALID_CHECK_LIMIT)
            q = q.order_by(AccountStatusModel.checked_at.asc(), AccountModel.id.asc())
        else:
            q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q if "account_ids" in payload else q.limit(limit)).all()

    total = len(accounts)
    logger.set_progress(0, total)
    if not total:
        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    proxy_mode = normalize_proxy_mode(
        str(payload.get("platform_proxy_mode") or settings.proxy_mode),
        default=PROXY_MODE_DIRECT,
    )
    manual_proxy = str(payload.get("platform_proxy_value") or payload.get("proxy") or settings.proxy_url or "").strip() or None
    if proxy_mode == PROXY_MODE_MANUAL and not manual_proxy:
        logger.finish(TASK_STATUS_FAILED, error="手动代理模式需要填写代理 URL")
        return
    relogin_params = dict(payload.get("relogin_params") or {})
    if relogin_invalid:
        if not relogin_params.get("browser_mode"):
            relogin_params["browser_mode"] = "headless"
        if not relogin_params.get("keep_browser_open"):
            relogin_params["keep_browser_open"] = "false"
        relogin_params.setdefault("platform_proxy_mode", proxy_mode)
        if manual_proxy:
            relogin_params.setdefault("platform_proxy_value", manual_proxy)

    task_id = str(logger.task_id or "")
    proxy_policy = WorkerProxyPolicy.load()
    relogin_note = "，失效后重登" if relogin_invalid else ""
    logger.log(f"账号有效性检测: {total} 个，并发 {concurrency}，请求超时 {request_timeout}s，代理 {proxy_mode}{relogin_note}")

    def _check_one(model: AccountModel) -> dict[str, Any]:
        email = str(model.email or "")
        if logger.is_cancel_requested():
            return {"cancelled": True, "email": email}
        attempts = proxy_policy.replace_max_attempts if proxy_mode == PROXY_MODE_PROXY_SERVICE else 1
        for attempt in range(1, attempts + 1):
            lease = None
            proxy = manual_proxy if proxy_mode == PROXY_MODE_MANUAL else None
            try:
                if proxy_mode == PROXY_MODE_PROXY_SERVICE:
                    lease = worker_proxy_manager.acquire(scope_id=task_id, cancel_check=logger.is_cancel_requested, policy=proxy_policy)
                    proxy = lease.url
                def check_state() -> AccountStateSnapshot:
                    valid, result = _run_single_account_check(
                        int(model.id or 0), proxy=proxy, disable_proxy_pool=True,
                        strict_proxy=proxy_mode != PROXY_MODE_DIRECT,
                        request_timeout_seconds=request_timeout, track_invalid_attempt=automatic,
                    )
                    return AccountStateSnapshot(ok=True, valid=bool(valid), data=dict(result or {}))

                def relogin_account() -> AccountReloginResult:
                    result = _execute_relogin_for_account(
                        platform=platform or str(model.platform or ""),
                        account_id=int(model.id or 0),
                        email=email,
                        params=relogin_params,
                        logger=logger,
                        scope_id=task_id,
                    )
                    account_refresh = result.get("account_refresh")
                    refreshed = None
                    if isinstance(account_refresh, dict):
                        refreshed_valid = account_refresh.get("valid")
                        refreshed = AccountStateSnapshot(
                            ok=bool(account_refresh.get("ok", True)),
                            valid=refreshed_valid if isinstance(refreshed_valid, bool) else None,
                            data=dict(account_refresh),
                            error=str(account_refresh.get("error") or ""),
                        )
                    return AccountReloginResult(
                        ok=bool(result.get("ok")),
                        data=dict(result.get("data") or {}) if isinstance(result.get("data"), dict) else {},
                        error=str(result.get("error") or ""),
                        refreshed=refreshed,
                    )

                recovery = check_and_recover_account(
                    check_state=check_state,
                    relogin=relogin_account,
                    relogin_invalid=relogin_invalid,
                    log_fn=logger.log,
                    label=f"{email}: ",
                )
                if lease is not None:
                    lease.report_success()
                if recovery.relogin_attempted:
                    if logger.is_cancel_requested() or recovery.relogin_error == "任务已取消":
                        return {"cancelled": True, "email": email}
                    final_valid = (
                        recovery.final.valid
                        if isinstance(recovery.final.valid, bool)
                        else recovery.relogin_ok
                    )
                    return {
                        "ok": True,
                        **recovery.initial.data,
                        "valid": bool(final_valid),
                        "relogin_attempted": True,
                        "relogin_ok": recovery.relogin_ok,
                        "relogin_error": recovery.relogin_error,
                    }
                return {"ok": True, "valid": bool(recovery.final.valid), **recovery.final.data}
            except Exception as exc:
                retryable = is_retryable_network_error(exc)
                if lease is not None and retryable:
                    lease.report_failure()
                if proxy_mode == PROXY_MODE_PROXY_SERVICE and attempt < attempts and retryable:
                    continue
                return {"ok": False, "email": email, "error": str(exc)}
            finally:
                if lease is not None:
                    lease.release()
        return {"ok": False, "email": email, "error": "账号检测未执行"}

    results = {"valid": 0, "invalid": 0, "error": 0, "relogin_success": 0, "relogin_failed": 0}
    completed = 0
    cancelled = False
    pool = ThreadPoolExecutor(max_workers=min(concurrency, total))
    pending = {pool.submit(_check_one, model) for model in accounts}
    try:
        while pending:
            if logger.is_cancel_requested():
                cancelled = True
                for future in pending:
                    future.cancel()
                break
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                outcome = future.result()
                if outcome.get("cancelled"):
                    cancelled = True
                    continue
                email = str(outcome.get("email") or "")
                if not outcome.get("ok"):
                    results["error"] += 1
                    error = str(outcome.get("error") or "unknown")
                    logger.record_error(error)
                    logger.log(f"{email}: 检测异常: {error}", level="error")
                elif outcome.get("valid"):
                    results["valid"] += 1
                    if outcome.get("relogin_attempted"):
                        results["relogin_success"] += 1
                        logger.log(f"{email}: 失效后重登成功，当前有效")
                    else:
                        logger.log(f"{email}: 有效")
                else:
                    results["invalid"] += 1
                    if outcome.get("relogin_attempted"):
                        if outcome.get("relogin_ok"):
                            results["relogin_success"] += 1
                            logger.log(f"{email}: 失效后重登完成，但刷新后仍为失效", level="warning")
                        else:
                            results["relogin_failed"] += 1
                            error = str(outcome.get("relogin_error") or "重新登录失败")
                            logger.log(_relogin_failure_log_message(email, "失效后重登失败", error), level="error")
                    else:
                        logger.log(f"{email}: 失效", level="warning")
                completed += 1
                logger.set_progress(completed, total)
    finally:
        _shutdown_task_pool(pool, cancel_futures=cancelled)
        worker_proxy_manager.clear_scope(task_id)

    logger.set_result_data(results)
    logger.finish(TASK_STATUS_CANCELLED if cancelled else TASK_STATUS_SUCCEEDED, error="任务已取消" if cancelled else "")


def execute_task(task_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        task_type = task.type
        payload = task.get_payload()

    logger = TaskLogger(task_id)
    if not logger.mark_running():
        if logger._status() == TASK_STATUS_CANCEL_REQUESTED:
            logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        return

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        return

    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_configured_account_check_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
        TASK_TYPE_CODEX_OAUTH_BATCH: _execute_codex_oauth_batch_task,
        TASK_TYPE_RELOGIN_BATCH: _execute_relogin_batch_task,
        TASK_TYPE_ACCOUNT_PUSH: _execute_account_push_task,
    }
    handler = handlers.get(task_type)
    if not handler:
        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
        return
    handler(payload, logger)


def _resolve_registration_proxy_for_platform(
    platform_name: str,
    *,
    explicit_proxy: str | None,
    proxy_getter: Callable[[], str | None],
) -> str | None:
    normalized_explicit_proxy = str(explicit_proxy or "").strip() or None
    if str(platform_name or "").strip().lower() == "chatgpt":
        # ChatGPT 只使用本次任务显式传入的动态 IP；留空时固定本地直连，
        # 不从全局代理池回退。
        return normalized_explicit_proxy
    return normalized_explicit_proxy or proxy_getter()


def _registration_platform_proxy(payload: dict[str, Any], proxy_getter: Callable[[], str | None]) -> tuple[str | None, str]:
    explicit_proxy = str(payload.get("proxy") or "").strip() or None
    mode = normalize_proxy_mode(
        str(payload.get("platform_proxy_mode") or "").strip(),
        default=PROXY_MODE_MANUAL if explicit_proxy else PROXY_MODE_DIRECT,
    )
    # Proxy-service mode is resolved inside each worker so concurrent accounts
    # never inherit one task-level IP.
    proxy = None if mode == PROXY_MODE_PROXY_SERVICE else resolve_proxy_by_mode(
        mode,
        manual_proxy=str(payload.get("platform_proxy_value") or "").strip() or explicit_proxy,
        proxy_getter=proxy_getter,
    )
    return proxy, mode


def _registration_mailbox_proxy() -> tuple[str | None, str]:
    """Mailbox/provider APIs always use the local direct connection."""
    return None, PROXY_MODE_DIRECT


def _registration_concurrency(requested: Any, count: int) -> int:
    return min(
        max(int(requested or 1), 1),
        max(int(count or 1), 1),
        MAX_REGISTER_CONCURRENCY,
    )


def _action_batch_concurrency(requested: Any, count: int, maximum: int) -> int:
    try:
        value = int(requested or 1)
    except Exception:
        value = 1
    return min(
        max(value, 1),
        max(int(count or 1), 1),
        maximum,
    )


def _codex_oauth_batch_concurrency(requested: Any, count: int) -> int:
    return _action_batch_concurrency(requested, count, MAX_CODEX_OAUTH_BATCH_CONCURRENCY)


def _relogin_batch_concurrency(requested: Any, count: int) -> int:
    return _action_batch_concurrency(requested, count, MAX_RELOGIN_BATCH_CONCURRENCY)


def _bounded_int(value: Any, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except Exception:
        result = default
    result = max(result, minimum)
    if maximum is not None:
        result = min(result, maximum)
    return result


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.proxy_pool import proxy_pool
    from core.worker_proxy import WorkerProxyPolicy, worker_proxy_manager

    count = max(int(payload.get("count", 1) or 1), 1)
    concurrency = _registration_concurrency(payload.get("concurrency", 1), count)
    platform_name = "chatgpt"
    email = payload.get("email") or None
    password = payload.get("password") or None
    extra = dict(payload.get("extra") or {})
    extra["_log_fn"] = logger.log
    task_id = str(getattr(logger, "task_id", "") or "")
    task_platform_proxy, platform_proxy_mode = _registration_platform_proxy(payload, proxy_pool.get_next)
    proxy_policy = WorkerProxyPolicy.load()
    mailbox_proxy, _ = _registration_mailbox_proxy()
    extra["mailbox_proxy"] = mailbox_proxy or ""
    payload["extra"] = extra

    logger.set_progress(0, count)
    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    shared_mailbox = None
    try:
        from core.base_identity import normalize_identity_provider
        from core.base_mailbox import create_mailbox

        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
        if identity_provider == "mailbox":
            fixed_mailbox_address_id = str(extra.get("mailbox_address_id") or "").strip()
            if fixed_mailbox_address_id:
                from core.mailbox_store import MailboxStore

                mailbox, mailbox_account, mailbox_context = MailboxStore().resolve_mailbox_for_address(
                    mailbox_address_id=fixed_mailbox_address_id,
                    proxy=mailbox_proxy,
                    extra=extra,
                )
                provider = str(((mailbox_context.get("account") or {}).get("provider")) or "").strip()
                if provider:
                    extra["mail_provider"] = provider
                    payload["extra"] = extra
                fixed_email = str(getattr(mailbox_account, "email", "") or "").strip()
                if fixed_email:
                    payload["email"] = fixed_email
                    email = fixed_email
                shared_mailbox = _FixedMailbox(mailbox, mailbox_account)
                logger.log(f"使用选中邮箱注册: {fixed_email or fixed_mailbox_address_id}")
            else:
                if not extra.get("mail_provider"):
                    from infrastructure.provider_settings_repository import ProviderSettingsRepository

                    extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
                shared_mailbox = create_mailbox(
                    provider=extra.get("mail_provider", ""),
                    extra=extra,
                    proxy=mailbox_proxy,
                )
                if hasattr(shared_mailbox, "set_cancel_checker"):
                    shared_mailbox.set_cancel_checker(logger.is_cancel_requested)
                if (
                    count > 1
                    and str(extra.get("mail_provider") or "").strip() == "hotmail007"
                    and hasattr(shared_mailbox, "configure_prefetch")
                ):
                    buy_concurrency = _bounded_int(
                        extra.get("hotmail007_buy_concurrency"),
                        concurrency,
                        minimum=1,
                        maximum=concurrency,
                    )
                    queue_max = _bounded_int(
                        extra.get("hotmail007_prefetch_queue_max"),
                        concurrency * 2,
                        minimum=1,
                        maximum=count,
                    )
                    shared_mailbox.configure_prefetch(
                        total_needed=count,
                        buy_concurrency=buy_concurrency,
                        queue_max=queue_max,
                    )
    except Exception as exc:
        logger.log(f"邮箱初始化失败: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"邮箱初始化失败: {exc}")
        return

    def _do_one(index: int) -> dict[str, Any] | str:
        if logger.is_cancel_requested():
            return "__cancel_requested__"
        subtask_id = f"worker_{index + 1}"
        logger.set_subtask(subtask_id, f"Worker {index + 1}")
        platform = None
        platform_proxy = task_platform_proxy
        proxy_lease = None
        allocation_id = ""
        allocation_succeeded = False
        failure_reason = ""
        existing_account_failure = False
        try:
            logger.log(f"开始注册第 {index + 1}/{count} 个账号")
            from core.mailbox_lifecycle import MailboxAllocationLifecycle

            attempts = proxy_policy.replace_max_attempts if platform_proxy_mode == PROXY_MODE_PROXY_SERVICE else 1
            account = None
            for proxy_attempt in range(1, attempts + 1):
                if logger.is_cancel_requested():
                    return "__cancel_requested__"
                if platform_proxy_mode == PROXY_MODE_PROXY_SERVICE:
                    proxy_lease = worker_proxy_manager.acquire(
                        scope_id=task_id,
                        log_fn=logger.log,
                        cancel_check=logger.is_cancel_requested,
                        policy=proxy_policy,
                    )
                    platform_proxy = proxy_lease.url
                platform = _build_platform_instance(
                    platform_name,
                    payload,
                    logger,
                    platform_proxy=platform_proxy,
                    mailbox_proxy=mailbox_proxy,
                    shared_mailbox=shared_mailbox,
                    task_id=task_id,
                    subtask_id=subtask_id,
                )
                logger.log(
                    f"ChatGPT/Codex 代理: {mask_proxy_url(platform_proxy) if platform_proxy else '直连'}"
                    f"（{platform_proxy_mode}）"
                )
                try:
                    account = platform.register(email=email, password=password)
                    allocation_id = MailboxAllocationLifecycle.allocation_id_from_account(account)
                    break
                except Exception as exc:
                    allocation_id = MailboxAllocationLifecycle.allocation_id_from_platform(platform)
                    retry_proxy = (
                        platform_proxy_mode == PROXY_MODE_PROXY_SERVICE
                        and proxy_attempt < attempts
                        and is_retryable_network_error(exc)
                    )
                    if not retry_proxy:
                        raise
                    if proxy_lease is not None:
                        proxy_lease.report_failure()
                        proxy_lease.release()
                        proxy_lease = None
                    if allocation_id:
                        MailboxAllocationLifecycle().release(
                            allocation_id,
                            outcome="failed",
                            reason=f"代理异常换 IP 重试: {exc}",
                        )
                        allocation_id = ""
                    logger.log(
                        f"代理网络异常，关闭当前注册实例并换 IP 重试 "
                        f"({proxy_attempt + 1}/{attempts}): {exc}",
                        level="warning",
                    )
                    platform = None
            if account is None:
                raise RuntimeError("注册流程未返回账号")
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            with Session(engine) as registration_session:
                account.extra = {
                    **dict(account.extra or {}),
                    "account_source": "registration",
                    "registration_executor": str(payload.get("executor_type") or ""),
                }
                saved_account = save_account(account, session=registration_session, commit=False)
                saved_account_id = int(saved_account.id)
                if allocation_id:
                    MailboxAllocationLifecycle().succeed_in_session(
                        registration_session,
                        allocation_id,
                        account_id=saved_account_id,
                        account_email=account.email,
                        platform=account.platform,
                    )
                registration_session.commit()
            if allocation_id:
                allocation_succeeded = True
            if proxy_lease is not None:
                proxy_lease.report_success()
            post_codex_oauth = dict((account.extra or {}).get("post_codex_oauth") or {})
            auto_codex_oauth_enabled = str(extra.get("auto_codex_oauth_after_register") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "是",
                "开启",
                "启用",
            }
            if auto_codex_oauth_enabled and not post_codex_oauth:
                if logger.is_cancel_requested():
                    return "__cancel_requested__"
                logger.log(f"{account.email} 注册后执行 Codex OAuth 授权")
                try:
                    setattr(account, "id", saved_account_id)
                except Exception:
                    pass
                browser_mode = str(extra.get("codex_oauth_browser_mode") or "").strip().lower()
                keep_browser_open = str(extra.get("codex_oauth_keep_browser_open") or "").strip().lower()
                browser_visible = str(extra.get("browser_visible") or "").strip().lower() in {
                    "1", "true", "yes", "on", "是", "开启", "启用",
                }
                codex_action = platform.execute_action(
                    "codex_oauth_authorize",
                    account,
                    {
                        "browser_mode": browser_mode or (
                            "headed"
                            if str(payload.get("executor_type") or "") == "headed" or browser_visible
                            else "headless"
                        ),
                        "keep_browser_open": keep_browser_open,
                        "oauth_mode": str(extra.get("codex_oauth_mode") or "browser"),
                    },
                )
                if logger.is_cancel_requested():
                    return "__cancel_requested__"
                if codex_action.get("ok") and isinstance(codex_action.get("data"), dict):
                    account.extra = {**dict(account.extra or {}), **codex_action["data"]}
                    save_account(account)
                    post_codex_oauth = {"ok": True}
                else:
                    post_codex_oauth = {"ok": False, "error": str(codex_action.get("error") or codex_action.get("data") or "unknown")}
            if post_codex_oauth:
                if post_codex_oauth.get("ok"):
                    logger.log(f"{account.email} 的 Codex OAuth 授权已完成")
                    _refresh_account_after_codex_oauth(
                        saved_account_id,
                        logger,
                        params={
                            "platform_proxy_mode": PROXY_MODE_MANUAL if platform_proxy else PROXY_MODE_DIRECT,
                            "platform_proxy_value": platform_proxy or "",
                        },
                        scope_id=str(getattr(logger, "task_id", "") or ""),
                    )
                    _log_codex_auto_push_enqueue(saved_account_id, logger, platform="chatgpt")
                else:
                    logger.log(
                        f"{account.email} 注册成功，但 Codex OAuth 授权失败: {post_codex_oauth.get('error') or 'unknown'}",
                        level="warning",
                    )
            logger.record_success()
            if bool((account.extra or {}).get("existing_account")):
                logger.log(f"已有账号登录成功: {account.email}")
            else:
                logger.log(f"注册成功: {account.email}")
            item = {
                "account_id": saved_account_id,
                "email": account.email,
            }
            if auto_codex_oauth_enabled or post_codex_oauth:
                item["codex_oauth"] = post_codex_oauth or {"ok": False, "skipped": True}
            return item
        except Exception as exc:
            failure_reason = str(exc)
            existing_account_failure = bool(getattr(exc, "preserve_mailbox", False))
            if logger.is_cancel_requested() or str(exc) == "任务已取消":
                return "__cancel_requested__"
            if proxy_lease is not None and is_retryable_network_error(exc):
                proxy_lease.report_failure()
            error = str(exc)
            logger.record_error(error)
            logger.log(f"注册失败: {error}", level="error")
            return error
        finally:
            if platform is not None and not allocation_id:
                from core.mailbox_lifecycle import MailboxAllocationLifecycle

                allocation_id = MailboxAllocationLifecycle.allocation_id_from_platform(platform)
            if allocation_id and not allocation_succeeded:
                from core.mailbox_lifecycle import (
                    ALLOCATION_CANCELLED,
                    ALLOCATION_FAILED,
                    MailboxAllocationLifecycle,
                )

                cancelled = logger.is_cancel_requested() or failure_reason == "任务已取消"
                lifecycle = MailboxAllocationLifecycle()
                if existing_account_failure and not cancelled:
                    lifecycle.mark_existing_account(
                        allocation_id,
                        reason=failure_reason or "检测到已有账号但登录认证未完成",
                    )
                else:
                    lifecycle.release(
                        allocation_id,
                        outcome=ALLOCATION_CANCELLED if cancelled else ALLOCATION_FAILED,
                        reason=failure_reason or ("任务已取消" if cancelled else "注册未成功"),
                    )
            if proxy_lease is not None:
                proxy_lease.release()
            logger.clear_subtask()

    success = 0
    errors: list[str] = []
    registered_accounts: list[dict[str, Any]] = []
    completed = 0
    pool: ThreadPoolExecutor | None = None
    cancel_pool = False
    try:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        pending = set()
        next_index = 0
        while next_index < count and len(pending) < concurrency and not logger.is_cancel_requested():
            pending.add(pool.submit(_do_one, next_index))
            next_index += 1
        while pending:
            if logger.is_cancel_requested():
                cancel_pool = True
                for future in pending:
                    future.cancel()
                logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                return
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                if future.cancelled():
                    continue
                result = future.result()
                completed += 1
                if isinstance(result, dict):
                    success += 1
                    registered_accounts.append(result)
                elif result != "__cancel_requested__":
                    errors.append(str(result))
                logger.set_progress(completed, count)
                if logger.is_cancel_requested():
                    cancel_pool = True
                    for item in pending:
                        item.cancel()
                    logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                    return
            while next_index < count and len(pending) < concurrency and not logger.is_cancel_requested():
                pending.add(pool.submit(_do_one, next_index))
                next_index += 1
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return
    finally:
        if pool is not None:
            _shutdown_task_pool(pool, cancel_futures=cancel_pool)
        if hasattr(shared_mailbox, "shutdown_prefetch"):
            try:
                shared_mailbox.shutdown_prefetch()
            except Exception as exc:
                logger.log(f"邮箱预取停止失败: {exc}", level="warning")
        worker_proxy_manager.clear_scope(task_id)

    result_data = {
        "success": success,
        "fail": len(errors),
        "account_ids": [item["account_id"] for item in registered_accounts],
        "accounts": registered_accounts,
    }
    logger.set_result_data(result_data)
    logger.log(f"完成: 成功 {success} 个, 失败 {len(errors)} 个", event_type="summary")
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    logger.finish(final_status, error=errors[0] if final_status == TASK_STATUS_FAILED else "")


def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    command_platform = str(payload.get("platform", ""))
    account_id = int(payload.get("account_id", 0) or 0)
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    task_id = str(getattr(logger, "task_id", "") or "")
    try:
        result = _execute_runtime_action_with_worker_proxy(
            platform=command_platform,
            account_id=account_id,
            action_id=action_id,
            params=params,
            logger=logger,
            scope_id=task_id,
        )
    except Exception as exc:
        if action_id == "relogin" and str(exc or "") != "任务已取消":
            _persist_relogin_action_failure(
                platform=command_platform,
                account_id=account_id,
                exc=exc,
            )
        logger.record_error(str(exc))
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return
    finally:
        from core.worker_proxy import worker_proxy_manager

        worker_proxy_manager.clear_scope(task_id)
    if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    if not result.ok:
        logger.record_error(result.error)
        logger.finish(TASK_STATUS_FAILED, error=result.error)
        return
    result_data = dict(result.data) if isinstance(result.data, dict) else result.data
    if action_id == "codex_oauth_authorize":
        quota_refresh = _refresh_account_after_codex_oauth(
            account_id,
            logger,
            params=params,
            scope_id=task_id,
        )
        if isinstance(result_data, dict):
            result_data["quota_refresh"] = quota_refresh
        from core.worker_proxy import worker_proxy_manager

        worker_proxy_manager.clear_scope(task_id)
        if bool(payload.get("auto_push_after_oauth", True)):
            _log_codex_auto_push_enqueue(account_id, logger, platform=command_platform)
    elif action_id == "relogin":
        account_refresh = _refresh_account_after_relogin(
            account_id,
            logger,
            params=params,
            scope_id=task_id,
        )
        if isinstance(result_data, dict):
            result_data["account_refresh"] = account_refresh
    logger.set_result_data(result_data)
    message = ""
    if isinstance(result_data, dict):
        message = str(result_data.get("message", "") or "")
    if message:
        logger.log(message, event_type="summary")
    logger.set_progress(1, 1)
    logger.finish(TASK_STATUS_SUCCEEDED)


def _execute_runtime_action_with_worker_proxy(
    *,
    platform: str,
    account_id: int,
    action_id: str,
    params: dict[str, Any],
    logger: TaskLogger,
    scope_id: str,
):
    """Execute one action with a worker-owned, replaceable proxy lease."""
    return _execute_shared_runtime_action_with_worker_proxy(
        platform=platform,
        account_id=account_id,
        action_id=action_id,
        params=params,
        scope_id=scope_id,
        log_fn=logger.log,
        cancel_check=logger.is_cancel_requested,
        runtime_factory=PlatformRuntime,
    )


def _execute_codex_oauth_batch_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform") or "chatgpt")
    account_ids = [
        int(item)
        for item in payload.get("account_ids", [])
        if int(item or 0) > 0
    ]
    params = dict(payload.get("params") or {})
    auto_push_after_oauth = bool(payload.get("auto_push_after_oauth", True))
    if not params.get("oauth_mode"):
        params["oauth_mode"] = "browser"
    if not params.get("browser_mode"):
        params["browser_mode"] = "headless"
    if not params.get("keep_browser_open"):
        params["keep_browser_open"] = "false"
    concurrency = _codex_oauth_batch_concurrency(payload.get("concurrency", 1), len(account_ids))
    task_id = str(getattr(logger, "task_id", "") or "")

    with Session(engine) as session:
        records = session.exec(
            select(AccountModel)
            .where(AccountModel.id.in_(account_ids))
            .where(AccountModel.platform == platform)
        ).all() if account_ids else []
        by_id = {int(item.id or 0): item for item in records}

    accounts = [by_id[item] for item in account_ids if item in by_id]
    total = len(accounts)
    logger.set_progress(0, total)
    logger.log(f"Codex OAuth 批量授权: {total} 个账号，并发 {concurrency}")
    if total == 0:
        logger.set_result_data({"success": 0, "fail": 0, "accounts": []})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    def _do_one(model: AccountModel) -> dict[str, Any]:
        account_id = int(model.id or 0)
        email = str(model.email or "")
        logger.set_subtask(f"account_{account_id}", email or f"账号 {account_id}")
        try:
            if logger.is_cancel_requested():
                return {"account_id": account_id, "email": email, "cancelled": True}
            logger.log(f"{email}: 开始 Codex OAuth 授权")
            result = _execute_runtime_action_with_worker_proxy(
                platform=platform,
                account_id=account_id,
                action_id="codex_oauth_authorize",
                params=params,
                logger=logger,
                scope_id=task_id,
            )
            if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
                return {"account_id": account_id, "email": email, "cancelled": True}
            if not result.ok:
                error = str(result.error or "Codex OAuth 授权失败")
                logger.record_error(error)
                logger.log(f"{email}: Codex OAuth 授权失败: {error}", level="error")
                return {"account_id": account_id, "email": email, "ok": False, "error": error}
            logger.record_success()
            logger.log(f"{email}: Codex OAuth 授权完成")
            quota_refresh = _refresh_account_after_codex_oauth(
                account_id,
                logger,
                params=params,
                scope_id=task_id,
            )
            item = {
                "account_id": account_id,
                "email": email,
                "ok": True,
                "data": result.data,
                "quota_refresh": quota_refresh,
            }
            if auto_push_after_oauth:
                item["auto_push"] = _log_codex_auto_push_enqueue(
                    account_id,
                    logger,
                    platform=platform,
                )
            return item
        except Exception as exc:
            error = str(exc)
            logger.record_error(error)
            logger.log(f"{email}: Codex OAuth 授权异常: {error}", level="error")
            return {"account_id": account_id, "email": email, "ok": False, "error": error}
        finally:
            logger.clear_subtask()

    completed = 0
    success = 0
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    pool: ThreadPoolExecutor | None = None
    cancel_pool = False
    try:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        pending = set()
        next_index = 0
        while next_index < total and len(pending) < concurrency and not logger.is_cancel_requested():
            pending.add(pool.submit(_do_one, accounts[next_index]))
            next_index += 1
        while pending:
            if logger.is_cancel_requested():
                cancel_pool = True
                for future in pending:
                    future.cancel()
                logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                return
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                if future.cancelled():
                    continue
                result = future.result()
                if result.get("cancelled"):
                    cancel_pool = True
                    for item in pending:
                        item.cancel()
                    logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                    return
                completed += 1
                if result.get("ok"):
                    success += 1
                else:
                    errors.append(str(result.get("error") or "unknown"))
                results.append(result)
                logger.set_progress(completed, total)
                if logger.is_cancel_requested():
                    cancel_pool = True
                    for item in pending:
                        item.cancel()
                    logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                    return
            while next_index < total and len(pending) < concurrency and not logger.is_cancel_requested():
                pending.add(pool.submit(_do_one, accounts[next_index]))
                next_index += 1
    finally:
        if pool is not None:
            _shutdown_task_pool(pool, cancel_futures=cancel_pool)
        from core.worker_proxy import worker_proxy_manager

        worker_proxy_manager.clear_scope(task_id)

    result_data = {
        "success": success,
        "fail": len(errors),
        "account_ids": [item["account_id"] for item in results if item.get("ok")],
        "accounts": results,
    }
    logger.set_result_data(result_data)
    logger.log(f"Codex OAuth 批量授权完成: 成功 {success} 个, 失败 {len(errors)} 个", event_type="summary")
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    logger.finish(final_status, error=errors[0] if final_status == TASK_STATUS_FAILED else "")


def _execute_relogin_batch_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform") or "chatgpt")
    account_ids = [
        int(item)
        for item in payload.get("account_ids", [])
        if int(item or 0) > 0
    ]
    params = dict(payload.get("params") or {})
    if not params.get("browser_mode"):
        params["browser_mode"] = "headless"
    if not params.get("keep_browser_open"):
        params["keep_browser_open"] = "false"
    concurrency = _relogin_batch_concurrency(payload.get("concurrency", 1), len(account_ids))
    task_id = str(getattr(logger, "task_id", "") or "")

    with Session(engine) as session:
        records = session.exec(
            select(AccountModel)
            .where(AccountModel.id.in_(account_ids))
            .where(AccountModel.platform == platform)
        ).all() if account_ids else []
        by_id = {int(item.id or 0): item for item in records}

    accounts = [by_id[item] for item in account_ids if item in by_id]
    total = len(accounts)
    logger.set_progress(0, total)
    logger.log(f"批量重新登录: {total} 个账号，并发 {concurrency}")
    if total == 0:
        logger.set_result_data({"success": 0, "fail": 0, "accounts": []})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    def _do_one(model: AccountModel) -> dict[str, Any]:
        account_id = int(model.id or 0)
        email = str(model.email or "")
        logger.set_subtask(f"account_{account_id}", email or f"账号 {account_id}")
        try:
            return _execute_relogin_for_account(
                platform=platform,
                account_id=account_id,
                email=email,
                params=params,
                logger=logger,
                scope_id=task_id,
            )
        finally:
            logger.clear_subtask()

    completed = 0
    success = 0
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    pool: ThreadPoolExecutor | None = None
    cancel_pool = False
    try:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        pending = set()
        next_index = 0
        while next_index < total and len(pending) < concurrency and not logger.is_cancel_requested():
            pending.add(pool.submit(_do_one, accounts[next_index]))
            next_index += 1
        while pending:
            if logger.is_cancel_requested():
                cancel_pool = True
                for future in pending:
                    future.cancel()
                logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                return
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                if future.cancelled():
                    continue
                result = future.result()
                if result.get("cancelled"):
                    cancel_pool = True
                    for item in pending:
                        item.cancel()
                    logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                    return
                completed += 1
                if result.get("ok"):
                    success += 1
                else:
                    errors.append(str(result.get("error") or "unknown"))
                results.append(result)
                logger.set_progress(completed, total)
                if logger.is_cancel_requested():
                    cancel_pool = True
                    for item in pending:
                        item.cancel()
                    logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                    return
            while next_index < total and len(pending) < concurrency and not logger.is_cancel_requested():
                pending.add(pool.submit(_do_one, accounts[next_index]))
                next_index += 1
    finally:
        if pool is not None:
            _shutdown_task_pool(pool, cancel_futures=cancel_pool)
        from core.worker_proxy import worker_proxy_manager

        worker_proxy_manager.clear_scope(task_id)

    result_data = {
        "success": success,
        "fail": len(errors),
        "account_ids": [item["account_id"] for item in results if item.get("ok")],
        "accounts": results,
    }
    logger.set_result_data(result_data)
    logger.log(f"批量重新登录完成: 成功 {success} 个, 失败 {len(errors)} 个", event_type="summary")
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    logger.finish(final_status, error=errors[0] if final_status == TASK_STATUS_FAILED else "")


def _execute_account_push_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from application.account_pushes import AccountPushService
    from domain.accounts import AccountExportSelection

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return

    platform = str(payload.get("platform") or "chatgpt")
    account_ids = [
        int(item)
        for item in payload.get("account_ids", [])
        if int(item or 0) > 0
    ]
    target_key = str(payload.get("target_key") or "")
    payload_format = str(payload.get("payload_format") or "")
    logger.set_progress(0, len(account_ids))
    try:
        result = AccountPushService().push_accounts(
            AccountExportSelection(platform=platform, ids=account_ids),
            target_key=target_key,
            payload_format=payload_format,
        )
    except Exception as exc:  # noqa: BLE001
        if logger.is_cancel_requested():
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        error = str(exc)
        logger.record_error(error)
        logger.log(f"账号后台推送失败: {error}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=error)
        return

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return

    for item in result.get("results", []):
        if item.get("ok"):
            logger.record_success()
        else:
            logger.record_error(str(item.get("error") or "推送失败"))
    logger.set_progress(len(result.get("results", [])), len(account_ids))
    logger.set_result_data(result)
    logger.log(
        f"后台推送完成: 成功 {result.get('succeeded', 0)} 个, 失败 {result.get('failed', 0)} 个",
        event_type="summary",
    )
    if result.get("ok"):
        logger.finish(TASK_STATUS_SUCCEEDED)
    else:
        first_error = next(
            (str(item.get("error") or "") for item in result.get("results", []) if not item.get("ok")),
            "推送失败",
        )
        logger.finish(TASK_STATUS_FAILED, error=first_error)


def _execute_account_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.worker_proxy import WorkerProxyPolicy, worker_proxy_manager

    platform = str(payload.get("platform", "") or "")
    limit = max(int(payload.get("limit", 50) or 50), 1)
    account_ids = [
        int(item)
        for item in payload.get("account_ids", [])
        if int(item or 0) > 0
    ]

    with Session(engine) as session:
        q = select(AccountModel)
        if account_ids:
            q = q.where(AccountModel.id.in_(account_ids))
        elif "account_ids" in payload:
            q = q.where(AccountModel.id == -1)
        if platform:
            q = q.where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        if account_ids or "account_ids" in payload:
            accounts = session.exec(q).all()
        else:
            accounts = session.exec(q.limit(limit)).all()

    total = len(accounts)
    logger.set_progress(0, total)
    if total == 0:
        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    results = {"valid": 0, "invalid": 0, "error": 0}
    completed = 0
    task_id = str(getattr(logger, "task_id", "") or "")
    proxy_mode = normalize_proxy_mode(
        str(payload.get("platform_proxy_mode") or "").strip(),
        default=PROXY_MODE_PROXY_SERVICE,
    )
    manual_proxy = str(payload.get("platform_proxy_value") or payload.get("proxy") or "").strip() or None
    proxy_policy = WorkerProxyPolicy.load()
    for model in accounts:
        if logger.is_cancel_requested():
            worker_proxy_manager.clear_scope(task_id)
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        try:
            attempts = proxy_policy.replace_max_attempts if proxy_mode == PROXY_MODE_PROXY_SERVICE else 1
            valid = False
            for proxy_attempt in range(1, attempts + 1):
                lease = None
                account_proxy = manual_proxy if proxy_mode == PROXY_MODE_MANUAL else None
                try:
                    if proxy_mode == PROXY_MODE_PROXY_SERVICE:
                        lease = worker_proxy_manager.acquire(
                            scope_id=task_id,
                            log_fn=logger.log,
                            cancel_check=logger.is_cancel_requested,
                            policy=proxy_policy,
                        )
                        account_proxy = lease.url
                    logger.log(
                        f"{model.email}: 账号检测代理 "
                        f"{mask_proxy_url(account_proxy) if account_proxy else '直连'}（{proxy_mode}）"
                    )
                    valid, _ = _run_single_account_check(
                        int(model.id or 0),
                        logger,
                        proxy=account_proxy,
                        disable_proxy_pool=True,
                        strict_proxy=proxy_mode != PROXY_MODE_DIRECT,
                    )
                    if lease is not None:
                        lease.report_success()
                    break
                except Exception as exc:
                    if lease is not None and is_retryable_network_error(exc):
                        lease.report_failure()
                    if (
                        proxy_mode == PROXY_MODE_PROXY_SERVICE
                        and proxy_attempt < attempts
                        and is_retryable_network_error(exc)
                    ):
                        logger.log(
                            f"{model.email}: 检测代理异常，换 IP 重试 "
                            f"({proxy_attempt + 1}/{attempts}): {exc}",
                            level="warning",
                        )
                        continue
                    raise
                finally:
                    if lease is not None:
                        lease.release()
            if valid:
                results["valid"] += 1
            else:
                results["invalid"] += 1
        except Exception as exc:
            results["error"] += 1
            logger.record_error(str(exc))
            logger.log(f"{model.email}: 检测异常 {exc}", level="error")
        completed += 1
        logger.set_progress(completed, total)
    logger.set_result_data(results)
    logger.finish(TASK_STATUS_SUCCEEDED)
    worker_proxy_manager.clear_scope(task_id)
