from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.workflows import (
    cancel_workflow_batch,
    cancel_workflow_run,
    create_workflow_batch,
    create_or_update_workflow_definition,
    create_workflow_run,
    get_workflow_batch,
    get_workflow_batch_summary,
    get_workflow_run,
    get_workflow_run_summary,
    list_workflow_adapters,
    list_workflow_batches,
    list_workflow_definitions,
    list_workflow_events,
    list_workflow_runs,
    pause_workflow_batch,
    resume_workflow_batch,
    retry_failed_workflow_batch,
    retry_workflow_step,
    update_workflow_step_input,
)
from application.workflow_presets import (
    delete_workflow_input_preset,
    list_workflow_input_presets,
    save_last_used_workflow_input,
    save_workflow_input_preset,
)
from services.workflow_runtime import workflow_runtime

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowDefinitionRequest(BaseModel):
    definition: dict = Field(default_factory=dict)


class WorkflowInputPresetRequest(BaseModel):
    name: str = ""
    definition_version: int = 0
    input: dict = Field(default_factory=dict)
    launch_mode: str = "single"
    batch_concurrency: int = 1
    batch_count: int = 5
    is_default: bool | None = None


class WorkflowLastUsedInputRequest(BaseModel):
    definition_version: int = 0
    input: dict = Field(default_factory=dict)
    launch_mode: str = "single"
    batch_concurrency: int = 1
    batch_count: int = 5


class WorkflowRunRequest(BaseModel):
    definition_key: str
    version: int = 0
    name: str = ""
    input: dict = Field(default_factory=dict)


class WorkflowBatchItemRequest(BaseModel):
    name: str = ""
    input: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class WorkflowBatchRequest(BaseModel):
    definition_key: str
    version: int = 0
    name: str = ""
    concurrency: int = 1
    items: list[WorkflowBatchItemRequest] = Field(default_factory=list)


class WorkflowStepInputRequest(BaseModel):
    input: dict = Field(default_factory=dict)


@router.get("/definitions")
def list_definitions(include_disabled: bool = False):
    return {"items": list_workflow_definitions(include_disabled=include_disabled)}


@router.get("/adapters")
def list_adapters():
    return {"items": list_workflow_adapters()}


@router.post("/definitions")
def upsert_definition(body: WorkflowDefinitionRequest):
    try:
        return create_or_update_workflow_definition(body.definition)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/definitions/{definition_key}/presets")
def list_input_presets(definition_key: str, version: int = 0):
    try:
        return list_workflow_input_presets(definition_key, version=version)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/definitions/{definition_key}/presets")
def create_input_preset(definition_key: str, body: WorkflowInputPresetRequest):
    try:
        return save_workflow_input_preset(definition_key, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/definitions/{definition_key}/presets/last-used")
def save_last_used_input(definition_key: str, body: WorkflowLastUsedInputRequest):
    try:
        return save_last_used_workflow_input(definition_key, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/definitions/{definition_key}/presets/{preset_id}")
def update_input_preset(definition_key: str, preset_id: int, body: WorkflowInputPresetRequest):
    try:
        return save_workflow_input_preset(definition_key, body.model_dump(), preset_id=preset_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/definitions/{definition_key}/presets/{preset_id}")
def delete_input_preset(definition_key: str, preset_id: int):
    if not delete_workflow_input_preset(definition_key, preset_id):
        raise HTTPException(404, "运行配置预设不存在")
    return {"ok": True}


@router.get("/batches")
def list_batches(limit: int = 50, offset: int = 0, status: str = "", definition_key: str = ""):
    return list_workflow_batches(limit=limit, offset=offset, status=status, definition_key=definition_key)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str):
    batch = get_workflow_batch(batch_id)
    if not batch:
        raise HTTPException(404, "工作流批次不存在")
    return batch


@router.get("/batches/{batch_id}/summary")
def get_batch_summary(batch_id: str):
    summary = get_workflow_batch_summary(batch_id)
    if not summary:
        raise HTTPException(404, "工作流批次不存在")
    return summary


@router.post("/batches/{batch_id}/pause")
def pause_batch(batch_id: str):
    batch = pause_workflow_batch(batch_id)
    if not batch:
        raise HTTPException(404, "工作流批次不存在")
    return batch


@router.post("/batches/{batch_id}/resume")
def resume_batch(batch_id: str):
    batch = resume_workflow_batch(batch_id)
    if not batch:
        raise HTTPException(404, "工作流批次不存在")
    workflow_runtime.wake_up()
    return batch


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str):
    batch = cancel_workflow_batch(batch_id)
    if not batch:
        raise HTTPException(404, "工作流批次不存在")
    workflow_runtime.wake_up()
    return batch


@router.post("/batches/{batch_id}/retry-failed")
def retry_failed_batch(batch_id: str):
    batch = retry_failed_workflow_batch(batch_id)
    if not batch:
        raise HTTPException(404, "工作流批次不存在")
    workflow_runtime.wake_up()
    return batch


@router.get("/runs")
def list_runs(limit: int = 50, offset: int = 0, status: str = "", definition_key: str = "", batch_id: str = ""):
    return list_workflow_runs(
        limit=limit,
        offset=offset,
        status=status,
        definition_key=definition_key,
        batch_id=batch_id,
    )


@router.post("/runs")
def start_run(body: WorkflowRunRequest):
    run = create_workflow_run(
        definition_key=body.definition_key,
        version=body.version,
        name=body.name,
        inputs=body.input,
    )
    if not run:
        raise HTTPException(404, "工作流定义不存在或未启用")
    workflow_runtime.wake_up()
    return run


@router.post("/runs/batch")
def start_batch(body: WorkflowBatchRequest):
    try:
        batch = create_workflow_batch(
            definition_key=body.definition_key,
            version=body.version,
            name=body.name,
            concurrency=body.concurrency,
            items=[item.model_dump() for item in body.items],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not batch:
        raise HTTPException(404, "工作流定义不存在或未启用")
    workflow_runtime.wake_up()
    return batch


@router.get("/runs/{run_id}/summary")
def get_run_summary(run_id: str):
    summary = get_workflow_run_summary(run_id)
    if not summary:
        raise HTTPException(404, "工作流不存在")
    return summary


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    run = get_workflow_run(run_id)
    if not run:
        raise HTTPException(404, "工作流不存在")
    return run


@router.get("/runs/{run_id}/events")
def get_events(
    run_id: str,
    since: int = 0,
    before: int = 0,
    limit: int = 200,
    latest: bool = False,
):
    events = list_workflow_events(run_id, since=since, before=before, limit=limit, latest=latest)
    if events is None:
        raise HTTPException(404, "工作流不存在")
    return events


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    run = cancel_workflow_run(run_id)
    if not run:
        raise HTTPException(404, "工作流不存在")
    workflow_runtime.wake_up()
    return run


@router.post("/runs/{run_id}/steps/{step_id}/retry")
def retry_step(run_id: str, step_id: str):
    try:
        run = retry_workflow_step(run_id, step_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not run:
        raise HTTPException(404, "工作流或步骤不存在")
    workflow_runtime.wake_up()
    return run


@router.patch("/runs/{run_id}/steps/{step_id}/input")
def update_step_input(run_id: str, step_id: str, body: WorkflowStepInputRequest):
    try:
        run = update_workflow_step_input(run_id, step_id, body.input)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not run:
        raise HTTPException(404, "工作流或步骤不存在")
    return run
