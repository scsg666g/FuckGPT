from __future__ import annotations

from typing import Any, Callable

from sqlmodel import Session

from application.tasks import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_SUCCEEDED,
    create_account_push_task,
    create_codex_oauth_batch_task,
    create_register_task,
    get_task,
    request_cancel,
)
from application.workflow_registry import register_step_adapter, register_workflow_definition
from core.db import AccountCodexAuthModel, engine
from domain.workflows import StepAdapter, StepTransition


class PersistentTaskAdapter(StepAdapter):
    poll_seconds = 1

    def create_task(
        self,
        *,
        inputs: dict[str, Any],
        task_id: str,
        workflow_context: dict[str, Any] | None = None,
    ) -> dict:
        raise NotImplementedError

    def task_succeeded(self, task: dict, *, inputs: dict[str, Any]) -> StepTransition:
        return StepTransition.succeeded({"task_id": task["id"], "task_result": task.get("data")})

    def start(self, *, inputs: dict[str, Any], idempotency_key: str, attempt: int) -> StepTransition:
        task_id = f"task_wf_{idempotency_key}_{attempt}"
        task = self.create_task(
            inputs=inputs,
            task_id=task_id,
            workflow_context=self._workflow_context(idempotency_key),
        )
        return self._observe(task, inputs=inputs)

    def resume(self, *, inputs: dict[str, Any], external_ref: str, attempt: int) -> StepTransition:
        task = get_task(external_ref)
        if not task:
            return StepTransition.failed("关联子任务不存在", code="child_task_missing", retryable=True)
        return self._observe(task, inputs=inputs)

    def cancel(self, *, inputs: dict[str, Any], external_ref: str) -> None:
        if external_ref:
            request_cancel(external_ref)

    @staticmethod
    def _workflow_context(idempotency_key: str) -> dict[str, Any]:
        text = str(idempotency_key or "").strip()
        context = {
            "source": "workflow",
            "workflow_idempotency_key": text,
        }
        if text.startswith("wf_"):
            parts = text.split("_")
            if len(parts) >= 4:
                context["workflow_run_id"] = "_".join(parts[:3])
                context["workflow_step_id"] = "_".join(parts[3:])
        return context

    def _observe(self, task: dict, *, inputs: dict[str, Any]) -> StepTransition:
        status = str(task.get("status") or "")
        if status == TASK_STATUS_SUCCEEDED:
            return self.task_succeeded(task, inputs=inputs)
        if status in {TASK_STATUS_FAILED, TASK_STATUS_INTERRUPTED}:
            return StepTransition.failed(
                str(task.get("error") or "子任务执行失败"),
                code=f"child_task_{status}",
                retryable=status == TASK_STATUS_INTERRUPTED,
            )
        if status == TASK_STATUS_CANCELLED:
            return StepTransition.failed("子任务已取消", code="child_task_cancelled")
        return StepTransition.waiting(
            str(task["id"]),
            seconds=self.poll_seconds,
            output={"task_id": task["id"], "task_status": status},
            message="等待子任务完成",
        )


class RegisterAccountAdapter(PersistentTaskAdapter):
    key = "account.register"

    def create_task(
        self,
        *,
        inputs: dict[str, Any],
        task_id: str,
        workflow_context: dict[str, Any] | None = None,
    ) -> dict:
        payload = dict(inputs.get("payload") or inputs)
        payload["count"] = 1
        payload["concurrency"] = 1
        payload.update(dict(workflow_context or {}))
        extra = dict(payload.get("extra") or {})
        extra["auto_codex_oauth_after_register"] = "false"
        payload["extra"] = extra
        return create_register_task(payload, task_id=task_id)

    def task_succeeded(self, task: dict, *, inputs: dict[str, Any]) -> StepTransition:
        data = task.get("data") if isinstance(task.get("data"), dict) else {}
        account_ids = [int(item) for item in data.get("account_ids", []) if int(item or 0) > 0]
        if len(account_ids) != 1:
            return StepTransition.failed("注册任务未返回唯一账号", code="registration_result_invalid")
        accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        account = next((item for item in accounts if int(item.get("account_id") or 0) == account_ids[0]), {})
        return StepTransition.succeeded(
            {"account_id": account_ids[0], "account": account, "task_id": task["id"]},
            message="账号注册完成",
        )


