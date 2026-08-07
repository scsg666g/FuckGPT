from __future__ import annotations

import json
import os
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from application.workflow_registry import (
    get_step_adapter,
    list_step_adapters,
    registered_workflow_definitions,
)
from core.datetime_utils import ensure_utc_datetime, format_local_clock, serialize_datetime
from core.db import (
    WorkflowBatchModel,
    WorkflowDefinitionModel,
    WorkflowEventModel,
    WorkflowRunModel,
    WorkflowStepRunModel,
    engine,
)
from domain.workflows import (
    RUN_CANCEL_REQUESTED,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_NEEDS_ATTENTION,
    RUN_PENDING,
    RUN_PAUSED,
    RUN_RETRY_SCHEDULED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_TERMINAL,
    RUN_WAITING_EXTERNAL,
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_NEEDS_ATTENTION,
    STEP_PENDING,
    STEP_READY,
    STEP_RETRY_SCHEDULED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    STEP_TERMINAL,
    STEP_WAITING_EXTERNAL,
    StepTransition,
    evaluate_condition,
    parse_duration_seconds,
    resolve_value,
)


FAILURE_POLICY_FAIL = "fail"
FAILURE_POLICY_NEEDS_ATTENTION = "needs_attention"
FAILURE_POLICY_SKIP = "skip"
FAILURE_POLICIES = {FAILURE_POLICY_FAIL, FAILURE_POLICY_NEEDS_ATTENTION, FAILURE_POLICY_SKIP}
LIMIT_HELD_STEP_STATUSES = {STEP_RUNNING, STEP_WAITING_EXTERNAL}
LOCAL_SLOT_HELD_STEP_STATUSES = {STEP_READY, STEP_RUNNING}
DEFAULT_STUCK_SECONDS = 30 * 60
DEFAULT_EXTERNAL_WAITING_LIMIT = 20
BATCH_RETRY_SLOT_HOLD_SECONDS = 60
_workflow_claim_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _load_json(value: str) -> dict[str, Any]:
    data = json.loads(value or "{}")
    return data if isinstance(data, dict) else {}


def _due(value: datetime | str | None, now: datetime) -> bool:
    normalized = ensure_utc_datetime(value)
    return normalized is None or normalized <= now


def _seconds_between(start: datetime | str | None, end: datetime | str | None) -> int:
    start_dt = ensure_utc_datetime(start)
    end_dt = ensure_utc_datetime(end) or _utcnow()
    if not start_dt:
        return 0
    return max(int((end_dt - start_dt).total_seconds()), 0)


def _duration_from_model(model: Any) -> int:
    return _seconds_between(
        getattr(model, "started_at", None) or getattr(model, "created_at", None),
        getattr(model, "finished_at", None) or _utcnow(),
    )


def _event_line(event: WorkflowEventModel) -> str:
    prefix = f"[{format_local_clock(event.created_at)}]"
    step = f"[{event.step_id}]" if event.step_id else ""
    return f"{prefix}{step} {event.message}"


def _serialize_event(event: WorkflowEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "workflow_run_id": event.workflow_run_id,
        "step_id": event.step_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": _event_line(event),
        "detail": event.get_detail(),
        "created_at": serialize_datetime(event.created_at),
    }


def _serialize_definition(model: WorkflowDefinitionModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "key": model.key,
        "version": model.version,
        "name": model.name,
        "description": model.description,
        "enabled": bool(model.enabled),
        "definition": model.get_definition(),
        "created_at": serialize_datetime(model.created_at),
        "updated_at": serialize_datetime(model.updated_at),
    }


def _serialize_step(step: WorkflowStepRunModel) -> dict[str, Any]:
    return {
        "id": step.id,
        "workflow_run_id": step.workflow_run_id,
        "step_id": step.step_id,
        "name": step.name,
        "adapter_key": step.adapter_key,
        "status": step.status,
        "attempt": int(step.attempt or 0),
        "max_attempts": int(step.max_attempts or 1),
        "input": step.get_input(),
        "output": step.get_output(),
        "error": step.get_error(),
        "external_ref": step.external_ref,
        "idempotency_key": step.idempotency_key,
        "next_run_at": serialize_datetime(step.next_run_at),
        "timeout_at": serialize_datetime(step.timeout_at),
        "started_at": serialize_datetime(step.started_at),
        "finished_at": serialize_datetime(step.finished_at),
        "duration_seconds": _duration_from_model(step) if step.started_at else 0,
        "created_at": serialize_datetime(step.created_at),
        "updated_at": serialize_datetime(step.updated_at),
    }


def _serialize_run(
    run: WorkflowRunModel,
    *,
    steps: list[WorkflowStepRunModel] | None = None,
    include_definition: bool = True,
) -> dict[str, Any]:
    data = {
        "id": run.id,
        "batch_id": run.batch_id,
        "batch_item_index": int(run.batch_item_index or 0),
        "definition_key": run.definition_key,
        "definition_version": int(run.definition_version or 1),
        "name": run.name,
        "status": run.status,
        "terminal": run.status in RUN_TERMINAL,
        "input": run.get_input(),
        "metadata": run.get_metadata(),
        "context": run.get_context(),
        "output": run.get_output(),
        "current_step_id": run.current_step_id,
        "error": run.error,
        "cancellation_requested_at": serialize_datetime(run.cancellation_requested_at),
        "started_at": serialize_datetime(run.started_at),
        "finished_at": serialize_datetime(run.finished_at),
        "created_at": serialize_datetime(run.created_at),
        "updated_at": serialize_datetime(run.updated_at),
    }
    if include_definition:
        data["definition"] = run.get_definition()
    if steps is not None:
        data["steps"] = [_serialize_step(item) for item in steps]
    return data


def _status_counts(runs: list[WorkflowRunModel]) -> dict[str, int]:
    counts = {
        "total": len(runs),
        "pending": 0,
        "running": 0,
        "waiting_external": 0,
        "retry_scheduled": 0,
        "needs_attention": 0,
        "cancel_requested": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "terminal": 0,
        "active": 0,
    }
    for run in runs:
        status = str(run.status or "")
        if status in counts:
            counts[status] += 1
        if status in RUN_TERMINAL:
            counts["terminal"] += 1
        else:
            counts["active"] += 1
    return counts


def _run_steps_progress_count(steps: list[WorkflowStepRunModel]) -> int:
    return sum(
        1
        for step in steps
        if step.status not in {STEP_PENDING, STEP_READY} or int(step.attempt or 0) > 0
    )


def _batch_status_from_counts(counts: dict[str, int]) -> str:
    total = int(counts.get("total") or 0)
    if total <= 0:
        return RUN_PENDING
    if int(counts.get("active") or 0) <= 0:
        if int(counts.get("failed") or 0) > 0:
            return RUN_FAILED
        if int(counts.get("cancelled") or 0) > 0:
            return RUN_CANCELLED
        return RUN_SUCCEEDED
    if int(counts.get("cancel_requested") or 0) > 0:
        return RUN_CANCEL_REQUESTED
    if int(counts.get("needs_attention") or 0) > 0:
        return RUN_NEEDS_ATTENTION
    if int(counts.get("running") or 0) > 0:
        return RUN_RUNNING
    if int(counts.get("waiting_external") or 0) > 0:
        return RUN_WAITING_EXTERNAL
    if int(counts.get("retry_scheduled") or 0) > 0:
        return RUN_RETRY_SCHEDULED
    return RUN_PENDING


def _serialize_batch(
    session: Session,
    batch: WorkflowBatchModel,
    *,
    include_runs: bool = False,
) -> dict[str, Any]:
    runs = session.exec(
        select(WorkflowRunModel)
        .where(WorkflowRunModel.batch_id == batch.id)
        .order_by(WorkflowRunModel.batch_item_index, WorkflowRunModel.created_at)
    ).all()
    counts = _status_counts(runs)
    status = _batch_status_from_counts(counts)
    if batch.status == RUN_PAUSED and status not in RUN_TERMINAL:
        status = RUN_PAUSED
    return {
        "id": batch.id,
        "definition_key": batch.definition_key,
        "definition_version": int(batch.definition_version or 1),
        "name": batch.name,
        "status": status,
        "terminal": status in RUN_TERMINAL,
        "total": int(batch.total or counts.get("total") or 0),
        "concurrency": int(batch.concurrency or 1),
        "input": batch.get_input(),
        "summary": counts,
        "observability": _batch_observability(runs),
        "created_at": serialize_datetime(batch.created_at),
        "updated_at": serialize_datetime(batch.updated_at),
        "runs": [_serialize_run(run, include_definition=False) for run in runs] if include_runs else None,
    }


