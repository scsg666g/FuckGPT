from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from application.workflow_registry import register_step_adapter, register_workflow_definition
from domain.workflows import StepAdapter, StepTransition

from .service import ACTIVE_STATES, KakaoPipelineService


_WAITING_STATES = {
    *ACTIVE_STATES,
    "plus_checking",
    "plus_pending",
}

_MANUAL_ATTENTION_STATES = {
    "supplier_submit_unconfirmed",
    "supplier_poll_failed",
    "scanner_poll_failed",
    "scanner_submit_unconfirmed",
    "scanner_recovery_unconfirmed",
    "plus_unconfirmed",
    "plus_check_failed",
}

_FAILED_STATES = {
    "supplier_failed",
    "scanner_failed",
}

_NON_RESTARTABLE_SUPPLIER_ERROR_CODES = {
    "approve_blocked",
    "invalid_payment_url",
    "zero_amount_not_verified",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bool_input(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds_until(value: Any, *, fallback: int = 5) -> int:
    when = _parse_time(value)
    if when is None:
        return fallback
    seconds = int((when.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
    return min(max(seconds, 1), 120)


class KakaoUpgradeAdapter(StepAdapter):
    """Expose the existing Kakao pipeline as one orchestration step.

    The adapter intentionally delegates all supplier/scanner/Plus recovery logic
    to ``KakaoPipelineService``.  It only starts the pipeline, optionally submits
    the scanner once a link is ready, and maps Kakao states to workflow states.
    """

    key = "kakao.upgrade"

    def __init__(self, service: KakaoPipelineService | None = None) -> None:
        self.service = service or KakaoPipelineService()

    def start(self, *, inputs: dict[str, Any], idempotency_key: str, attempt: int) -> StepTransition:
        account_id = _int_or_zero(inputs.get("account_id"))
        if account_id <= 0:
            return StepTransition.failed("Kakao 升级步骤缺少 account_id", code="kakao_account_missing")

        existing = self._get_pipeline(account_id)
        if existing:
            if existing.get("archived_at"):
                return self._map_pipeline(existing)
            if not self._pipeline_is_complete(existing):
                existing = self.service.set_codex_post_actions_enabled(account_id, False)
            return self._advance_or_map(existing, inputs=inputs, attempt=attempt)

        if self.service._account_is_plus(account_id):  # type: ignore[attr-defined]  # same feature boundary
            return StepTransition.skipped(
                {"account_id": account_id, "already_plus": True},
                message="账号当前已是 Plus，Kakao 升级已跳过",
            )

        try:
            pipeline = self.service.start_extraction(
                account_id,
                supplier_setting_id=self._optional_int(inputs.get("supplier_setting_id")),
                payment_method=_text(inputs.get("payment_method")) or "kakao_pay",
                enable_post_actions=False,
            )
        except Exception as exc:  # noqa: BLE001 - persisted Kakao state is the source of truth.
            pipeline = self._get_pipeline(account_id)
            if pipeline:
                return self._map_pipeline(pipeline)
            return StepTransition.failed(str(exc), code="kakao_start_failed", retryable=False)
        return self._advance_or_map(pipeline, inputs=inputs, attempt=attempt)

    def resume(self, *, inputs: dict[str, Any], external_ref: str, attempt: int) -> StepTransition:
        account_id = _int_or_zero(inputs.get("account_id")) or self._account_id_from_ref(external_ref)
        if account_id <= 0:
            return StepTransition.failed("Kakao 升级步骤缺少 account_id", code="kakao_account_missing")
        try:
            existing = self._get_pipeline(account_id)
            if existing and existing.get("archived_at"):
                return self._map_pipeline(existing)
            if existing and not self._pipeline_is_complete(existing):
                self.service.set_codex_post_actions_enabled(account_id, False)
            pipeline = self.service.advance_background(account_id)
        except Exception:
            pipeline = self._get_pipeline(account_id)
            if not pipeline:
                raise
        return self._advance_or_map(pipeline, inputs=inputs, attempt=attempt)

    def cancel(self, *, inputs: dict[str, Any], external_ref: str) -> None:
        # KakaoPipelineService does not have a safe non-destructive cancel.  The
        # workflow can be cancelled without resetting supplier/scanner state.
        return

    def _advance_or_map(self, pipeline: dict[str, Any], *, inputs: dict[str, Any], attempt: int) -> StepTransition:
        if pipeline.get("archived_at"):
            return self._map_pipeline(pipeline)
        state = _text(pipeline.get("state"))
        account_id = _int_or_zero(pipeline.get("account_id") or inputs.get("account_id"))

        if attempt > 1 and state == "supplier_failed" and not self._supplier_failure_is_final(pipeline):
            try:
                pipeline = self.service.start_extraction(
                    account_id,
                    supplier_setting_id=self._optional_int(inputs.get("supplier_setting_id")),
                    payment_method=_text(inputs.get("payment_method")) or "kakao_pay",
                    enable_post_actions=False,
                )
            except Exception:
                latest = self._get_pipeline(account_id)
                if latest:
                    return self._map_pipeline(latest)
                raise
            return self._map_pipeline(pipeline)

        if state == "link_ready":
            if _bool_input(inputs.get("auto_submit_scanner"), default=True):
                try:
                    pipeline = self.service.submit_scanner(
                        account_id,
                        scanner_setting_id=self._optional_int(inputs.get("scanner_setting_id")),
                        scanner_kind=_text(inputs.get("scanner_kind")),
                    )
                except Exception as exc:  # noqa: BLE001 - keep persisted state visible in workflow output.
                    latest = self._get_pipeline(account_id)
                    if latest:
                        return self._map_pipeline(latest)
                    return StepTransition.failed(str(exc), code="kakao_scanner_submit_failed")
                return self._map_pipeline(pipeline)
            return StepTransition.needs_attention(
                "Kakao 长链已就绪，请确认后上传扫码，或开启自动上传扫码后重试",
                code="kakao_scanner_upload_required",
            )

        if attempt > 1 and state == "scanner_failed" and _text(pipeline.get("payment_url")):
            if _bool_input(inputs.get("auto_submit_scanner"), default=True):
                try:
                    pipeline = self.service.submit_scanner(
                        account_id,
                        scanner_setting_id=self._optional_int(inputs.get("scanner_setting_id")),
                        scanner_kind=_text(inputs.get("scanner_kind")),
                    )
                except Exception:
                    latest = self._get_pipeline(account_id)
                    if latest:
                        return self._map_pipeline(latest)
                    raise
                return self._map_pipeline(pipeline)

        if state in {"scanner_succeeded", "plus_check_failed", "plus_unconfirmed", "scanner_recovery_unconfirmed", "scanner_submit_unconfirmed"}:
            if attempt > 1:
                try:
                    pipeline = self.service.check_plus(
                        account_id,
                        advance_pipeline=True,
                        enable_post_actions=False,
                    )
                except Exception:
                    pipeline = self._get_pipeline(account_id) or pipeline
                return self._map_pipeline(pipeline)

        return self._map_pipeline(pipeline)

    def _map_pipeline(self, pipeline: dict[str, Any]) -> StepTransition:
        state = _text(pipeline.get("state"))
        account_id = _int_or_zero(pipeline.get("account_id"))
        output = self._output(pipeline)

        if pipeline.get("archived_at"):
            disposition = _text(pipeline.get("archive_disposition"))
            if disposition == "completed":
                return StepTransition.succeeded(output, message="Kakao 流水线已完成并归档")
            return StepTransition.needs_attention(
                "Kakao 流水线已放弃并归档，工作流不会继续推进",
                code="kakao_pipeline_archived",
                output=output,
            )

        if state == "completed" or _text(pipeline.get("final_result")) == "plus" or _text(pipeline.get("plus_status")) == "plus":
            return StepTransition.succeeded(output, message="Kakao 已确认升级为 Plus")

        if state in _WAITING_STATES:
            return StepTransition.waiting(
                self._external_ref(account_id),
                seconds=self._next_wait_seconds(pipeline),
                output=output,
                message=self._waiting_message(pipeline),
            )

        if state in _MANUAL_ATTENTION_STATES:
            return StepTransition.needs_attention(
                _text(pipeline.get("last_error_message")) or self._attention_message(state),
                code=_text(pipeline.get("last_error_code")) or f"kakao_{state}",
                output=output,
            )

        if state in _FAILED_STATES:
            return StepTransition.needs_attention(
                _text(pipeline.get("last_error_message")) or self._attention_message(state),
                code=_text(pipeline.get("last_error_code")) or f"kakao_{state}",
                output=output,
            )

        if state == "idle":
            return StepTransition.needs_attention("Kakao 流水线尚未启动", code="kakao_pipeline_idle", output=output)

        return StepTransition.needs_attention(
            f"Kakao 流水线状态未识别: {state or 'unknown'}",
            code="kakao_state_unknown",
            output=output,
        )

    def _get_pipeline(self, account_id: int) -> dict[str, Any] | None:
        try:
            return self.service.get_account_pipeline(account_id)
        except ValueError:
            return None

    @staticmethod
    def _supplier_failure_is_final(pipeline: dict[str, Any]) -> bool:
        code = _text(pipeline.get("last_error_code")).lower().replace("-", "_")
        message = _text(pipeline.get("last_error_message")).lower()
        if code in _NON_RESTARTABLE_SUPPLIER_ERROR_CODES:
            return True
        if "approve" in code or "approve" in message:
            return "blocked" in code or "blocked" in message or "未通过" in message or "不通过" in message
        return False

    @staticmethod
    def _pipeline_is_complete(pipeline: dict[str, Any]) -> bool:
        return (
            _text(pipeline.get("state")) == "completed"
            or _text(pipeline.get("final_result")) == "plus"
            or _text(pipeline.get("plus_status")) == "plus"
        )

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        current = _int_or_zero(value)
        return current if current > 0 else None

    @staticmethod
    def _account_id_from_ref(value: str) -> int:
        text = _text(value)
        if text.startswith("kakao:"):
            text = text.partition(":")[2]
        return _int_or_zero(text)

    @staticmethod
    def _external_ref(account_id: int) -> str:
        return f"kakao:{int(account_id)}" if account_id > 0 else "kakao"

    @staticmethod
    def _output(pipeline: dict[str, Any]) -> dict[str, Any]:
        return {
            "account_id": _int_or_zero(pipeline.get("account_id")),
            "state": _text(pipeline.get("state")),
            "payment_method": _text(pipeline.get("payment_method")),
            "supplier_status": _text(pipeline.get("supplier_status")),
            "supplier_order_id": _text(pipeline.get("supplier_order_id")),
            "payment_url_present": bool(_text(pipeline.get("payment_url"))),
            "scanner_driver": _text(pipeline.get("scanner_driver")),
            "scanner_status": _text(pipeline.get("scanner_status")),
            "scanner_order_id": _text(pipeline.get("scanner_order_id")),
            "scanner_submit_attempts": _int_or_zero(pipeline.get("scanner_submit_attempts")),
            "scanner_compensation_attempted": bool(pipeline.get("scanner_compensation_attempted")),
            "scanner_recovery_check_count": _int_or_zero(pipeline.get("scanner_recovery_check_count")),
            "plus_status": _text(pipeline.get("plus_status")),
            "plus_check_count": _int_or_zero(pipeline.get("plus_check_count")),
            "final_result": _text(pipeline.get("final_result")),
            "completion_source": _text(pipeline.get("completion_source")),
            "last_error_code": _text(pipeline.get("last_error_code")),
            "last_error_message": _text(pipeline.get("last_error_message")),
            "archived_at": pipeline.get("archived_at"),
            "archive_disposition": _text(pipeline.get("archive_disposition")),
            "archive_reason": _text(pipeline.get("archive_reason")),
            "purged_at": pipeline.get("purged_at"),
        }

    @staticmethod
    def _next_wait_seconds(pipeline: dict[str, Any]) -> int:
        state = _text(pipeline.get("state"))
        if state in {"plus_pending", "plus_checking"}:
            return _seconds_until(pipeline.get("plus_next_check_at"), fallback=10)
        if state == "scanner_accepted_untracked":
            return _seconds_until(pipeline.get("scanner_recovery_next_check_at"), fallback=10)
        return 5

    @staticmethod
    def _waiting_message(pipeline: dict[str, Any]) -> str:
        state = _text(pipeline.get("state"))
        if state in {"supplier_submitting", "supplier_processing"}:
            return "Kakao 提链处理中"
        if state in {"scanner_submitting", "scanner_processing"}:
            return "已提交扫码供应商，等待处理结果"
        if state == "scanner_accepted_untracked":
            return "已提交给供应商，正在通过账号状态确认结果"
        if state in {"scanner_succeeded", "plus_checking", "plus_pending"}:
            return "正在确认账号 Plus 状态"
        return "Kakao 流水线处理中"

    @staticmethod
    def _attention_message(state: str) -> str:
        return {
            "supplier_submit_unconfirmed": "提链提交结果无法确认，请人工检查后处理",
            "supplier_poll_failed": "提链订单查询暂停，请人工检查供应商结果",
            "supplier_failed": "Kakao 提链失败，请人工检查供应商配置或账号状态",
            "scanner_poll_failed": "扫码订单查询暂停，请人工检查原订单结果",
            "scanner_submit_unconfirmed": "扫码提交结果无法确认，请人工检查是否已扣费或重复提交",
            "scanner_recovery_unconfirmed": "30 分钟内未确认 Plus，请人工检查是否已扣费或重复提交",
            "scanner_failed": "扫码供应商处理失败，请人工检查支付链接或 CDK",
            "plus_unconfirmed": "10 分钟内未确认 Plus，请人工检查供应商结果",
            "plus_check_failed": "Plus 状态检查失败，请人工重试",
        }.get(state, "Kakao 流水线需要人工处理")


def register_kakao_workflow_components() -> None:
    register_step_adapter(KakaoUpgradeAdapter())
    register_workflow_definition(
        {
            "key": "register_kakao_codex_push",
            "version": 1,
            "name": "注册 → Kakao 升级 → Codex 授权 → 推送",
            "description": "注册单个 ChatGPT 账号，完成 Kakao Plus 升级、Codex 授权后推送到指定目标。",
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
                "kakao": {
                    "payment_method": "kakao_pay",
                    "supplier_setting_id": None,
                    "scanner_setting_id": None,
                    "scanner_kind": "",
                    "auto_submit_scanner": True,
                },
                "codex": {
                    "oauth_mode": "browser",
                    "browser_mode": "headless",
                    "keep_browser_open": "false",
                    "platform_proxy_mode": "direct",
                    "platform_proxy_value": "",
                },
                "push": {
                    "target_key": "nvtokens",
                    "payload_format": "codex",
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
                        "title": "Kakao",
                        "description": "默认沿用 Kakao 流水线配置；这里只需设置本次任务需要覆盖的选项。",
                        "fields": [
                            {
                                "path": "kakao.payment_method",
                                "label": "支付方式",
                                "type": "select",
                                "options": [
                                    {"label": "Kakao Pay", "value": "kakao_pay"},
                                    {"label": "Naver Pay", "value": "naver_pay"},
                                ],
                            },
                            {
                                "path": "kakao.scanner_kind",
                                "label": "扫码供应商",
                                "type": "select",
                                "options": [
                                    {"label": "使用 Kakao 全局默认", "value": ""},
                                    {"label": "I7wap 扫码平台", "value": "scanner"},
                                    {"label": "546789 扫码平台", "value": "scanner_546789"},
                                ],
                            },
                            {
                                "path": "kakao.auto_submit_scanner",
                                "label": "自动上传扫码",
                                "type": "boolean",
                                "helper": "关闭后，长链生成时任务会暂停并等待人工处理。",
                            },
                            {
                                "path": "kakao.supplier_setting_id",
                                "label": "提链供应商配置 ID",
                                "type": "number",
                                "min": 0,
                                "advanced": True,
                                "helper": "留空使用 Kakao 流水线中的全局提链供应商。",
                            },
                            {
                                "path": "kakao.scanner_setting_id",
                                "label": "扫码供应商配置 ID",
                                "type": "number",
                                "min": 0,
                                "advanced": True,
                                "helper": "留空使用上方所选扫码供应商的全局配置。",
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
                    "id": "kakao",
                    "name": "Kakao 升级",
                    "uses": "kakao.upgrade",
                    "needs": ["register"],
                    "input": {
                        "account_id": {"$path": "steps.register.output.account_id"},
                        "payment_method": {"$path": "workflow.inputs.kakao.payment_method"},
                        "supplier_setting_id": {"$path": "workflow.inputs.kakao.supplier_setting_id"},
                        "scanner_setting_id": {"$path": "workflow.inputs.kakao.scanner_setting_id"},
                        "scanner_kind": {"$path": "workflow.inputs.kakao.scanner_kind"},
                        "auto_submit_scanner": {"$path": "workflow.inputs.kakao.auto_submit_scanner"},
                    },
                    "timeout": "90m",
                    "max_attempts": 1,
                },
                {
                    "id": "codex",
                    "name": "Codex 授权",
                    "uses": "codex.authorize",
                    "needs": ["kakao"],
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
        }
    )