class CodexAuthorizeAdapter(PersistentTaskAdapter):
    key = "codex.authorize"

    def create_task(
        self,
        *,
        inputs: dict[str, Any],
        task_id: str,
        workflow_context: dict[str, Any] | None = None,
    ) -> dict:
        account_id = int(inputs.get("account_id") or 0)
        if account_id <= 0:
            raise ValueError("Codex 授权步骤缺少 account_id")
        return create_codex_oauth_batch_task(
            platform=str(inputs.get("platform") or "chatgpt"),
            account_ids=[account_id],
            params=dict(inputs.get("params") or {}),
            concurrency=1,
            auto_push_after_oauth=False,
            task_id=task_id,
            source="workflow",
            workflow_context=dict(workflow_context or {}),
        )

    def start(self, *, inputs: dict[str, Any], idempotency_key: str, attempt: int) -> StepTransition:
        account_id = int(inputs.get("account_id") or 0)
        with Session(engine) as session:
            auth = session.get(AccountCodexAuthModel, account_id) if account_id > 0 else None
            if auth and auth.has_access_token and auth.has_refresh_token:
                return StepTransition.skipped(
                    {"account_id": account_id, "already_authorized": True},
                    message="账号已有 Codex 授权，已跳过",
                )
        return super().start(inputs=inputs, idempotency_key=idempotency_key, attempt=attempt)

    def task_succeeded(self, task: dict, *, inputs: dict[str, Any]) -> StepTransition:
        account_id = int(inputs.get("account_id") or 0)
        data = task.get("data") if isinstance(task.get("data"), dict) else {}
        accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        result = next((item for item in accounts if int(item.get("account_id") or 0) == account_id), None)
        if not result or not result.get("ok"):
            return StepTransition.failed(
                str((result or {}).get("error") or "Codex 授权未成功"),
                code="codex_account_failed",
            )
        return StepTransition.succeeded(
            {"account_id": account_id, "task_id": task["id"], "result": result},
            message="Codex 授权完成",
        )


class PushAccountAdapter(PersistentTaskAdapter):
    key = "account.push"

    def create_task(
        self,
        *,
        inputs: dict[str, Any],
        task_id: str,
        workflow_context: dict[str, Any] | None = None,
    ) -> dict:
        account_id = int(inputs.get("account_id") or 0)
        target_key = str(inputs.get("target_key") or "").strip()
        if account_id <= 0:
            raise ValueError("推送步骤缺少 account_id")
        if not target_key:
            raise ValueError("推送步骤缺少 target_key")
        return create_account_push_task(
            platform=str(inputs.get("platform") or "chatgpt"),
            account_ids=[account_id],
            target_key=target_key,
            payload_format=str(inputs.get("payload_format") or "codex"),
            source="workflow",
            task_id=task_id,
            workflow_context=dict(workflow_context or {}),
        )

    def start(self, *, inputs: dict[str, Any], idempotency_key: str, attempt: int) -> StepTransition:
        if not str(inputs.get("target_key") or "").strip():
            return StepTransition.needs_attention("未选择推送目标", code="push_target_missing")
        return super().start(inputs=inputs, idempotency_key=idempotency_key, attempt=attempt)

    def task_succeeded(self, task: dict, *, inputs: dict[str, Any]) -> StepTransition:
        data = task.get("data") if isinstance(task.get("data"), dict) else {}
        if int(data.get("failed") or 0) > 0 or int(data.get("succeeded") or 0) < 1:
            results = data.get("results") if isinstance(data.get("results"), list) else []
            error = next((str(item.get("error") or "") for item in results if not item.get("ok")), "账号推送失败")
            retryable = any(int(item.get("http_status") or 0) in {0, 429} or int(item.get("http_status") or 0) >= 500 for item in results)
            return StepTransition.failed(error, code="push_failed", retryable=retryable)
        return StepTransition.succeeded(
            {"account_id": int(inputs.get("account_id") or 0), "task_id": task["id"], "delivery": data},
            message="账号推送完成",
        )