def _step_summary(step: WorkflowStepRunModel, definition: dict[str, Any]) -> dict[str, Any]:
    error = step.get_error()
    stuck = _step_stuck_info(step, definition)
    return {
        "step_id": step.step_id,
        "name": step.name,
        "status": step.status,
        "adapter_key": step.adapter_key,
        "attempt": int(step.attempt or 0),
        "max_attempts": int(step.max_attempts or 1),
        "error_code": str(error.get("code") or ""),
        "error_message": str(error.get("message") or ""),
        "error_category": str(error.get("category") or ""),
        "operator_hint": str(error.get("operator_hint") or ""),
        "external_ref": step.external_ref,
        "duration_seconds": _duration_from_model(step) if step.started_at else 0,
        "stuck": bool(stuck["stuck"]),
        "stuck_reason": str(stuck["stuck_reason"] or ""),
    }


def _current_summary_step(
    run: WorkflowRunModel,
    steps: list[WorkflowStepRunModel],
) -> WorkflowStepRunModel | None:
    attention = next((step for step in steps if step.status in {STEP_NEEDS_ATTENTION, STEP_FAILED}), None)
    if attention:
        return attention
    for status in (STEP_RUNNING, STEP_READY, STEP_WAITING_EXTERNAL, STEP_RETRY_SCHEDULED):
        current = next((step for step in steps if step.status == status), None)
        if current:
            return current
    if run.current_step_id:
        current = next((step for step in steps if step.step_id == run.current_step_id), None)
        if current:
            return current
    return next((step for step in reversed(steps) if step.status != STEP_PENDING), None)


def _run_summary(run: WorkflowRunModel, steps: list[WorkflowStepRunModel]) -> dict[str, Any]:
    return _run_summary_with_batch_state(run, steps, batch_paused=False)


def _run_summary_with_batch_state(
    run: WorkflowRunModel,
    steps: list[WorkflowStepRunModel],
    *,
    batch_paused: bool = False,
) -> dict[str, Any]:
    definition = run.get_definition()
    by_status = {step.status for step in steps}
    register = next((step for step in steps if step.step_id == "register"), None)
    register_output = register.get_output() if register else {}
    account = register_output.get("account") if isinstance(register_output.get("account"), dict) else {}
    account_id = int(register_output.get("account_id") or account.get("account_id") or 0)
    email = str(account.get("email") or "")
    current_step = _current_summary_step(run, steps)
    current_error = current_step.get_error() if current_step else {}
    if batch_paused and run.status not in RUN_TERMINAL:
        step_label = (current_step.name if current_step else run.current_step_id) or "-"
        if run.status == RUN_NEEDS_ATTENTION:
            display_status = f"批次已暂停: {str(current_error.get('message') or step_label)}"
            operator_action = "处理问题后恢复批次"
        else:
            display_status = f"批次已暂停: {step_label}"
            operator_action = "恢复批次后继续"
    elif run.status == RUN_SUCCEEDED:
        display_status = "工作流已完成"
        operator_action = "无需操作"
    elif run.status == RUN_FAILED:
        display_status = str(current_error.get("message") or run.error or "工作流失败")
        operator_action = str(current_error.get("operator_hint") or "查看失败步骤后重试")
    elif run.status == RUN_NEEDS_ATTENTION:
        display_status = str(current_error.get("message") or "需要人工处理")
        operator_action = str(current_error.get("operator_hint") or "处理后重试")
    elif run.status == RUN_WAITING_EXTERNAL:
        display_status = f"等待外部结果: {(current_step.name if current_step else run.current_step_id) or '-'}"
        operator_action = "等待自动推进"
    elif run.status == RUN_RETRY_SCHEDULED:
        display_status = "等待自动重试"
        operator_action = "可等待或人工重试"
    elif run.status == RUN_RUNNING:
        step_label = (current_step.name if current_step else run.current_step_id) or "-"
        if current_step and current_step.status == STEP_RUNNING:
            display_status = f"正在执行: {step_label}"
            operator_action = "等待执行完成"
        elif current_step and current_step.status == STEP_READY:
            display_status = f"等待执行: {step_label}"
            operator_action = "等待 worker 接手"
        else:
            display_status = f"正在推进: {step_label}"
            operator_action = "等待自动推进"
    else:
        display_status = "等待调度"
        operator_action = "等待启动"
    stuck = _run_stuck_info(run, steps)
    return {
        "run_id": run.id,
        "batch_id": run.batch_id,
        "batch_item_index": int(run.batch_item_index or 0),
        "definition_key": run.definition_key,
        "status": run.status,
        "terminal": run.status in RUN_TERMINAL,
        "batch_paused": bool(batch_paused and run.status not in RUN_TERMINAL),
        "account_id": account_id,
        "email": email,
        "current_stage": (current_step.step_id if current_step else run.current_step_id) or "",
        "display_status": display_status,
        "operator_action": operator_action,
        "risk": "needs_attention" if STEP_NEEDS_ATTENTION in by_status else ("failed" if STEP_FAILED in by_status else "none"),
        "duration_seconds": _duration_from_model(run),
        "stuck": bool(stuck["stuck"]),
        "stuck_reason": str(stuck["stuck_reason"] or ""),
        "stuck_step_id": str(stuck["stuck_step_id"] or ""),
        "steps": [_step_summary(step, definition) for step in steps],
    }


