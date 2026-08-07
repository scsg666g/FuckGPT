from __future__ import annotations

from datetime import timedelta
import threading
import time

import pytest
from sqlmodel import Session, select

from application.tasks import TASK_STATUS_SUCCEEDED, TASK_TYPE_CODEX_OAUTH_BATCH, create_task
from application.workflow_adapters import CodexAuthorizeAdapter, register_builtin_workflow_components
from application.workflow_registry import register_step_adapter, registered_workflow_definitions
from application.workflows import (
    cancel_workflow_batch,
    cancel_workflow_run,
    create_or_update_workflow_definition,
    create_workflow_batch,
    create_workflow_run,
    get_workflow_batch_summary,
    get_workflow_run,
    get_workflow_run_summary,
    list_workflow_runs,
    pause_workflow_batch,
    recover_incomplete_workflow_runs,
    resume_workflow_batch,
    retry_failed_workflow_batch,
    retry_workflow_step,
    run_due_workflow_once,
    update_workflow_step_input,
    validate_workflow_definition,
)
from core.db import TaskModel, WorkflowRunModel, WorkflowStepRunModel, engine
from domain.workflows import (
    ERROR_CONFIG,
    ERROR_NETWORK,
    ERROR_OPERATOR_REQUIRED,
    RUN_CANCELLED,
    RUN_NEEDS_ATTENTION,
    RUN_PENDING,
    RUN_PAUSED,
    RUN_RETRY_SCHEDULED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_FAILED,
    STEP_NEEDS_ATTENTION,
    STEP_PENDING,
    STEP_READY,
    STEP_RETRY_SCHEDULED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    STEP_WAITING_EXTERNAL,
    StepAdapter,
    StepTransition,
    evaluate_condition,
    utcnow,
)
from services.workflow_runtime import WorkflowRuntime


class _ImmediateAdapter(StepAdapter):
    key = "test.immediate"

    def start(self, *, inputs, idempotency_key, attempt):
        return StepTransition.succeeded(
            {"inputs": inputs, "attempt": attempt, "idempotency_key": idempotency_key}
        )

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


class _WaitingAdapter(StepAdapter):
    key = "test.wait"
    cancelled_refs: list[str] = []

    def start(self, *, inputs, idempotency_key, attempt):
        return StepTransition(
            STEP_WAITING_EXTERNAL,
            external_ref=f"external-{idempotency_key}",
            output={"started": True},
            next_run_at=utcnow(),
            message="等待外部确认",
        )

    def resume(self, *, inputs, external_ref, attempt):
        return StepTransition.succeeded({"external_ref": external_ref, "attempt": attempt})

    def cancel(self, *, inputs, external_ref):
        self.cancelled_refs.append(external_ref)


class _HoldingAdapter(StepAdapter):
    key = "test.hold"

    def start(self, *, inputs, idempotency_key, attempt):
        return StepTransition(
            STEP_WAITING_EXTERNAL,
            external_ref=f"hold-{idempotency_key}",
            output={"started": True},
            next_run_at=utcnow() + timedelta(minutes=5),
            message="等待外部确认",
        )

    def resume(self, *, inputs, external_ref, attempt):
        return StepTransition.succeeded({"external_ref": external_ref, "attempt": attempt})


