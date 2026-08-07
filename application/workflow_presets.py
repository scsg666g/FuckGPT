from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from core.datetime_utils import serialize_datetime
from core.db import WorkflowDefinitionModel, WorkflowInputPresetModel, engine


LAST_USED_PRESET_NAME = "__last_used__"
VALID_LAUNCH_MODES = {"single", "batch"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _definition(session: Session, definition_key: str, version: int = 0) -> WorkflowDefinitionModel:
    key = str(definition_key or "").strip()
    query = select(WorkflowDefinitionModel).where(WorkflowDefinitionModel.key == key)
    if int(version or 0) > 0:
        model = session.exec(query.where(WorkflowDefinitionModel.version == int(version))).first()
    else:
        model = session.exec(query.order_by(WorkflowDefinitionModel.version.desc())).first()
    if model is None:
        raise ValueError("工作流定义不存在")
    return model


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _template_input(definition: WorkflowDefinitionModel) -> dict[str, Any]:
    payload = definition.get_definition()
    sample = payload.get("sample_input") if isinstance(payload, dict) else None
    return deepcopy(sample) if isinstance(sample, dict) else {}


def _normalize_launch_settings(payload: dict[str, Any]) -> tuple[str, int, int]:
    launch_mode = str(payload.get("launch_mode") or "single").strip().lower()
    if launch_mode not in VALID_LAUNCH_MODES:
        raise ValueError("未知启动模式")
    batch_concurrency = min(max(int(payload.get("batch_concurrency") or 1), 1), 50)
    batch_count = min(max(int(payload.get("batch_count") or 5), 1), 200)
    return launch_mode, batch_concurrency, batch_count


def _serialize_preset(
    model: WorkflowInputPresetModel,
    *,
    definition: WorkflowDefinitionModel,
) -> dict[str, Any]:
    return {
        "id": int(model.id or 0),
        "definition_key": model.definition_key,
        "definition_version": int(model.definition_version or 1),
        "current_definition_version": int(definition.version or 1),
        "version_mismatch": int(model.definition_version or 1) != int(definition.version or 1),
        "name": "上次使用" if model.is_last_used else model.name,
        "is_default": bool(model.is_default),
        "is_last_used": bool(model.is_last_used),
        "input": _deep_merge(_template_input(definition), model.get_input()),
        "launch_mode": model.launch_mode if model.launch_mode in VALID_LAUNCH_MODES else "single",
        "batch_concurrency": min(max(int(model.batch_concurrency or 1), 1), 50),
        "batch_count": min(max(int(model.batch_count or 5), 1), 200),
        "created_at": serialize_datetime(model.created_at),
        "updated_at": serialize_datetime(model.updated_at),
    }


def list_workflow_input_presets(definition_key: str, *, version: int = 0) -> dict[str, Any]:
    with Session(engine) as session:
        definition = _definition(session, definition_key, version)
        models = session.exec(
            select(WorkflowInputPresetModel)
            .where(WorkflowInputPresetModel.definition_key == definition.key)
            .order_by(WorkflowInputPresetModel.is_last_used, WorkflowInputPresetModel.name)
        ).all()
        named = [item for item in models if not item.is_last_used]
        last_used = next((item for item in models if item.is_last_used), None)
        default = next((item for item in named if item.is_default), None)
        return {
            "definition_key": definition.key,
            "definition_version": int(definition.version or 1),
            "template_input": _template_input(definition),
            "items": [_serialize_preset(item, definition=definition) for item in named],
            "last_used": _serialize_preset(last_used, definition=definition) if last_used else None,
            "default_id": int(default.id or 0) if default else None,
        }


def save_workflow_input_preset(
    definition_key: str,
    payload: dict[str, Any],
    *,
    preset_id: int = 0,
) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("预设名称不能为空")
    if name == LAST_USED_PRESET_NAME:
        raise ValueError("预设名称不可用")
    if len(name) > 80:
        raise ValueError("预设名称不能超过 80 个字符")
    input_payload = payload.get("input")
    if not isinstance(input_payload, dict):
        raise ValueError("运行配置必须是 JSON 对象")
    launch_mode, batch_concurrency, batch_count = _normalize_launch_settings(payload)

    with Session(engine) as session:
        definition = _definition(session, definition_key, int(payload.get("definition_version") or 0))
        model = session.get(WorkflowInputPresetModel, int(preset_id or 0)) if int(preset_id or 0) > 0 else None
        if model is not None and (model.definition_key != definition.key or model.is_last_used):
            raise ValueError("运行配置预设不存在")
        duplicate = session.exec(
            select(WorkflowInputPresetModel)
            .where(WorkflowInputPresetModel.definition_key == definition.key)
            .where(WorkflowInputPresetModel.name == name)
        ).first()
        if duplicate is not None and (model is None or duplicate.id != model.id):
            if int(preset_id or 0) > 0:
                raise ValueError("同名运行配置预设已存在")
            model = duplicate
        if model is None:
            model = WorkflowInputPresetModel(definition_key=definition.key, name=name)

        requested_default = payload.get("is_default")
        if requested_default is True:
            existing_defaults = session.exec(
                select(WorkflowInputPresetModel)
                .where(WorkflowInputPresetModel.definition_key == definition.key)
                .where(WorkflowInputPresetModel.is_default == True)  # noqa: E712
            ).all()
            for item in existing_defaults:
                item.is_default = False
                item.updated_at = _utcnow()
                session.add(item)
        model.name = name
        model.definition_version = int(definition.version or 1)
        model.is_last_used = False
        if requested_default is not None:
            model.is_default = bool(requested_default)
        model.launch_mode = launch_mode
        model.batch_concurrency = batch_concurrency
        model.batch_count = batch_count
        model.set_input(input_payload)
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()
        session.refresh(model)
        return _serialize_preset(model, definition=definition)


def save_last_used_workflow_input(definition_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    input_payload = payload.get("input")
    if not isinstance(input_payload, dict):
        raise ValueError("运行配置必须是 JSON 对象")
    launch_mode, batch_concurrency, batch_count = _normalize_launch_settings(payload)
    with Session(engine) as session:
        definition = _definition(session, definition_key, int(payload.get("definition_version") or 0))
        model = session.exec(
            select(WorkflowInputPresetModel)
            .where(WorkflowInputPresetModel.definition_key == definition.key)
            .where(WorkflowInputPresetModel.name == LAST_USED_PRESET_NAME)
        ).first()
        if model is None:
            model = WorkflowInputPresetModel(
                definition_key=definition.key,
                name=LAST_USED_PRESET_NAME,
                is_last_used=True,
            )
        model.definition_version = int(definition.version or 1)
        model.is_default = False
        model.is_last_used = True
        model.launch_mode = launch_mode
        model.batch_concurrency = batch_concurrency
        model.batch_count = batch_count
        model.set_input(input_payload)
        model.updated_at = _utcnow()
        session.add(model)
        session.commit()
        session.refresh(model)
        return _serialize_preset(model, definition=definition)


def delete_workflow_input_preset(definition_key: str, preset_id: int) -> bool:
    with Session(engine) as session:
        model = session.get(WorkflowInputPresetModel, int(preset_id))
        if model is None or model.is_last_used or model.definition_key != str(definition_key or "").strip():
            return False
        session.delete(model)
        session.commit()
        return True