def _batch_observability(runs: list[WorkflowRunModel], summaries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    durations = [_duration_from_model(run) for run in runs if run.started_at or run.created_at]
    stuck_count = 0
    if summaries is not None:
        stuck_count = len([item for item in summaries if item.get("stuck")])
    return {
        "duration_seconds_avg": int(sum(durations) / len(durations)) if durations else 0,
        "duration_seconds_max": max(durations or [0]),
        "stuck": stuck_count,
    }


def _append_event_in_session(
    session: Session,
    run_id: str,
    message: str,
    *,
    step_id: str = "",
    event_type: str = "log",
    level: str = "info",
    detail: dict[str, Any] | None = None,
) -> None:
    event = WorkflowEventModel(
        workflow_run_id=run_id,
        step_id=step_id,
        type=event_type,
        level=level,
        message=message,
        detail_json=_dump_json(detail or {}),
    )
    session.add(event)


def _steps_for_run(session: Session, run_id: str) -> list[WorkflowStepRunModel]:
    return session.exec(
        select(WorkflowStepRunModel).where(WorkflowStepRunModel.workflow_run_id == run_id)
    ).all()


def _step_defs(definition: dict[str, Any]) -> list[dict[str, Any]]:
    steps = definition.get("steps") if isinstance(definition.get("steps"), list) else []
    return [item for item in steps if isinstance(item, dict)]


def _step_def_map(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id") or ""): item for item in _step_defs(definition)}


def _step_order(definition: dict[str, Any]) -> dict[str, int]:
    return {str(item.get("id") or ""): index for index, item in enumerate(_step_defs(definition))}


def _sort_datetime(value: datetime | str | None) -> datetime:
    return ensure_utc_datetime(value) or datetime.min.replace(tzinfo=timezone.utc)


def _run_promotion_sort_key(session: Session, run: WorkflowRunModel) -> tuple:
    steps = _steps_for_run(session, run.id)
    progress = _run_steps_progress_count(steps)
    return (
        str(run.batch_id or ""),
        0 if progress > 0 else 1,
        -progress,
        int(run.batch_item_index or 0),
        _sort_datetime(run.created_at),
        run.id,
    )


def _step_claim_sort_key(session: Session, step: WorkflowStepRunModel) -> tuple:
    run = session.get(WorkflowRunModel, step.workflow_run_id)
    if not run:
        return (9, "", 1, 0, 0, 99, _sort_datetime(step.next_run_at), _sort_datetime(step.created_at), step.id)
    steps = _steps_for_run(session, run.id)
    progress = _run_steps_progress_count(steps)
    definition_order = _step_order(run.get_definition())
    status_priority = {
        STEP_WAITING_EXTERNAL: 0,
        STEP_RETRY_SCHEDULED: 1,
        STEP_READY: 2,
    }.get(step.status, 9)
    return (
        0,
        str(run.batch_id or ""),
        0 if progress > 0 else 1,
        -progress,
        int(run.batch_item_index or 0),
        definition_order.get(step.step_id, 0),
        status_priority,
        _sort_datetime(step.next_run_at),
        _sort_datetime(step.created_at),
        step.id,
    )


def _stuck_threshold_seconds(definition: dict[str, Any], step_id: str = "") -> int:
    spec = _step_def_map(definition).get(step_id) if step_id else {}
    raw = (spec or {}).get("stuck_after") or definition.get("stuck_after") or "30m"
    return parse_duration_seconds(raw, default=DEFAULT_STUCK_SECONDS)


def _step_stuck_info(step: WorkflowStepRunModel, definition: dict[str, Any]) -> dict[str, Any]:
    if step.status not in {STEP_READY, STEP_RUNNING, STEP_WAITING_EXTERNAL, STEP_RETRY_SCHEDULED}:
        return {"stuck": False, "stuck_reason": ""}
    threshold = _stuck_threshold_seconds(definition, step.step_id)
    if threshold <= 0:
        return {"stuck": False, "stuck_reason": ""}
    now = _utcnow()
    if step.status == STEP_RUNNING:
        age = _seconds_between(step.started_at or step.updated_at, now)
        if age >= threshold:
            return {"stuck": True, "stuck_reason": f"步骤执行超过 {threshold} 秒"}
    if step.status in {STEP_READY, STEP_WAITING_EXTERNAL, STEP_RETRY_SCHEDULED}:
        due_at = ensure_utc_datetime(step.next_run_at)
        if due_at and due_at <= now and int((now - due_at).total_seconds()) >= threshold:
            return {"stuck": True, "stuck_reason": f"步骤超过 {threshold} 秒未被继续调度"}
    return {"stuck": False, "stuck_reason": ""}


def _run_stuck_info(run: WorkflowRunModel, steps: list[WorkflowStepRunModel]) -> dict[str, Any]:
    definition = run.get_definition()
    for step in steps:
        info = _step_stuck_info(step, definition)
        if info["stuck"]:
            return {
                "stuck": True,
                "stuck_reason": info["stuck_reason"],
                "stuck_step_id": step.step_id,
            }
    return {"stuck": False, "stuck_reason": "", "stuck_step_id": ""}


def _build_context(run: WorkflowRunModel, steps: list[WorkflowStepRunModel]) -> dict[str, Any]:
    context = run.get_context()
    if not isinstance(context, dict):
        context = {}
    workflow_context = context.get("workflow") if isinstance(context.get("workflow"), dict) else {}
    workflow_context["inputs"] = run.get_input()
    context["workflow"] = workflow_context
    context["steps"] = {
        step.step_id: {
            "status": step.status,
            "input": step.get_input(),
            "output": step.get_output(),
            "error": step.get_error(),
            "external_ref": step.external_ref,
            "attempt": int(step.attempt or 0),
        }
        for step in steps
    }
    return context


def _run_output_from_steps(steps: list[WorkflowStepRunModel]) -> dict[str, Any]:
    return {
        "steps": {
            step.step_id: {
                "status": step.status,
                "output": step.get_output(),
                "external_ref": step.external_ref,
            }
            for step in steps
        }
    }


def _normalize_positive_limit(value: Any, *, default: int = 0, maximum: int = 200) -> int:
    try:
        number = int(value or default)
    except (TypeError, ValueError):
        raise ValueError("并发限制必须是数字")
    if number <= 0:
        return 0
    return min(number, maximum)


def _normalize_workflow_limits(
    raw_limits: Any,
    *,
    step_ids: set[str],
    adapter_keys: set[str],
) -> dict[str, dict[str, int]]:
    if raw_limits in (None, ""):
        return {"adapters": {}, "steps": {}}
    if not isinstance(raw_limits, dict):
        raise ValueError("limits 必须是对象")
    normalized = {"adapters": {}, "steps": {}}
    raw_adapters = raw_limits.get("adapters") if isinstance(raw_limits.get("adapters"), dict) else {}
    raw_steps = raw_limits.get("steps") if isinstance(raw_limits.get("steps"), dict) else {}
    for key, value in raw_adapters.items():
        adapter_key = str(key).strip()
        if adapter_key not in adapter_keys:
            raise ValueError(f"adapter 限流引用了未注册 adapter: {adapter_key}")
        limit = _normalize_positive_limit(value)
        if limit:
            normalized["adapters"][adapter_key] = limit
    for key, value in raw_steps.items():
        step_id = str(key).strip()
        if step_id not in step_ids:
            raise ValueError(f"步骤限流引用了不存在的步骤: {step_id}")
        limit = _normalize_positive_limit(value)
        if limit:
            normalized["steps"][step_id] = limit
    return normalized


def validate_workflow_definition(definition: dict[str, Any]) -> dict[str, Any]:
    key = str(definition.get("key") or "").strip()
    version = max(int(definition.get("version") or 1), 1)
    if not key:
        raise ValueError("工作流定义 key 不能为空")
    steps = _step_defs(definition)
    if not steps:
        raise ValueError("工作流至少需要一个步骤")

    known_adapters = set(list_step_adapters())
    normalized_steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in steps:
        step = dict(raw)
        step_id = str(step.get("id") or "").strip()
        adapter_key = str(step.get("uses") or "").strip()
        if not step_id:
            raise ValueError("工作流步骤 id 不能为空")
        if step_id in seen:
            raise ValueError(f"工作流步骤 id 重复: {step_id}")
        if not adapter_key:
            raise ValueError(f"步骤 {step_id} 缺少 uses")
        if adapter_key not in known_adapters:
            raise ValueError(f"步骤 {step_id} 使用了未注册 adapter: {adapter_key}")
        needs = [str(item).strip() for item in step.get("needs") or [] if str(item).strip()]
        on_failure = str(step.get("on_failure") or FAILURE_POLICY_FAIL).strip()
        if on_failure not in FAILURE_POLICIES:
            raise ValueError(f"步骤 {step_id} 的 on_failure 不支持: {on_failure}")
        step_concurrency = _normalize_positive_limit(step.get("concurrency"), default=0, maximum=200)
        stuck_after = str(step.get("stuck_after") or "").strip()
        step.update(
            {
                "id": step_id,
                "name": str(step.get("name") or step_id),
                "uses": adapter_key,
                "needs": needs,
                "input": step.get("input") if isinstance(step.get("input"), dict) else {},
                "max_attempts": max(int(step.get("max_attempts") or 1), 1),
                "retry_delay": step.get("retry_delay", "30s"),
                "timeout": step.get("timeout", ""),
                "concurrency": step_concurrency,
                "on_failure": on_failure,
                "stuck_after": stuck_after,
            }
        )
        if "if" in step and not isinstance(step.get("if"), (dict, bool)):
            raise ValueError(f"步骤 {step_id} 的 if 条件必须是对象或布尔值")
        parse_duration_seconds(step.get("retry_delay"), default=30)
        parse_duration_seconds(step.get("timeout"), default=0)
        if stuck_after:
            parse_duration_seconds(stuck_after, default=DEFAULT_STUCK_SECONDS)
        normalized_steps.append(step)
        seen.add(step_id)

    for step in normalized_steps:
        for need in step.get("needs") or []:
            if need not in seen:
                raise ValueError(f"步骤 {step['id']} 依赖不存在: {need}")

    graph = {step["id"]: list(step.get("needs") or []) for step in normalized_steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError(f"工作流存在循环依赖: {node}")
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)

    stuck_after = str(definition.get("stuck_after") or "30m").strip()
    parse_duration_seconds(stuck_after, default=DEFAULT_STUCK_SECONDS)
    limits = _normalize_workflow_limits(
        definition.get("limits"),
        step_ids=seen,
        adapter_keys=known_adapters,
    )

    return {
        **definition,
        "key": key,
        "version": version,
        "name": str(definition.get("name") or key),
        "description": str(definition.get("description") or ""),
        "enabled": bool(definition.get("enabled", True)),
        "stuck_after": stuck_after,
        "limits": limits,
        "steps": normalized_steps,
    }


def sync_registered_workflow_definitions() -> None:
    with Session(engine) as session:
        for raw in registered_workflow_definitions():
            definition = validate_workflow_definition(raw)
            model = session.exec(
                select(WorkflowDefinitionModel)
                .where(WorkflowDefinitionModel.key == definition["key"])
                .where(WorkflowDefinitionModel.version == int(definition["version"]))
            ).first()
            if not model:
                model = WorkflowDefinitionModel(
                    key=definition["key"],
                    version=int(definition["version"]),
                    name=definition["name"],
                    description=definition.get("description", ""),
                    enabled=bool(definition.get("enabled", True)),
                )
            model.name = definition["name"]
            model.description = str(definition.get("description") or "")
            model.enabled = bool(definition.get("enabled", True))
            model.definition_json = _dump_json(definition)
            model.updated_at = _utcnow()
            session.add(model)
        session.commit()


def list_workflow_adapters() -> list[dict[str, str]]:
    return [{"key": key} for key in sorted(list_step_adapters())]


def list_workflow_definitions(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    with Session(engine) as session:
        query = select(WorkflowDefinitionModel)
        if not include_disabled:
            query = query.where(WorkflowDefinitionModel.enabled == True)  # noqa: E712
        items = session.exec(query.order_by(WorkflowDefinitionModel.key, WorkflowDefinitionModel.version)).all()
        return [_serialize_definition(item) for item in items]


def create_or_update_workflow_definition(definition: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_workflow_definition(definition)
    with Session(engine) as session:
        model = session.exec(
            select(WorkflowDefinitionModel)
            .where(WorkflowDefinitionModel.key == normalized["key"])
            .where(WorkflowDefinitionModel.version == int(normalized["version"]))
        ).first()
        if not model:
            model = WorkflowDefinitionModel(
                key=normalized["key"],
                version=int(normalized["version"]),
                name=normalized["name"],
            )
        model.name = normalized["name"]
        model.description = str(normalized.get("description") or "")
        model.enabled = bool(normalized.get("enabled", True))
        model.definition_json = _dump_json(normalized)
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()
        session.refresh(model)
        return _serialize_definition(model)


def _get_definition_model(
    session: Session,
    definition_key: str,
    *,
    version: int = 0,
    require_enabled: bool = True,
) -> WorkflowDefinitionModel | None:
    query = select(WorkflowDefinitionModel).where(WorkflowDefinitionModel.key == definition_key)
    if require_enabled:
        query = query.where(WorkflowDefinitionModel.enabled == True)  # noqa: E712
    if version > 0:
        return session.exec(query.where(WorkflowDefinitionModel.version == version)).first()
    return session.exec(query.order_by(WorkflowDefinitionModel.version.desc())).first()


def _set_run_context(session: Session, run: WorkflowRunModel, steps: list[WorkflowStepRunModel]) -> None:
    run.context_json = _dump_json(_build_context(run, steps))
    session.add(run)


def _mark_step_skipped(
    session: Session,
    run: WorkflowRunModel,
    step: WorkflowStepRunModel,
    *,
    message: str,
) -> None:
    now = _utcnow()
    step.status = STEP_SKIPPED
    step.output_json = _dump_json({"reason": "condition_false"})
    step.error_json = "{}"
    step.next_run_at = None
    step.timeout_at = None
    step.finished_at = now
    step.updated_at = now
    session.add(step)
    _append_event_in_session(session, run.id, message, step_id=step.step_id, event_type="state")


def _promote_ready_steps(session: Session, run: WorkflowRunModel) -> bool:
    changed = False
    definition = run.get_definition()
    step_defs = _step_def_map(definition)
    if run.batch_id:
        batch = session.get(WorkflowBatchModel, run.batch_id)
        if batch and batch.status == RUN_PAUSED:
            return False
    while True:
        loop_changed = False
        steps = _steps_for_run(session, run.id)
        by_id = {step.step_id: step for step in steps}
        context = _build_context(run, steps)

        for step in steps:
            if step.status != STEP_PENDING:
                continue
            spec = step_defs.get(step.step_id) or {}
            needs = [str(item) for item in spec.get("needs") or []]
            dependency_steps = [by_id.get(item) for item in needs]
            if any(item is None or item.status not in STEP_TERMINAL for item in dependency_steps):
                continue
            if any(item and item.status in {STEP_FAILED, STEP_CANCELLED} for item in dependency_steps):
                continue
            try:
                should_run = evaluate_condition(spec.get("if"), context)
            except Exception as exc:
                step.status = STEP_NEEDS_ATTENTION
                step.error_json = _dump_json({"code": "condition_invalid", "message": str(exc)})
                step.updated_at = _utcnow()
                session.add(step)
                _append_event_in_session(
                    session,
                    run.id,
                    f"步骤条件无法判断: {exc}",
                    step_id=step.step_id,
                    event_type="state",
                    level="warning",
                )
                loop_changed = True
                continue
            if not should_run:
                _mark_step_skipped(session, run, step, message="步骤条件未满足，已跳过")
                loop_changed = True
                continue
            now = _utcnow()
            if not _batch_can_promote_step(session, run, now=now):
                continue
            step.input_json = _dump_json(resolve_value(spec.get("input") or {}, context))
            step.status = STEP_READY
            step.next_run_at = now
            step.updated_at = now
            session.add(step)
            _append_event_in_session(session, run.id, "步骤已就绪", step_id=step.step_id, event_type="state")
            loop_changed = True

        if not loop_changed:
            break
        changed = True
    steps = _steps_for_run(session, run.id)
    _set_run_context(session, run, steps)
    return changed


def _finish_run(
    session: Session,
    run: WorkflowRunModel,
    *,
    status: str,
    error: str = "",
    steps: list[WorkflowStepRunModel] | None = None,
) -> None:
    now = _utcnow()
    run.status = status
    run.error = error
    run.finished_at = now
    run.updated_at = now
    run.started_at = run.started_at or now
    if steps is None:
        steps = _steps_for_run(session, run.id)
    run.output_json = _dump_json(_run_output_from_steps(steps))
    _set_run_context(session, run, steps)
    session.add(run)


def _refresh_batch_status_in_session(session: Session, batch_id: str) -> None:
    if not batch_id:
        return
    batch = session.get(WorkflowBatchModel, batch_id)
    if not batch:
        return
    runs = session.exec(select(WorkflowRunModel).where(WorkflowRunModel.batch_id == batch_id)).all()
    next_status = _batch_status_from_counts(_status_counts(runs))
    if batch.status == RUN_PAUSED and next_status not in RUN_TERMINAL:
        next_status = RUN_PAUSED
    batch.status = next_status
    batch.updated_at = _utcnow()
    session.add(batch)


def _step_holds_local_batch_slot(step: WorkflowStepRunModel, *, now: datetime) -> bool:
    if step.status in LOCAL_SLOT_HELD_STEP_STATUSES:
        return True
    if step.status != STEP_RETRY_SCHEDULED:
        return False
    retry_at = ensure_utc_datetime(step.next_run_at)
    if retry_at is None:
        return True
    scheduled_at = ensure_utc_datetime(step.updated_at) or now
    retry_delay_seconds = max(int((retry_at - scheduled_at).total_seconds()), 0)
    return retry_delay_seconds <= BATCH_RETRY_SLOT_HOLD_SECONDS


def _batch_local_slot_run_count(session: Session, batch_id: str, *, now: datetime, exclude_run_id: str = "") -> int:
    if not batch_id:
        return 0
    steps = session.exec(
        select(WorkflowStepRunModel)
        .join(WorkflowRunModel, WorkflowRunModel.id == WorkflowStepRunModel.workflow_run_id)
        .where(WorkflowRunModel.batch_id == batch_id)
        .where(WorkflowStepRunModel.status.in_(list(LOCAL_SLOT_HELD_STEP_STATUSES | {STEP_RETRY_SCHEDULED})))
    ).all()
    run_ids: set[str] = set()
    for step in steps:
        run_id = str(step.workflow_run_id)
        if run_id == exclude_run_id:
            continue
        if _step_holds_local_batch_slot(step, now=now):
            run_ids.add(run_id)
    return len(run_ids)


def _batch_run_holds_local_slot(session: Session, run: WorkflowRunModel, *, now: datetime) -> bool:
    if not run.batch_id:
        return True
    steps = _steps_for_run(session, run.id)
    return any(_step_holds_local_batch_slot(step, now=now) for step in steps)


def _batch_can_promote_step(session: Session, run: WorkflowRunModel, *, now: datetime) -> bool:
    if not run.batch_id:
        return True
    batch = session.get(WorkflowBatchModel, run.batch_id)
    if not batch:
        return True
    if batch.status == RUN_PAUSED:
        return False
    concurrency = max(int(batch.concurrency or 1), 1)
    if _batch_run_holds_local_slot(session, run, now=now):
        return True
    return _batch_local_slot_run_count(session, run.batch_id, now=now, exclude_run_id=run.id) < concurrency


def _batch_can_claim_step(session: Session, run: WorkflowRunModel, step: WorkflowStepRunModel, *, now: datetime) -> bool:
    if not run.batch_id:
        return True
    batch = session.get(WorkflowBatchModel, run.batch_id)
    if not batch:
        return True
    if batch.status == RUN_PAUSED:
        return False
    if step.status == STEP_WAITING_EXTERNAL:
        return True
    concurrency = max(int(batch.concurrency or 1), 1)
    if _batch_run_holds_local_slot(session, run, now=now):
        return True
    return _batch_local_slot_run_count(session, run.batch_id, now=now, exclude_run_id=run.id) < concurrency


def _workflow_limits(definition: dict[str, Any]) -> dict[str, dict[str, int]]:
    raw_limits = definition.get("limits") if isinstance(definition.get("limits"), dict) else {}
    return {
        "adapters": raw_limits.get("adapters") if isinstance(raw_limits.get("adapters"), dict) else {},
        "steps": raw_limits.get("steps") if isinstance(raw_limits.get("steps"), dict) else {},
    }


def _default_external_waiting_limit() -> int:
    try:
        return _normalize_positive_limit(
            os.environ.get("WORKFLOW_DEFAULT_EXTERNAL_WAITING_LIMIT"),
            default=DEFAULT_EXTERNAL_WAITING_LIMIT,
            maximum=200,
        )
    except ValueError:
        return DEFAULT_EXTERNAL_WAITING_LIMIT


def _inflight_adapter_count(session: Session, adapter_key: str, *, exclude_step_pk: str = "") -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.adapter_key == adapter_key)
            .where(WorkflowStepRunModel.status.in_(list(LIMIT_HELD_STEP_STATUSES)))
            .where(WorkflowStepRunModel.id != exclude_step_pk)
        ).one()
        or 0
    )


def _waiting_external_adapter_count(session: Session, adapter_key: str, *, exclude_step_pk: str = "") -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.adapter_key == adapter_key)
            .where(WorkflowStepRunModel.status == STEP_WAITING_EXTERNAL)
            .where(WorkflowStepRunModel.id != exclude_step_pk)
        ).one()
        or 0
    )