class _ConcurrentBlockingAdapter(StepAdapter):
    key = "test.concurrent_block"
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.active = 0
            cls.max_active = 0
        cls.entered.clear()
        cls.release.clear()

    def start(self, *, inputs, idempotency_key, attempt):
        with self.lock:
            self.__class__.active += 1
            self.__class__.max_active = max(self.__class__.max_active, self.__class__.active)
            if self.__class__.active >= 2:
                self.__class__.entered.set()
        self.__class__.release.wait(timeout=2)
        with self.lock:
            self.__class__.active -= 1
        return StepTransition.succeeded({"idempotency_key": idempotency_key, "attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


class _FlakyAdapter(StepAdapter):
    key = "test.flaky"

    def start(self, *, inputs, idempotency_key, attempt):
        if attempt == 1:
            return StepTransition.failed("temporary", code="temporary", retryable=True)
        return StepTransition.succeeded({"attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


class _FailingAdapter(StepAdapter):
    key = "test.fail"

    def start(self, *, inputs, idempotency_key, attempt):
        return StepTransition.failed("permanent failure", code="permanent_failure")

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


class _AttentionAdapter(StepAdapter):
    key = "test.attention"

    def start(self, *, inputs, idempotency_key, attempt):
        if not inputs.get("value"):
            return StepTransition.needs_attention("缺少 value", code="value_missing")
        return StepTransition.succeeded({"value": inputs["value"], "attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


def _register_test_adapters() -> None:
    for adapter in (_ImmediateAdapter(), _WaitingAdapter(), _HoldingAdapter(), _FlakyAdapter(), _FailingAdapter(), _AttentionAdapter()):
        register_step_adapter(adapter)


def _definition(key: str, steps: list[dict]) -> dict:
    return {
        "key": key,
        "version": 1,
        "name": key,
        "steps": steps,
    }


def test_condition_dsl_supports_safe_ops():
    context = {"workflow": {"inputs": {"enabled": True, "mode": "plus", "count": 2}}}

    assert evaluate_condition({"path": "workflow.inputs.enabled", "op": "truthy"}, context)
    assert evaluate_condition({"path": "workflow.inputs.mode", "op": "eq", "value": "plus"}, context)
    assert evaluate_condition({"path": "workflow.inputs.mode", "op": "in", "value": ["plus"]}, context)
    assert evaluate_condition(
        {
            "all": [
                {"path": "workflow.inputs.enabled", "op": "truthy"},
                {"not": {"path": "workflow.inputs.count", "op": "eq", "value": 3}},
            ]
        },
        context,
    )


def test_builtin_workflow_template_exposes_proxy_inputs():
    register_builtin_workflow_components()
    definition = next(item for item in registered_workflow_definitions() if item["key"] == "register_codex_push")

    registration = definition["sample_input"]["registration"]
    assert registration["executor_type"] == "browser"
    assert registration["extra"]["browser_visible"] is False
    assert definition["sample_input"]["registration"]["platform_proxy_mode"] == "direct"
    assert definition["sample_input"]["registration"]["platform_proxy_value"] == ""
    assert definition["sample_input"]["codex"]["oauth_mode"] == "browser"
    assert definition["sample_input"]["codex"]["platform_proxy_mode"] == "direct"
    assert definition["sample_input"]["codex"]["platform_proxy_value"] == ""

    field_paths = {
        field["path"]
        for section in definition["ui_schema"]["sections"]
        for field in section["fields"]
    }
    assert "registration.platform_proxy_mode" in field_paths
    assert "registration.platform_proxy_value" in field_paths
    assert "registration.extra.browser_visible" in field_paths
    assert "codex.oauth_mode" in field_paths
    assert "codex.platform_proxy_mode" in field_paths
    assert "codex.platform_proxy_value" in field_paths

    executor_field = next(
        field
        for section in definition["ui_schema"]["sections"]
        for field in section["fields"]
        if field["path"] == "registration.executor_type"
    )
    assert [option["value"] for option in executor_field["options"]] == [
        "browser_protocol",
        "browser",
    ]
    codex_mode_field = next(
        field
        for section in definition["ui_schema"]["sections"]
        for field in section["fields"]
        if field["path"] == "codex.oauth_mode"
    )
    assert [option["value"] for option in codex_mode_field["options"]] == [
        "browser",
        "browser_protocol",
        "protocol",
    ]


def test_step_transition_adds_error_category_and_operator_hint():
    failed = StepTransition.failed("缺少 target", code="target_missing")
    assert failed.error["category"] == ERROR_CONFIG
    assert failed.error["operator_hint"]

    retryable = StepTransition.failed("temporary timeout", code="temporary", retryable=True)
    assert retryable.error["category"] == ERROR_NETWORK
    assert "自动重试" in retryable.error["operator_hint"]

    attention = StepTransition.needs_attention("需要人工确认")
    assert attention.error["category"] == ERROR_OPERATOR_REQUIRED
    assert attention.error["operator_hint"]


def test_definition_validation_rejects_cycles_and_unknown_adapters():
    _register_test_adapters()

    with pytest.raises(ValueError, match="循环依赖"):
        validate_workflow_definition(
            _definition(
                "test_cycle",
                [
                    {"id": "a", "uses": "test.immediate", "needs": ["b"]},
                    {"id": "b", "uses": "test.immediate", "needs": ["a"]},
                ],
            )
        )

    with pytest.raises(ValueError, match="未注册 adapter"):
        validate_workflow_definition(
            _definition("test_unknown", [{"id": "a", "uses": "test.missing"}])
        )


def test_run_executes_steps_and_skips_false_condition():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_skip",
            [
                {
                    "id": "first",
                    "uses": "test.immediate",
                    "input": {"value": {"$path": "workflow.inputs.value"}},
                },
                {
                    "id": "second",
                    "uses": "test.immediate",
                    "needs": ["first"],
                    "if": {"path": "workflow.inputs.run_second", "op": "eq", "value": True},
                },
            ],
        )
    )

    run = create_workflow_run(definition_key="test_skip", inputs={"value": 7, "run_second": False})
    assert run is not None
    assert run_due_workflow_once()

    updated = get_workflow_run(run["id"])
    assert updated["status"] == RUN_SUCCEEDED
    assert updated["steps"][0]["status"] == STEP_SUCCEEDED
    assert updated["steps"][1]["status"] == "skipped"
    assert updated["steps"][0]["output"]["inputs"]["value"] == 7


def test_waiting_external_resumes_without_occupying_a_worker():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_waiting", [{"id": "wait", "uses": "test.wait", "timeout": "1m"}])
    )
    run = create_workflow_run(definition_key="test_waiting", inputs={})

    assert run_due_workflow_once()
    waiting = get_workflow_run(run["id"])
    assert waiting["status"] == "waiting_external"
    assert waiting["steps"][0]["status"] == STEP_WAITING_EXTERNAL

    assert run_due_workflow_once()
    done = get_workflow_run(run["id"])
    assert done["status"] == RUN_SUCCEEDED
    assert done["steps"][0]["output"]["external_ref"].startswith("external-")


def test_retryable_failure_schedules_and_uses_next_attempt():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_retry",
            [{"id": "flaky", "uses": "test.flaky", "max_attempts": 2, "retry_delay": "0s"}],
        )
    )
    run = create_workflow_run(definition_key="test_retry", inputs={})

    assert run_due_workflow_once()
    retrying = get_workflow_run(run["id"])
    assert retrying["status"] == RUN_RETRY_SCHEDULED
    assert retrying["steps"][0]["status"] == STEP_RETRY_SCHEDULED

    assert run_due_workflow_once()
    done = get_workflow_run(run["id"])
    assert done["status"] == RUN_SUCCEEDED
    assert done["steps"][0]["output"]["attempt"] == 2


def test_needs_attention_can_patch_input_and_retry():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_attention",
            [{"id": "attention", "uses": "test.attention", "input": {}}],
        )
    )
    run = create_workflow_run(definition_key="test_attention", inputs={})

    assert run_due_workflow_once()
    attention = get_workflow_run(run["id"])
    assert attention["status"] == RUN_NEEDS_ATTENTION
    assert attention["steps"][0]["status"] == STEP_NEEDS_ATTENTION

    update_workflow_step_input(run["id"], "attention", {"value": 42})
    retry_workflow_step(run["id"], "attention")
    assert run_due_workflow_once()

    done = get_workflow_run(run["id"])
    assert done["status"] == RUN_SUCCEEDED
    assert done["steps"][0]["output"]["value"] == 42


