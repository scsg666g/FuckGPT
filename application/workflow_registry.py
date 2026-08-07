from __future__ import annotations

from copy import deepcopy

from domain.workflows import StepAdapter


_adapters: dict[str, StepAdapter] = {}
_definitions: dict[tuple[str, int], dict] = {}


def register_step_adapter(adapter: StepAdapter) -> None:
    key = str(adapter.key or "").strip()
    if not key:
        raise ValueError("工作流 adapter key 不能为空")
    _adapters[key] = adapter


def get_step_adapter(key: str) -> StepAdapter:
    adapter = _adapters.get(str(key or ""))
    if not adapter:
        raise KeyError(f"未注册工作流步骤 adapter: {key}")
    return adapter


def list_step_adapters() -> list[str]:
    return sorted(_adapters)


def register_workflow_definition(definition: dict) -> None:
    key = str(definition.get("key") or "").strip()
    version = max(int(definition.get("version") or 1), 1)
    if not key:
        raise ValueError("工作流定义 key 不能为空")
    _definitions[(key, version)] = deepcopy({**definition, "key": key, "version": version})


def registered_workflow_definitions() -> list[dict]:
    return [deepcopy(item) for _, item in sorted(_definitions.items())]