def _waiting_external_definition_step_count(
    session: Session,
    run: WorkflowRunModel,
    step: WorkflowStepRunModel,
) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(WorkflowStepRunModel)
            .join(WorkflowRunModel, WorkflowRunModel.id == WorkflowStepRunModel.workflow_run_id)
            .where(WorkflowRunModel.definition_key == run.definition_key)
            .where(WorkflowRunModel.definition_version == run.definition_version)
            .where(WorkflowStepRunModel.step_id == step.step_id)
            .where(WorkflowStepRunModel.status == STEP_WAITING_EXTERNAL)
            .where(WorkflowStepRunModel.id != step.id)
        ).one()
        or 0
    )


def _inflight_definition_step_count(
    session: Session,
    run: WorkflowRunModel,
    step: WorkflowStepRunModel,
) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(WorkflowStepRunModel)
            .join(WorkflowRunModel, WorkflowRunModel.id == WorkflowStepRunModel.workflow_run_id)
            .where(WorkflowRunModel.definition_key == run.definition_key)
            .where(WorkflowRunModel.definition_version == run.definition_version)
            .where(WorkflowStepRunModel.step_id == step.step_id)
            .where(WorkflowStepRunModel.status.in_(list(LIMIT_HELD_STEP_STATUSES)))
            .where(WorkflowStepRunModel.id != step.id)
        ).one()
        or 0
    )