def test_cancel_waiting_workflow_cancels_external_ref():
    _register_test_adapters()
    _WaitingAdapter.cancelled_refs.clear()
    create_or_update_workflow_definition(
        _definition("test_cancel", [{"id": "wait", "uses": "test.wait", "timeout": "1m"}])
    )
    run = create_workflow_run(definition_key="test_cancel", inputs={})

    assert run_due_workflow_once()
    cancelled = cancel_workflow_run(run["id"])

    assert cancelled["status"] == RUN_CANCELLED
    assert cancelled["steps"][0]["status"] == "cancelled"
    assert _WaitingAdapter.cancelled_refs


def test_workflow_batch_creates_runs_and_summaries():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_batch", [{"id": "first", "uses": "test.immediate"}])
    )

    batch = create_workflow_batch(
        definition_key="test_batch",
        concurrency=2,
        items=[
            {"input": {"value": 1}, "metadata": {"row": 1}},
            {"input": {"value": 2}, "metadata": {"row": 2}},
        ],
    )

    assert batch is not None
    assert batch["total"] == 2
    assert len(batch["runs"]) == 2
    assert {run["batch_id"] for run in batch["runs"]} == {batch["id"]}
    assert batch["runs"][0]["metadata"]["row"] == 1

    listed = list_workflow_runs(limit=10, offset=0, batch_id=batch["id"])
    assert listed["total"] == 2

    assert run_due_workflow_once()
    assert run_due_workflow_once()

    batch_summary = get_workflow_batch_summary(batch["id"])
    assert batch_summary["summary"]["succeeded"] == 2
    run_summary = get_workflow_run_summary(batch["runs"][0]["id"])
    assert run_summary["display_status"] == "工作流已完成"
    assert run_summary["operator_action"] == "无需操作"