def register_builtin_workflow_components() -> None:
    for adapter in (RegisterAccountAdapter(), CodexAuthorizeAdapter(), PushAccountAdapter()):
        register_step_adapter(adapter)

    register_workflow_definition({
        "key": "register_codex_push",
        "version": 1,
        "name": "注册 → Codex 授权 → 推送",
        "description": "注册单个 ChatGPT 账号，完成 Codex 授权后推送到指定目标。",
        "sample_input": {
            "registration": {
                "count": 1,
                "concurrency": 1,
                "executor_type": "browser",
                "platform_proxy_mode": "direct",
                "platform_proxy_value": "",
                "extra": {
                    "identity_provider": "mailbox",
                    "browser_visible": False,
                },
            },
            "codex": {
                "oauth_mode": "browser",
                "browser_mode": "headless",
                "keep_browser_open": "false",
                "platform_proxy_mode": "direct",
                "platform_proxy_value": "",
            },
            "push": {"target_key": "nvtokens", "payload_format": "codex"},
        },
        "input_schema": {
            "type": "object",
            "properties": {
                "registration": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "minimum": 1, "maximum": 200},
                        "concurrency": {"type": "integer", "minimum": 1, "maximum": 20},
                        "executor_type": {"type": "string", "enum": ["browser_protocol", "browser"]},
                        "platform_proxy_mode": {"type": "string", "enum": ["direct", "manual", "proxy_service"]},
                        "platform_proxy_value": {"type": "string"},
                        "extra": {
                            "type": "object",
                            "properties": {
                                "browser_visible": {"type": "boolean"},
                            },
                        },
                    },
                },
                "codex": {
                    "type": "object",
                    "properties": {
                        "oauth_mode": {"type": "string", "enum": ["browser", "browser_protocol", "protocol"]},
                        "browser_mode": {"type": "string", "enum": ["headless", "headed"]},
                        "keep_browser_open": {"type": "string", "enum": ["false", "true"]},
                        "platform_proxy_mode": {"type": "string", "enum": ["direct", "manual", "proxy_service"]},
                        "platform_proxy_value": {"type": "string"},
                    },
                },
                "push": {
                    "type": "object",
                    "properties": {
                        "target_key": {"type": "string"},
                        "payload_format": {"type": "string", "enum": ["codex", "account"]},
                    },
                },
            },
        },
        "ui_schema": {
            "sections": [
                {
                    "title": "注册",
                    "fields": [
                        {"path": "registration.count", "label": "注册数量", "type": "number", "min": 1, "max": 200},
                        {"path": "registration.concurrency", "label": "注册并发", "type": "number", "min": 1, "max": 20},
                        {
                            "path": "registration.executor_type",
                            "label": "注册执行器",
                            "type": "select",
                            "options": [
                                {"label": "浏览器协议模式", "value": "browser_protocol"},
                                {"label": "浏览器模式", "value": "browser"},
                            ],
                        },
                        {
                            "path": "registration.extra.browser_visible",
                            "label": "显示浏览器窗口",
                            "type": "boolean",
                            "helper": "适用于浏览器协议模式和浏览器模式；关闭时在后台运行。",
                        },
                        {
                            "path": "registration.platform_proxy_mode",
                            "label": "注册代理模式",
                            "type": "select",
                            "options": [
                                {"label": "直连", "value": "direct"},
                                {"label": "手动代理", "value": "manual"},
                                {"label": "代理服务", "value": "proxy_service"},
                            ],
                        },
                        {
                            "path": "registration.platform_proxy_value",
                            "label": "注册手动代理",
                            "type": "text",
                            "placeholder": "http://user:pass@host:port",
                        },
                    ],
                },
                {
                    "title": "Codex",
                    "fields": [
                        {
                            "path": "codex.oauth_mode",
                            "label": "授权模式",
                            "type": "select",
                            "options": [
                                {"label": "浏览器模式", "value": "browser"},
                                {"label": "浏览器协议模式（Fetch 优先）", "value": "browser_protocol"},
                                {"label": "协议模式（复用已有会话）", "value": "protocol"},
                            ],
                            "helper": "协议模式不启动浏览器，需要账号已保存可复用 session/cookies。",
                        },
                        {
                            "path": "codex.browser_mode",
                            "label": "浏览器模式",
                            "type": "select",
                            "options": [
                                {"label": "无头模式", "value": "headless"},
                                {"label": "可视模式", "value": "headed"},
                            ],
                        },
                        {"path": "codex.keep_browser_open", "label": "保持浏览器打开", "type": "boolean"},
                        {
                            "path": "codex.platform_proxy_mode",
                            "label": "Codex 代理模式",
                            "type": "select",
                            "options": [
                                {"label": "直连", "value": "direct"},
                                {"label": "手动代理", "value": "manual"},
                                {"label": "代理服务", "value": "proxy_service"},
                            ],
                        },
                        {
                            "path": "codex.platform_proxy_value",
                            "label": "Codex 手动代理",
                            "type": "text",
                            "placeholder": "http://user:pass@host:port",
                        },
                    ],
                },
                {
                    "title": "推送",
                    "fields": [
                        {"path": "push.target_key", "label": "推送目标", "type": "text", "placeholder": "nvtokens"},
                        {
                            "path": "push.payload_format",
                            "label": "推送格式",
                            "type": "select",
                            "options": [
                                {"label": "Codex", "value": "codex"},
                                {"label": "账号", "value": "account"},
                            ],
                        },
                    ],
                },
            ],
        },
        "steps": [
            {
                "id": "register",
                "name": "注册账号",
                "uses": "account.register",
                "input": {"payload": {"$path": "workflow.inputs.registration"}},
                "timeout": "30m",
                "max_attempts": 1,
            },
            {
                "id": "codex",
                "name": "Codex 授权",
                "uses": "codex.authorize",
                "needs": ["register"],
                "input": {
                    "account_id": {"$path": "steps.register.output.account_id"},
                    "platform": "chatgpt",
                    "params": {"$path": "workflow.inputs.codex"},
                },
                "timeout": "20m",
                "max_attempts": 2,
                "retry_delay": "10s",
            },
            {
                "id": "push",
                "name": "推送账号",
                "uses": "account.push",
                "needs": ["codex"],
                "if": {"path": "workflow.inputs.push.target_key", "op": "exists"},
                "input": {
                    "account_id": {"$path": "steps.register.output.account_id"},
                    "platform": "chatgpt",
                    "target_key": {"$path": "workflow.inputs.push.target_key"},
                    "payload_format": {"$path": "workflow.inputs.push.payload_format"},
                },
                "timeout": "5m",
                "max_attempts": 3,
                "retry_delay": "30s",
            },
        ],
    })