def _step_can_claim_by_limits(session: Session, run: WorkflowRunModel, step: WorkflowStepRunModel) -> bool:
    definition = run.get_definition()
    spec = _step_def_map(definition).get(step.step_id) or {}
    limits = _workflow_limits(definition)
    adapter_limit = _normalize_positive_limit(limits["adapters"].get(step.adapter_key), default=0, maximum=200)
    if adapter_limit and _inflight_adapter_count(session, step.adapter_key, exclude_step_pk=step.id) >= adapter_limit:
        return False
    step_limit = _normalize_positive_limit(
        spec.get("concurrency") or limits["steps"].get(step.step_id),
        default=0,
        maximum=200,
    )
    if step_limit and _inflight_definition_step_count(session, run, step) >= step_limit:
        return False
    if step.status != STEP_WAITING_EXTERNAL:
        external_adapter_limit = adapter_limit or _default_external_waiting_limit()
        if (
            external_adapter_limit
            and _waiting_external_adapter_count(session, step.adapter_key, exclude_step_pk=step.id)
            >= external_adapter_limit
        ):
            return False
        if (
            step_limit
            and _waiting_external_definition_step_count(session, run, step)
            >= step_limit
        ):
            return False
    return True


def _refresh_run_status(session: Session, run: WorkflowRunModel) -> None:
    steps = _steps_for_run(session, run.id)
    statuses = [step.status for step in steps]
    now = _utcnow()

    def sync_batch() -> None:
        _refresh_batch_status_in_session(session, run.batch_id)

    if run.cancellation_requested_at:
        for step in steps:
            if step.status == STEP_RUNNING:
                run.status = RUN_CANCEL_REQUESTED
                run.updated_at = now
                _set_run_context(session, run, steps)
                sync_batch()
                return
            if step.status not in STEP_TERMINAL:
                step.status = STEP_CANCELLED
                step.error_json = _dump_json({"code": "workflow_cancelled", "message": "工作流已取消"})
                step.next_run_at = None
                step.timeout_at = None
                step.finished_at = now
                step.updated_at = now
                session.add(step)
        _finish_run(session, run, status=RUN_CANCELLED, error="工作流已取消", steps=steps)
        sync_batch()
        return

    failed = next((step for step in steps if step.status == STEP_FAILED), None)
    if failed:
        error = str((failed.get_error() or {}).get("message") or "工作流步骤失败")
        for step in steps:
            if step.status not in STEP_TERMINAL:
                step.status = STEP_CANCELLED
                step.error_json = _dump_json({"code": "dependency_failed", "message": "依赖步骤失败"})
                step.next_run_at = None
                step.timeout_at = None
                step.finished_at = now
                step.updated_at = now
                session.add(step)
        _finish_run(session, run, status=RUN_FAILED, error=error, steps=steps)
        sync_batch()
        return

    if all(status in {STEP_SUCCEEDED, STEP_SKIPPED} for status in statuses):
        _finish_run(session, run, status=RUN_SUCCEEDED, steps=steps)
        sync_batch()
        return

    if any(status == STEP_NEEDS_ATTENTION for status in statuses):
        run.status = RUN_NEEDS_ATTENTION
    elif any(status == STEP_RUNNING or status == STEP_READY for status in statuses):
        run.status = RUN_RUNNING
    elif any(status == STEP_RETRY_SCHEDULED for status in statuses):
        run.status = RUN_RETRY_SCHEDULED
    elif any(status == STEP_WAITING_EXTERNAL for status in statuses):
        run.status = RUN_WAITING_EXTERNAL
    else:
        run.status = RUN_PENDING
    if run.status != RUN_PENDING or any(status != STEP_PENDING for status in statuses):
        run.started_at = run.started_at or now
    run.updated_at = now
    _set_run_context(session, run, steps)
    session.add(run)
    sync_batch()


def create_workflow_run(
    *,
    definition_key: str,
    inputs: dict[str, Any] | None = None,
    version: int = 0,
    name: str = "",
    batch_id: str = "",
    batch_item_index: int = 0,
    metadata: dict[str, Any] | None = None,
    activate: bool = True,
) -> dict[str, Any] | None:
    with Session(engine) as session:
        definition_model = _get_definition_model(session, definition_key, version=version)
        if not definition_model:
            return None
        definition = definition_model.get_definition()
        run_id = f"wf_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        run = WorkflowRunModel(
            id=run_id,
            batch_id=str(batch_id or ""),
            batch_item_index=max(int(batch_item_index or 0), 0),
            definition_key=definition_model.key,
            definition_version=int(definition_model.version or 1),
            name=str(name or definition_model.name),
            status=RUN_PENDING,
            input_json=_dump_json(inputs or {}),
            context_json=_dump_json({"workflow": {"inputs": inputs or {}}, "steps": {}}),
            output_json="{}",
            definition_json=_dump_json(definition),
            metadata_json=_dump_json(metadata or {}),
            started_at=None,
        )
        session.add(run)
        session.flush()
        for spec in _step_defs(definition):
            step = WorkflowStepRunModel(
                id=f"{run_id}_{spec['id']}",
                workflow_run_id=run_id,
                step_id=spec["id"],
                name=str(spec.get("name") or spec["id"]),
                adapter_key=str(spec.get("uses") or ""),
                status=STEP_PENDING,
                max_attempts=max(int(spec.get("max_attempts") or 1), 1),
                idempotency_key=f"{run_id}_{spec['id']}",
            )
            session.add(step)
        session.flush()
        _append_event_in_session(
            session,
            run.id,
            f"工作流已创建: {run.name}",
            event_type="state",
            detail={"definition_key": run.definition_key, "definition_version": run.definition_version},
        )
        if activate:
            _promote_ready_steps(session, run)
            _refresh_run_status(session, run)
        session.commit()
        steps = sorted(_steps_for_run(session, run.id), key=lambda item: _step_order(definition).get(item.step_id, 0))
        session.refresh(run)
        return _serialize_run(run, steps=steps)