def test_workflow_batch_sliding_window_releases_slot_for_external_wait():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_batch_concurrency", [{"id": "hold", "uses": "test.hold"}])
    )
    batch = create_workflow_batch(
        definition_key="test_batch_concurrency",
        concurrency=1,
        items=[{"input": {"value": 1}}, {"input": {"value": 2}}],
    )

    assert run_due_workflow_once()
    runs = [
        get_workflow_run(item["id"])
        for item in list_workflow_runs(limit=10, offset=0, batch_id=batch["id"])["items"]
    ]
    waiting = [run for run in runs if run["steps"][0]["status"] == STEP_WAITING_EXTERNAL]
    pending = [run for run in runs if run["steps"][0]["status"] == STEP_PENDING]
    assert len(waiting) == 1
    assert len(pending) == 1

    assert run_due_workflow_once()
    still_waiting = [
        get_workflow_run(item["id"])
        for item in list_workflow_runs(limit=10, offset=0, batch_id=batch["id"])["items"]
    ]
    assert len([run for run in still_waiting if run["steps"][0]["status"] == STEP_WAITING_EXTERNAL]) == 2
    assert not run_due_workflow_once()


def test_workflow_batch_external_waiting_limit_blocks_new_external_starts(monkeypatch):
    monkeypatch.setenv("WORKFLOW_DEFAULT_EXTERNAL_WAITING_LIMIT", "1")
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_batch_external_limit", [{"id": "hold", "uses": "test.hold"}])
    )
    batch = create_workflow_batch(
        definition_key="test_batch_external_limit",
        concurrency=2,
        items=[{"input": {"value": 1}}, {"input": {"value": 2}}],
    )

    assert run_due_workflow_once()
    assert not run_due_workflow_once()

    runs = [
        get_workflow_run(item["id"])
        for item in list_workflow_runs(limit=10, offset=0, batch_id=batch["id"])["items"]
    ]
    assert len([run for run in runs if run["steps"][0]["status"] == STEP_WAITING_EXTERNAL]) == 1
    assert len([run for run in runs if run["steps"][0]["status"] == STEP_READY]) == 1


def test_workflow_batch_long_retry_releases_slot_for_next_item():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_batch_long_retry",
            [{"id": "flaky", "uses": "test.flaky", "max_attempts": 2, "retry_delay": "5m"}],
        )
    )
    batch = create_workflow_batch(
        definition_key="test_batch_long_retry",
        concurrency=1,
        items=[{"input": {"value": 1}}, {"input": {"value": 2}}],
    )
    first_run_id = batch["runs"][0]["id"]
    second_run_id = batch["runs"][1]["id"]

    assert run_due_workflow_once()
    first_detail = get_workflow_run(first_run_id)
    second_detail = get_workflow_run(second_run_id)
    assert first_detail["steps"][0]["status"] == STEP_RETRY_SCHEDULED
    assert second_detail["steps"][0]["status"] == STEP_PENDING

    assert run_due_workflow_once()
    first_detail = get_workflow_run(first_run_id)
    second_detail = get_workflow_run(second_run_id)
    assert first_detail["steps"][0]["status"] == STEP_RETRY_SCHEDULED
    assert second_detail["steps"][0]["status"] == STEP_RETRY_SCHEDULED


def test_workflow_batch_prefers_existing_pipeline_before_opening_next_item():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_batch_pipeline_first",
            [
                {"id": "first", "uses": "test.immediate"},
                {"id": "second", "uses": "test.immediate", "needs": ["first"]},
            ],
        )
    )
    batch = create_workflow_batch(
        definition_key="test_batch_pipeline_first",
        concurrency=1,
        items=[{"input": {"value": 1}}, {"input": {"value": 2}}],
    )
    first_run_id = batch["runs"][0]["id"]
    second_run_id = batch["runs"][1]["id"]

    assert run_due_workflow_once()
    first_detail = get_workflow_run(first_run_id)
    second_detail = get_workflow_run(second_run_id)
    assert first_detail["steps"][0]["status"] == STEP_SUCCEEDED
    assert first_detail["steps"][1]["status"] == STEP_READY
    assert second_detail["steps"][0]["status"] == STEP_PENDING

    assert run_due_workflow_once()
    first_detail = get_workflow_run(first_run_id)
    second_detail = get_workflow_run(second_run_id)
    assert first_detail["status"] == RUN_SUCCEEDED
    assert second_detail["steps"][0]["status"] == STEP_PENDING

    assert run_due_workflow_once()
    second_detail = get_workflow_run(second_run_id)
    assert second_detail["steps"][0]["status"] == STEP_SUCCEEDED
    assert second_detail["steps"][1]["status"] == STEP_READY


def test_workflow_summary_distinguishes_ready_running_and_waiting_external():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition(
            "test_summary_state_copy",
            [
                {"id": "first", "name": "第一步", "uses": "test.immediate"},
                {"id": "second", "name": "第二步", "uses": "test.immediate", "needs": ["first"]},
            ],
        )
    )
    run = create_workflow_run(definition_key="test_summary_state_copy", inputs={})

    assert run_due_workflow_once()
    summary = get_workflow_run_summary(run["id"])
    assert summary["current_stage"] == "second"
    assert summary["display_status"] == "等待执行: 第二步"

    with Session(engine) as session:
        model = session.get(WorkflowRunModel, run["id"])
        second = session.exec(
            select(WorkflowStepRunModel)
            .where(WorkflowStepRunModel.workflow_run_id == run["id"])
            .where(WorkflowStepRunModel.step_id == "second")
        ).one()
        second.status = STEP_RUNNING
        model.status = RUN_RUNNING
        model.current_step_id = "second"
        session.add(second)
        session.add(model)
        session.commit()

    summary = get_workflow_run_summary(run["id"])
    assert summary["current_stage"] == "second"
    assert summary["display_status"] == "正在执行: 第二步"

    create_or_update_workflow_definition(
        _definition("test_summary_waiting_copy", [{"id": "wait", "name": "外部步骤", "uses": "test.hold"}])
    )
    waiting_run = create_workflow_run(definition_key="test_summary_waiting_copy", inputs={})
    assert run_due_workflow_once()
    summary = get_workflow_run_summary(waiting_run["id"])
    assert summary["current_stage"] == "wait"
    assert summary["display_status"] == "等待外部结果: 外部步骤"


def test_workflow_runtime_executes_due_steps_concurrently():
    _register_test_adapters()
    _ConcurrentBlockingAdapter.reset()
    register_step_adapter(_ConcurrentBlockingAdapter())
    create_or_update_workflow_definition(
        _definition("test_runtime_parallel", [{"id": "block", "uses": "test.concurrent_block"}])
    )
    batch = create_workflow_batch(
        definition_key="test_runtime_parallel",
        concurrency=2,
        items=[{"input": {"value": 1}}, {"input": {"value": 2}}],
    )
    runtime = WorkflowRuntime(max_parallel_steps=2, poll_interval=0.01)

    try:
        runtime.start()
        runtime.wake_up()
        assert _ConcurrentBlockingAdapter.entered.wait(timeout=2)
        with _ConcurrentBlockingAdapter.lock:
            assert _ConcurrentBlockingAdapter.max_active >= 2
        _ConcurrentBlockingAdapter.release.set()

        deadline = time.time() + 2
        summary = {}
        while time.time() < deadline:
            summary = get_workflow_batch_summary(batch["id"])
            if summary["summary"]["succeeded"] == 2:
                break
            time.sleep(0.02)
        assert summary["summary"]["succeeded"] == 2
    finally:
        _ConcurrentBlockingAdapter.release.set()
        runtime.stop()