def create_workflow_batch(
    *,
    definition_key: str,
    items: list[dict[str, Any]],
    version: int = 0,
    name: str = "",
    concurrency: int = 1,
) -> dict[str, Any] | None:
    normalized_items: list[dict[str, Any]] = []
    for index, raw in enumerate(items or []):
        item = raw if isinstance(raw, dict) else {}
        item_input = item.get("input") if isinstance(item.get("input"), dict) else item
        normalized_items.append(
            {
                "input": item_input if isinstance(item_input, dict) else {},
                "name": str(item.get("name") or ""),
                "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                "index": index + 1,
            }
        )
    if not normalized_items:
        raise ValueError("批量启动至少需要一个输入项")
    if len(normalized_items) > 200:
        raise ValueError("单次批量启动最多 200 个工作流")
    concurrency = min(max(int(concurrency or 1), 1), 50)

    with Session(engine) as session:
        definition_model = _get_definition_model(session, definition_key, version=version)
        if not definition_model:
            return None
        batch_id = f"wfb_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        batch = WorkflowBatchModel(
            id=batch_id,
            definition_key=definition_model.key,
            definition_version=int(definition_model.version or 1),
            name=str(name or f"{definition_model.name} 批量"),
            status=RUN_PENDING,
            total=len(normalized_items),
            concurrency=concurrency,
            input_json=_dump_json({"items": [item["input"] for item in normalized_items]}),
        )
        session.add(batch)
        session.commit()

    created_runs: list[dict[str, Any]] = []
    for item in normalized_items:
        run = create_workflow_run(
            definition_key=definition_key,
            version=version,
            name=str(item["name"] or f"{name or definition_key} #{item['index']}"),
            inputs=dict(item["input"]),
            batch_id=batch_id,
            batch_item_index=int(item["index"]),
            metadata=dict(item["metadata"]),
            activate=False,
        )
        if run:
            created_runs.append(run)

    with Session(engine) as session:
        batch = session.get(WorkflowBatchModel, batch_id)
        if not batch:
            return None
        runs = session.exec(
            select(WorkflowRunModel)
            .where(WorkflowRunModel.batch_id == batch_id)
            .order_by(WorkflowRunModel.batch_item_index, WorkflowRunModel.created_at)
        ).all()
        for run in runs:
            _promote_ready_steps(session, run)
            _refresh_run_status(session, run)
        batch.updated_at = _utcnow()
        session.add(batch)
        session.commit()
        payload = _serialize_batch(session, batch, include_runs=True)
        payload["runs"] = [
            _serialize_run(
                run,
                steps=sorted(
                    _steps_for_run(session, run.id),
                    key=lambda item: _step_order(run.get_definition()).get(item.step_id, 0),
                ),
                include_definition=False,
            )
            for run in runs
        ] or created_runs
        return payload