def test_workflow_batch_pause_resume_cancel_and_retry_failed_items():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_batch_pause", [{"id": "first", "uses": "test.immediate"}])
    )
    paused_batch = create_workflow_batch(
        definition_key="test_batch_pause",
        concurrency=2,
        items=[{"input": {"value": 1}}, {"input": {"value": 2}}],
    )

    paused = pause_workflow_batch(paused_batch["id"])
    assert paused["status"] == RUN_PAUSED
    paused_run_summary = get_workflow_run_summary(paused_batch["runs"][0]["id"])
    assert paused_run_summary["batch_paused"] is True
    assert paused_run_summary["display_status"] == "批次已暂停: first"
    assert paused_run_summary["operator_action"] == "恢复批次后继续"
    paused_batch_summary = get_workflow_batch_summary(paused_batch["id"])
    assert paused_batch_summary["runs"][0]["batch_paused"] is True
    assert paused_batch_summary["runs"][0]["display_status"] == "批次已暂停: first"
    assert not run_due_workflow_once()

    resumed = resume_workflow_batch(paused_batch["id"])
    assert resumed["status"] in {RUN_PENDING, RUN_RUNNING}
    resumed_run_summary = get_workflow_run_summary(paused_batch["runs"][0]["id"])
    assert resumed_run_summary["batch_paused"] is False
    assert resumed_run_summary["display_status"] == "等待执行: first"
    assert run_due_workflow_once()
    assert run_due_workflow_once()
    assert get_workflow_batch_summary(paused_batch["id"])["summary"]["succeeded"] == 2

    create_or_update_workflow_definition(
        _definition("test_batch_cancel", [{"id": "hold", "uses": "test.hold"}])
    )
    cancel_batch = create_workflow_batch(
        definition_key="test_batch_cancel",
        concurrency=1,
        items=[{"input": {"value": 1}}, {"input": {"value": 2}}],
    )
    assert run_due_workflow_once()
    cancelled = cancel_workflow_batch(cancel_batch["id"])
    assert cancelled["summary"]["cancelled"] == 2

    create_or_update_workflow_definition(
        _definition("test_batch_retry_failed", [{"id": "needs", "uses": "test.attention", "input": {}}])
    )
    retry_batch = create_workflow_batch(
        definition_key="test_batch_retry_failed",
        concurrency=2,
        items=[{"input": {}}, {"input": {}}],
    )
    assert run_due_workflow_once()
    assert run_due_workflow_once()
    failed_summary = get_workflow_batch_summary(retry_batch["id"])
    assert failed_summary["summary"]["needs_attention"] == 2

    retried = retry_failed_workflow_batch(retry_batch["id"])
    assert retried["retried"] == 2
    assert retried["summary"]["retry_scheduled"] == 2


def test_workflow_definition_limits_and_failure_policies_are_enforced():
    _register_test_adapters()
    create_or_update_workflow_definition(
        {
            **_definition(
                "test_adapter_limit",
                [{"id": "hold", "uses": "test.hold", "stuck_after": "1s"}],
            ),
            "limits": {"adapters": {"test.hold": 1}},
        }
    )
    first = create_workflow_run(definition_key="test_adapter_limit", inputs={"value": 1})
    second = create_workflow_run(definition_key="test_adapter_limit", inputs={"value": 2})

    assert run_due_workflow_once()
    first_detail = get_workflow_run(first["id"])
    second_detail = get_workflow_run(second["id"])
    assert first_detail["steps"][0]["status"] == STEP_WAITING_EXTERNAL
    assert second_detail["steps"][0]["status"] == STEP_READY
    assert not run_due_workflow_once()

    create_or_update_workflow_definition(
        _definition(
            "test_failure_skip",
            [
                {"id": "may_fail", "uses": "test.fail", "on_failure": "skip"},
                {"id": "after", "uses": "test.immediate", "needs": ["may_fail"]},
            ],
        )
    )
    skipped = create_workflow_run(definition_key="test_failure_skip", inputs={})
    assert run_due_workflow_once()
    assert run_due_workflow_once()
    skipped_detail = get_workflow_run(skipped["id"])
    assert skipped_detail["status"] == RUN_SUCCEEDED
    assert skipped_detail["steps"][0]["status"] == STEP_SKIPPED
    assert skipped_detail["steps"][1]["status"] == STEP_SUCCEEDED

    create_or_update_workflow_definition(
        _definition(
            "test_failure_attention",
            [{"id": "may_fail", "uses": "test.fail", "on_failure": "needs_attention"}],
        )
    )
    attention = create_workflow_run(definition_key="test_failure_attention", inputs={})
    assert run_due_workflow_once()
    attention_detail = get_workflow_run(attention["id"])
    assert attention_detail["status"] == RUN_NEEDS_ATTENTION
    assert attention_detail["steps"][0]["status"] == STEP_NEEDS_ATTENTION