def list_workflow_runs(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str = "",
    definition_key: str = "",
    batch_id: str = "",
) -> dict[str, Any]:
    limit = min(max(int(limit or 50), 1), 100)
    offset = max(int(offset or 0), 0)
    with Session(engine) as session:
        query = select(WorkflowRunModel)
        count_query = select(func.count()).select_from(WorkflowRunModel)
        if status:
            query = query.where(WorkflowRunModel.status == status)
            count_query = count_query.where(WorkflowRunModel.status == status)
        if definition_key:
            query = query.where(WorkflowRunModel.definition_key == definition_key)
            count_query = count_query.where(WorkflowRunModel.definition_key == definition_key)
        if batch_id:
            query = query.where(WorkflowRunModel.batch_id == batch_id)
            count_query = count_query.where(WorkflowRunModel.batch_id == batch_id)
        items = session.exec(
            query.order_by(WorkflowRunModel.created_at.desc(), WorkflowRunModel.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        total = int(session.exec(count_query).one() or 0)
        running = int(
            session.exec(
                select(func.count())
                .select_from(WorkflowRunModel)
                .where(WorkflowRunModel.status.notin_(list(RUN_TERMINAL)))
            ).one()
            or 0
        )
        return {
            "items": [_serialize_run(item, include_definition=False) for item in items],
            "total": total,
            "running": running,
            "limit": limit,
            "offset": offset,
        }


def list_workflow_batches(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str = "",
    definition_key: str = "",
) -> dict[str, Any]:
    limit = min(max(int(limit or 50), 1), 100)
    offset = max(int(offset or 0), 0)
    with Session(engine) as session:
        query = select(WorkflowBatchModel)
        count_query = select(func.count()).select_from(WorkflowBatchModel)
        if status:
            query = query.where(WorkflowBatchModel.status == status)
            count_query = count_query.where(WorkflowBatchModel.status == status)
        if definition_key:
            query = query.where(WorkflowBatchModel.definition_key == definition_key)
            count_query = count_query.where(WorkflowBatchModel.definition_key == definition_key)
        items = session.exec(
            query.order_by(WorkflowBatchModel.created_at.desc(), WorkflowBatchModel.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        total = int(session.exec(count_query).one() or 0)
        return {
            "items": [_serialize_batch(session, item) for item in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


def get_workflow_batch(batch_id: str, *, include_runs: bool = True) -> dict[str, Any] | None:
    with Session(engine) as session:
        batch = session.get(WorkflowBatchModel, batch_id)
        if not batch:
            return None
        return _serialize_batch(session, batch, include_runs=include_runs)


def get_workflow_run(run_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return None
        definition = run.get_definition()
        order = _step_order(definition)
        steps = sorted(_steps_for_run(session, run.id), key=lambda item: order.get(item.step_id, 0))
        return _serialize_run(run, steps=steps)


def get_workflow_run_summary(run_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return None
        definition = run.get_definition()
        order = _step_order(definition)
        steps = sorted(_steps_for_run(session, run.id), key=lambda item: order.get(item.step_id, 0))
        batch = session.get(WorkflowBatchModel, run.batch_id) if run.batch_id else None
        return _run_summary_with_batch_state(
            run,
            steps,
            batch_paused=bool(batch and batch.status == RUN_PAUSED),
        )


def get_workflow_batch_summary(batch_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        batch = session.get(WorkflowBatchModel, batch_id)
        if not batch:
            return None
        runs = session.exec(
            select(WorkflowRunModel)
            .where(WorkflowRunModel.batch_id == batch.id)
            .order_by(WorkflowRunModel.batch_item_index, WorkflowRunModel.created_at)
        ).all()
        summaries = []
        counts = _status_counts(runs)
        status = _batch_status_from_counts(counts)
        batch_paused = bool(batch.status == RUN_PAUSED and status not in RUN_TERMINAL)
        for run in runs:
            definition = run.get_definition()
            order = _step_order(definition)
            steps = sorted(_steps_for_run(session, run.id), key=lambda item: order.get(item.step_id, 0))
            summaries.append(_run_summary_with_batch_state(run, steps, batch_paused=batch_paused))
        if batch.status == RUN_PAUSED and status not in RUN_TERMINAL:
            status = RUN_PAUSED
        return {
            "id": batch.id,
            "definition_key": batch.definition_key,
            "definition_version": int(batch.definition_version or 1),
            "name": batch.name,
            "status": status,
            "terminal": status in RUN_TERMINAL,
            "total": int(batch.total or counts.get("total") or 0),
            "concurrency": int(batch.concurrency or 1),
            "summary": counts,
            "observability": _batch_observability(runs, summaries),
            "runs": summaries,
            "created_at": serialize_datetime(batch.created_at),
            "updated_at": serialize_datetime(batch.updated_at),
        }


def list_workflow_events(
    run_id: str,
    *,
    since: int = 0,
    before: int = 0,
    limit: int = 200,
    latest: bool = False,
) -> dict[str, Any] | None:
    limit = min(max(int(limit or 200), 1), 1000)
    since = max(int(since or 0), 0)
    before = max(int(before or 0), 0)
    with Session(engine) as session:
        if not session.get(WorkflowRunModel, run_id):
            return None
        if before:
            query = (
                select(WorkflowEventModel)
                .where(WorkflowEventModel.workflow_run_id == run_id)
                .where(WorkflowEventModel.id < before)
                .order_by(WorkflowEventModel.id.desc())
                .limit(limit + 1)
            )
            raw_items = session.exec(query).all()
            has_more_before = len(raw_items) > limit
            items = list(reversed(raw_items[:limit]))
        elif latest:
            query = (
                select(WorkflowEventModel)
                .where(WorkflowEventModel.workflow_run_id == run_id)
                .order_by(WorkflowEventModel.id.desc())
                .limit(limit + 1)
            )
            raw_items = session.exec(query).all()
            has_more_before = len(raw_items) > limit
            items = list(reversed(raw_items[:limit]))
        else:
            items = session.exec(
                select(WorkflowEventModel)
                .where(WorkflowEventModel.workflow_run_id == run_id)
                .where(WorkflowEventModel.id > since)
                .order_by(WorkflowEventModel.id)
                .limit(limit)
            ).all()
            has_more_before = False
        serialized = [_serialize_event(item) for item in items]
        return {
            "items": serialized,
            "cursor": max([int(item["id"] or 0) for item in serialized] or [since]),
            "before": min([int(item["id"] or 0) for item in serialized] or [before]),
            "has_more_before": has_more_before,
        }


def _claim_next_due_step() -> dict[str, Any] | None:
    with Session(engine) as session:
        active_runs = session.exec(
            select(WorkflowRunModel).where(WorkflowRunModel.status.notin_(list(RUN_TERMINAL)))
        ).all()
        active_runs = sorted(active_runs, key=lambda item: _run_promotion_sort_key(session, item))
        for run in active_runs:
            _promote_ready_steps(session, run)
            _refresh_run_status(session, run)
        session.commit()

    now = _utcnow()
    with Session(engine) as session:
        candidates = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.status.in_([STEP_READY, STEP_RETRY_SCHEDULED, STEP_WAITING_EXTERNAL]))
            .order_by(WorkflowStepRunModel.next_run_at, WorkflowStepRunModel.created_at)
        ).all()
        candidates = sorted(candidates, key=lambda item: _step_claim_sort_key(session, item))
        for step in candidates:
            if not _due(step.next_run_at, now):
                continue
            run = session.get(WorkflowRunModel, step.workflow_run_id)
            if not run or run.status in RUN_TERMINAL:
                continue
            if run.cancellation_requested_at:
                _refresh_run_status(session, run)
                session.commit()
                return {"cancelled": True}
            if not _batch_can_claim_step(session, run, step, now=now):
                continue
            if not _step_can_claim_by_limits(session, run, step):
                continue
            if step.timeout_at and ensure_utc_datetime(step.timeout_at) and ensure_utc_datetime(step.timeout_at) <= now:
                transition = StepTransition.failed("步骤执行超时", code="step_timeout", retryable=True)
                _apply_step_transition_in_session(session, run, step, transition)
                session.commit()
                return {"timed_out": True}

            previous_status = step.status
            external_ref = step.external_ref
            if previous_status in {STEP_READY, STEP_RETRY_SCHEDULED}:
                if previous_status == STEP_RETRY_SCHEDULED or int(step.attempt or 0) <= 0:
                    step.attempt = int(step.attempt or 0) + 1
                if previous_status == STEP_RETRY_SCHEDULED:
                    external_ref = ""
                    step.external_ref = ""
                if not _load_json(step.input_json):
                    definition = run.get_definition()
                    spec = _step_def_map(definition).get(step.step_id) or {}
                    context = _build_context(run, _steps_for_run(session, run.id))
                    step.input_json = _dump_json(resolve_value(spec.get("input") or {}, context))
                timeout_seconds = parse_duration_seconds(
                    (_step_def_map(run.get_definition()).get(step.step_id) or {}).get("timeout"),
                    default=0,
                )
                step.timeout_at = now + timedelta(seconds=timeout_seconds) if timeout_seconds else None
            step.status = STEP_RUNNING
            step.next_run_at = None
            step.started_at = step.started_at or now
            step.updated_at = now
            run.status = RUN_RUNNING
            run.current_step_id = step.step_id
            run.started_at = run.started_at or now
            run.finished_at = None
            run.updated_at = now
            session.add(step)
            session.add(run)
            session.commit()
            return {
                "run_id": run.id,
                "step_id": step.step_id,
                "adapter_key": step.adapter_key,
                "input": step.get_input(),
                "external_ref": external_ref,
                "idempotency_key": step.idempotency_key,
                "attempt": int(step.attempt or 1),
            }
    return None


def _apply_step_transition_in_session(
    session: Session,
    run: WorkflowRunModel,
    step: WorkflowStepRunModel,
    transition: StepTransition,
) -> None:
    now = _utcnow()
    previous_status = step.status
    message = transition.message
    if run.cancellation_requested_at:
        step.status = STEP_CANCELLED
        step.error_json = _dump_json({"code": "workflow_cancelled", "message": "工作流已取消"})
        step.next_run_at = None
        step.timeout_at = None
        step.finished_at = now
        step.updated_at = now
        session.add(step)
        _append_event_in_session(
            session,
            run.id,
            "步骤已取消",
            step_id=step.step_id,
            event_type="state",
            level="warning",
        )
        _refresh_run_status(session, run)
        return

    if transition.status == STEP_WAITING_EXTERNAL:
        step.status = STEP_WAITING_EXTERNAL
        step.output_json = _dump_json(transition.output)
        step.error_json = "{}"
        step.external_ref = transition.external_ref or step.external_ref
        step.next_run_at = transition.next_run_at or (now + timedelta(seconds=1))
        step.updated_at = now
        session.add(step)
        if previous_status != STEP_WAITING_EXTERNAL:
            _append_event_in_session(
                session,
                run.id,
                message or "步骤等待外部结果",
                step_id=step.step_id,
                event_type="state",
                detail={"external_ref": step.external_ref},
            )
        _refresh_run_status(session, run)
        return

    if transition.status == STEP_FAILED:
        step.error_json = _dump_json(transition.error)
        step.output_json = _dump_json(transition.output)
        step.next_run_at = None
        step.timeout_at = None
        step.updated_at = now
        if transition.retryable and int(step.attempt or 0) < int(step.max_attempts or 1):
            delay_seconds = parse_duration_seconds(
                (_step_def_map(run.get_definition()).get(step.step_id) or {}).get("retry_delay"),
                default=30,
            )
            step.status = STEP_RETRY_SCHEDULED
            step.next_run_at = now + timedelta(seconds=delay_seconds)
            _append_event_in_session(
                session,
                run.id,
                message or "步骤失败，将自动重试",
                step_id=step.step_id,
                event_type="state",
                level="warning",
                detail={"retry_at": serialize_datetime(step.next_run_at), "attempt": step.attempt},
            )
        else:
            step_spec = _step_def_map(run.get_definition()).get(step.step_id) or {}
            on_failure = str(step_spec.get("on_failure") or FAILURE_POLICY_FAIL)
            if on_failure == FAILURE_POLICY_NEEDS_ATTENTION:
                step.status = STEP_NEEDS_ATTENTION
                step.finished_at = None
                _append_event_in_session(
                    session,
                    run.id,
                    message or "步骤失败，已转人工处理",
                    step_id=step.step_id,
                    event_type="state",
                    level="warning",
                    detail=transition.error,
                )
            elif on_failure == FAILURE_POLICY_SKIP:
                step.status = STEP_SKIPPED
                step.finished_at = now
                _append_event_in_session(
                    session,
                    run.id,
                    message or "步骤失败，按策略跳过",
                    step_id=step.step_id,
                    event_type="state",
                    level="warning",
                    detail=transition.error,
                )
                _promote_ready_steps(session, run)
            else:
                step.status = STEP_FAILED
                step.finished_at = now
                _append_event_in_session(
                    session,
                    run.id,
                    message or "步骤失败",
                    step_id=step.step_id,
                    event_type="state",
                    level="error",
                    detail=transition.error,
                )
        session.add(step)
        _refresh_run_status(session, run)
        return

    if transition.status == STEP_NEEDS_ATTENTION:
        step.status = STEP_NEEDS_ATTENTION
        step.error_json = _dump_json(transition.error)
        step.output_json = _dump_json(transition.output)
        step.next_run_at = None
        step.updated_at = now
        session.add(step)
        _append_event_in_session(
            session,
            run.id,
            message or "步骤需要人工处理",
            step_id=step.step_id,
            event_type="state",
            level="warning",
            detail=transition.error,
        )
        _refresh_run_status(session, run)
        return

    if transition.status in {STEP_SUCCEEDED, STEP_SKIPPED, STEP_CANCELLED}:
        step.status = transition.status
        step.output_json = _dump_json(transition.output)
        step.error_json = _dump_json(transition.error)
        step.external_ref = transition.external_ref or step.external_ref
        step.next_run_at = None
        step.timeout_at = None
        step.finished_at = now
        step.updated_at = now
        session.add(step)
        _append_event_in_session(
            session,
            run.id,
            message or ("步骤已跳过" if transition.status == STEP_SKIPPED else "步骤已完成"),
            step_id=step.step_id,
            event_type="state",
            level="warning" if transition.status == STEP_CANCELLED else "info",
            detail=transition.output,
        )
        _promote_ready_steps(session, run)
        _refresh_run_status(session, run)
        return

    step.status = STEP_FAILED
    step.error_json = _dump_json({"code": "invalid_transition", "message": f"未知步骤状态: {transition.status}"})
    step.finished_at = now
    step.updated_at = now
    session.add(step)
    _refresh_run_status(session, run)


def apply_step_transition(run_id: str, step_id: str, transition: StepTransition) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        step = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.workflow_run_id == run_id)
            .where(WorkflowStepRunModel.step_id == step_id)
        ).first()
        if not run or not step:
            return None
        _apply_step_transition_in_session(session, run, step, transition)
        session.commit()
        session.refresh(run)
        return get_workflow_run(run.id)


def run_due_workflow_once() -> bool:
    with _workflow_claim_lock:
        claim = _claim_next_due_step()
    if not claim:
        return False
    if claim.get("cancelled") or claim.get("timed_out"):
        return True

    try:
        adapter = get_step_adapter(str(claim["adapter_key"]))
        if claim.get("external_ref"):
            transition = adapter.resume(
                inputs=dict(claim.get("input") or {}),
                external_ref=str(claim.get("external_ref") or ""),
                attempt=int(claim.get("attempt") or 1),
            )
        else:
            transition = adapter.start(
                inputs=dict(claim.get("input") or {}),
                idempotency_key=str(claim.get("idempotency_key") or ""),
                attempt=int(claim.get("attempt") or 1),
            )
    except Exception as exc:  # noqa: BLE001 - adapter failures become workflow state.
        transition = StepTransition.failed(str(exc), code="adapter_exception")
    apply_step_transition(str(claim["run_id"]), str(claim["step_id"]), transition)
    return True


def recover_incomplete_workflow_runs() -> None:
    now = _utcnow()
    with Session(engine) as session:
        runs = session.exec(
            select(WorkflowRunModel).where(WorkflowRunModel.status.notin_(list(RUN_TERMINAL)))
        ).all()
        for run in runs:
            run.finished_at = None
            for step in _steps_for_run(session, run.id):
                if step.status == STEP_RUNNING:
                    step.status = STEP_WAITING_EXTERNAL if step.external_ref else STEP_READY
                    step.next_run_at = now
                    step.updated_at = now
                    session.add(step)
                    _append_event_in_session(
                        session,
                        run.id,
                        "步骤在服务重启后恢复调度",
                        step_id=step.step_id,
                        event_type="state",
                        level="warning",
                    )
            _promote_ready_steps(session, run)
            _refresh_run_status(session, run)
        session.commit()


def cancel_workflow_run(run_id: str) -> dict[str, Any] | None:
    cancel_calls: list[tuple[str, dict[str, Any], str]] = []
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return None
        if run.status in RUN_TERMINAL:
            return _serialize_run(run, steps=_steps_for_run(session, run.id))
        now = _utcnow()
        run.status = RUN_CANCEL_REQUESTED
        run.cancellation_requested_at = run.cancellation_requested_at or now
        run.updated_at = now
        for step in _steps_for_run(session, run.id):
            if step.status == STEP_WAITING_EXTERNAL and step.external_ref:
                cancel_calls.append((step.adapter_key, step.get_input(), step.external_ref))
            if step.status in STEP_TERMINAL or step.status == STEP_RUNNING:
                continue
            step.status = STEP_CANCELLED
            step.error_json = _dump_json({"code": "workflow_cancelled", "message": "工作流已取消"})
            step.next_run_at = None
            step.timeout_at = None
            step.finished_at = now
            step.updated_at = now
            session.add(step)
        session.add(run)
        _append_event_in_session(session, run.id, "已请求取消工作流", event_type="state", level="warning")
        _refresh_run_status(session, run)
        session.commit()

    for adapter_key, inputs, external_ref in cancel_calls:
        try:
            get_step_adapter(adapter_key).cancel(inputs=inputs, external_ref=external_ref)
        except Exception:
            pass
    return get_workflow_run(run_id)


def _descendant_step_ids(definition: dict[str, Any], step_id: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for spec in _step_defs(definition):
        for need in spec.get("needs") or []:
            children.setdefault(str(need), []).append(str(spec.get("id") or ""))
    result: set[str] = set()
    stack = list(children.get(step_id, []))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(children.get(current, []))
    return result


def retry_workflow_step(run_id: str, step_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        step = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.workflow_run_id == run_id)
            .where(WorkflowStepRunModel.step_id == step_id)
        ).first()
        if not run or not step:
            return None
        if step.status not in {STEP_FAILED, STEP_NEEDS_ATTENTION, STEP_CANCELLED}:
            raise ValueError("只有失败、已取消或需人工处理的步骤可以人工重试")
        now = _utcnow()
        descendants = _descendant_step_ids(run.get_definition(), step_id)
        for item in _steps_for_run(session, run.id):
            if item.step_id not in descendants:
                continue
            item.status = STEP_PENDING
            item.attempt = 0
            item.input_json = "{}"
            item.output_json = "{}"
            item.error_json = "{}"
            item.external_ref = ""
            item.next_run_at = None
            item.timeout_at = None
            item.started_at = None
            item.finished_at = None
            item.updated_at = now
            session.add(item)
        step.status = STEP_RETRY_SCHEDULED
        step.max_attempts = max(int(step.max_attempts or 1), int(step.attempt or 0) + 1)
        step.error_json = "{}"
        step.output_json = "{}"
        step.external_ref = ""
        step.next_run_at = now
        step.timeout_at = None
        step.finished_at = None
        step.updated_at = now
        run.status = RUN_RETRY_SCHEDULED
        run.error = ""
        run.finished_at = None
        run.cancellation_requested_at = None
        run.updated_at = now
        session.add(step)
        session.add(run)
        _append_event_in_session(
            session,
            run.id,
            "步骤已安排人工重试",
            step_id=step.step_id,
            event_type="state",
            level="warning",
        )
        _refresh_run_status(session, run)
        session.commit()
        return get_workflow_run(run.id)


def update_workflow_step_input(run_id: str, step_id: str, inputs: dict[str, Any]) -> dict[str, Any] | None:
    with Session(engine) as session:
        run = session.get(WorkflowRunModel, run_id)
        step = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.workflow_run_id == run_id)
            .where(WorkflowStepRunModel.step_id == step_id)
        ).first()
        if not run or not step:
            return None
        if step.status in {STEP_RUNNING, STEP_WAITING_EXTERNAL}:
            raise ValueError("运行中或等待外部结果的步骤不能直接修改输入")
        step.input_json = _dump_json(inputs)
        step.updated_at = _utcnow()
        session.add(step)
        _append_event_in_session(
            session,
            run.id,
            "步骤输入已更新",
            step_id=step.step_id,
            event_type="state",
            level="warning",
        )
        _set_run_context(session, run, _steps_for_run(session, run.id))
        session.commit()
        return get_workflow_run(run.id)


def pause_workflow_batch(batch_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        batch = session.get(WorkflowBatchModel, batch_id)
        if not batch:
            return None
        runs = session.exec(select(WorkflowRunModel).where(WorkflowRunModel.batch_id == batch_id)).all()
        computed_status = _batch_status_from_counts(_status_counts(runs))
        if computed_status in RUN_TERMINAL:
            batch.status = computed_status
            batch.updated_at = _utcnow()
            session.add(batch)
            session.commit()
            return _serialize_batch(session, batch, include_runs=True)
        batch.status = RUN_PAUSED
        batch.updated_at = _utcnow()
        session.add(batch)
        for run in runs:
            if run.status in RUN_TERMINAL:
                continue
            _append_event_in_session(session, run.id, "批次已暂停，后续步骤暂不调度", event_type="state", level="warning")
        session.commit()
        return _serialize_batch(session, batch, include_runs=True)


def resume_workflow_batch(batch_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        batch = session.get(WorkflowBatchModel, batch_id)
        if not batch:
            return None
        runs = session.exec(select(WorkflowRunModel).where(WorkflowRunModel.batch_id == batch_id)).all()
        batch.status = _batch_status_from_counts(_status_counts(runs))
        batch.updated_at = _utcnow()
        session.add(batch)
        for run in runs:
            if run.status in RUN_TERMINAL:
                continue
            _append_event_in_session(session, run.id, "批次已恢复，等待调度器继续执行", event_type="state")
        session.commit()
        return _serialize_batch(session, batch, include_runs=True)


def cancel_workflow_batch(batch_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        batch = session.get(WorkflowBatchModel, batch_id)
        if not batch:
            return None
        batch.status = RUN_CANCEL_REQUESTED
        batch.updated_at = _utcnow()
        session.add(batch)
        run_ids = [
            run.id
            for run in session.exec(select(WorkflowRunModel).where(WorkflowRunModel.batch_id == batch_id)).all()
            if run.status not in RUN_TERMINAL
        ]
        session.commit()

    for run_id in run_ids:
        cancel_workflow_run(run_id)
    return get_workflow_batch_summary(batch_id)


def retry_failed_workflow_batch(batch_id: str) -> dict[str, Any] | None:
    retry_targets: list[tuple[str, str]] = []
    with Session(engine) as session:
        batch = session.get(WorkflowBatchModel, batch_id)
        if not batch:
            return None
        runs = session.exec(
            select(WorkflowRunModel)
            .where(WorkflowRunModel.batch_id == batch_id)
            .order_by(WorkflowRunModel.batch_item_index, WorkflowRunModel.created_at)
        ).all()
        for run in runs:
            if run.status not in {RUN_FAILED, RUN_NEEDS_ATTENTION, RUN_CANCELLED}:
                continue
            order = _step_order(run.get_definition())
            steps = sorted(_steps_for_run(session, run.id), key=lambda item: order.get(item.step_id, 0))
            target = next(
                (step for step in steps if step.status in {STEP_FAILED, STEP_NEEDS_ATTENTION, STEP_CANCELLED}),
                None,
            )
            if target:
                retry_targets.append((run.id, target.step_id))

    retried = 0
    for run_id, step_id in retry_targets:
        if retry_workflow_step(run_id, step_id):
            retried += 1
    summary = get_workflow_batch_summary(batch_id)
    if summary is not None:
        summary["retried"] = retried
    return summary