def test_workflow_summary_reports_duration_and_stuck_steps():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_stuck_summary", [{"id": "hold", "uses": "test.hold", "stuck_after": "1s"}])
    )
    run = create_workflow_run(definition_key="test_stuck_summary", inputs={})
    assert run_due_workflow_once()

    with Session(engine) as session:
        model = session.get(WorkflowRunModel, run["id"])
        step = session.exec(select(WorkflowStepRunModel)).one()
        old = utcnow() - timedelta(seconds=5)
        model.started_at = old
        step.started_at = old
        step.next_run_at = old
        session.add(model)
        session.add(step)
        session.commit()

    summary = get_workflow_run_summary(run["id"])
    assert summary["duration_seconds"] >= 5
    assert summary["stuck"] is True
    assert summary["stuck_step_id"] == "hold"
    assert summary["steps"][0]["stuck"] is True
    assert summary["steps"][0]["duration_seconds"] >= 5


def test_recovery_requeues_running_step_without_incrementing_attempt():
    _register_test_adapters()
    create_or_update_workflow_definition(
        _definition("test_recover", [{"id": "first", "uses": "test.immediate"}])
    )
    run = create_workflow_run(definition_key="test_recover", inputs={})
    with Session(engine) as session:
        model = session.get(WorkflowRunModel, run["id"])
        step = session.exec(select(WorkflowStepRunModel)).one()
        step.status = STEP_RUNNING
        step.attempt = 1
        step.input_json = "{}"
        step.next_run_at = None
        step.timeout_at = utcnow() + timedelta(minutes=1)
        model.status = "running"
        session.add(model)
        session.add(step)
        session.commit()

    recover_incomplete_workflow_runs()
    recovered = get_workflow_run(run["id"])
    assert recovered["steps"][0]["status"] == STEP_READY

    assert run_due_workflow_once()
    done = get_workflow_run(run["id"])
    assert done["status"] == RUN_SUCCEEDED
    assert done["steps"][0]["output"]["attempt"] == 1


def test_deterministic_child_task_id_is_reused():
    first = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"account_ids": [1]},
        task_id="task_wf_same_1",
    )
    second = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"account_ids": [2]},
        task_id="task_wf_same_1",
    )

    assert second["id"] == first["id"]
    with Session(engine) as session:
        assert len(session.exec(select(TaskModel)).all()) == 1
        assert session.exec(select(TaskModel)).one().get_payload()["account_ids"] == [1]


def test_codex_adapter_checks_account_level_result_and_disables_auto_push():
    task = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"account_ids": [9]},
        result_seed={"data": {"accounts": [{"account_id": 9, "ok": False, "error": "oauth failed"}]}},
    )
    with Session(engine) as session:
        model = session.get(TaskModel, task["id"])
        model.status = TASK_STATUS_SUCCEEDED
        session.add(model)
        session.commit()

    adapter = CodexAuthorizeAdapter()
    transition = adapter.task_succeeded({**task, "status": TASK_STATUS_SUCCEEDED}, inputs={"account_id": 9})
    assert transition.status == STEP_FAILED
    assert transition.error["code"] == "codex_account_failed"

    created = adapter.create_task(inputs={"account_id": 9, "params": {}}, task_id="task_wf_codex_1")
    with Session(engine) as session:
        model = session.get(TaskModel, created["id"])
        assert model.get_payload()["auto_push_after_oauth"] is False
