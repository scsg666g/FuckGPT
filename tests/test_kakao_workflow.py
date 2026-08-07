from __future__ import annotations

from typing import Any

from application.workflow_adapters import register_builtin_workflow_components
from application.workflow_registry import list_step_adapters, registered_workflow_definitions
from domain.workflows import STEP_NEEDS_ATTENTION, STEP_SUCCEEDED, STEP_WAITING_EXTERNAL
from features.kakao_pipeline.orchestration_adapter import (
    KakaoUpgradeAdapter,
    register_kakao_workflow_components,
)


class _FakeKakaoService:
    def __init__(
        self,
        pipeline: dict[str, Any] | None = None,
        *,
        start_result: dict[str, Any] | None = None,
        submit_result: dict[str, Any] | None = None,
        advance_result: dict[str, Any] | None = None,
        check_result: dict[str, Any] | None = None,
        already_plus: bool = False,
    ) -> None:
        self.pipeline = pipeline
        self.start_result = start_result
        self.submit_result = submit_result
        self.advance_result = advance_result
        self.check_result = check_result
        self.already_plus = already_plus
        self.start_calls: list[dict[str, Any]] = []
        self.submit_calls: list[dict[str, Any]] = []
        self.advance_calls: list[int] = []
        self.check_calls: list[dict[str, Any]] = []
        self.post_action_calls: list[dict[str, Any]] = []

    def get_account_pipeline(self, account_id: int) -> dict[str, Any]:
        if self.pipeline is None:
            raise ValueError("账号还没有 Kakao 操作记录")
        return {**self.pipeline, "account_id": account_id}

    def _account_is_plus(self, account_id: int) -> bool:
        return self.already_plus

    def start_extraction(
        self,
        account_id: int,
        supplier_setting_id: int | None = None,
        payment_method: str = "kakao_pay",
        *,
        enable_post_actions: bool = False,
    ) -> dict[str, Any]:
        self.start_calls.append(
            {
                "account_id": account_id,
                "supplier_setting_id": supplier_setting_id,
                "payment_method": payment_method,
                "enable_post_actions": enable_post_actions,
            }
        )
        self.pipeline = self.start_result or {
            "account_id": account_id,
            "state": "supplier_processing",
            "supplier_status": "PENDING",
        }
        return self.pipeline

    def submit_scanner(
        self,
        account_id: int,
        scanner_setting_id: int | None = None,
        scanner_kind: str = "",
    ) -> dict[str, Any]:
        self.submit_calls.append(
            {
                "account_id": account_id,
                "scanner_setting_id": scanner_setting_id,
                "scanner_kind": scanner_kind,
            }
        )
        self.pipeline = self.submit_result or {
            "account_id": account_id,
            "state": "scanner_processing",
            "scanner_status": "PENDING",
            "scanner_order_id": "scanner-1",
        }
        return self.pipeline

    def advance_background(self, account_id: int) -> dict[str, Any]:
        self.advance_calls.append(account_id)
        return self.advance_result or self.get_account_pipeline(account_id)

    def check_plus(
        self,
        account_id: int,
        *,
        advance_pipeline: bool = False,
        enable_post_actions: bool | None = None,
    ) -> dict[str, Any]:
        self.check_calls.append(
            {
                "account_id": account_id,
                "advance_pipeline": advance_pipeline,
                "enable_post_actions": enable_post_actions,
            }
        )
        self.pipeline = self.check_result or self.get_account_pipeline(account_id)
        return self.pipeline

    def set_codex_post_actions_enabled(self, account_id: int, enabled: bool) -> dict[str, Any]:
        self.post_action_calls.append({"account_id": account_id, "enabled": enabled})
        current = self.get_account_pipeline(account_id)
        self.pipeline = {**current, "codex_post_action_armed": bool(enabled)}
        return self.pipeline


def test_kakao_adapter_succeeds_when_pipeline_is_completed_plus():
    service = _FakeKakaoService(
        {
            "account_id": 7,
            "state": "completed",
            "plus_status": "plus",
            "final_result": "plus",
            "completion_source": "normal_scanner",
        }
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.start(inputs={"account_id": 7}, idempotency_key="wf_kakao", attempt=1)

    assert transition.status == STEP_SUCCEEDED
    assert transition.output["account_id"] == 7
    assert transition.output["completion_source"] == "normal_scanner"
    assert service.post_action_calls == []


def test_kakao_adapter_treats_completed_archive_as_succeeded():
    service = _FakeKakaoService(
        {
            "account_id": 71,
            "state": "completed",
            "plus_status": "plus",
            "final_result": "plus",
            "archived_at": "2026-08-04T00:00:00+00:00",
            "archive_disposition": "completed",
            "archive_reason": "finished",
        }
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.start(inputs={"account_id": 71}, idempotency_key="wf_archived_done", attempt=1)

    assert transition.status == STEP_SUCCEEDED
    assert transition.output["archive_disposition"] == "completed"
    assert service.post_action_calls == []
    assert service.advance_calls == []


def test_kakao_adapter_stops_abandoned_archive_without_advancing():
    service = _FakeKakaoService(
        {
            "account_id": 72,
            "state": "scanner_processing",
            "archived_at": "2026-08-04T00:00:00+00:00",
            "archive_disposition": "abandoned",
            "archive_reason": "operator stopped tracking",
        }
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.resume(inputs={"account_id": 72}, external_ref="kakao:72", attempt=1)

    assert transition.status == STEP_NEEDS_ATTENTION
    assert transition.error["code"] == "kakao_pipeline_archived"
    assert transition.output["archive_reason"] == "operator stopped tracking"
    assert service.post_action_calls == []
    assert service.advance_calls == []


def test_kakao_adapter_starts_missing_pipeline_and_waits():
    service = _FakeKakaoService(
        None,
        start_result={
            "account_id": 8,
            "state": "supplier_processing",
            "supplier_status": "PENDING",
            "supplier_order_id": "supplier-1",
        },
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.start(
        inputs={"account_id": 8, "supplier_setting_id": 12, "payment_method": "kakao_pay"},
        idempotency_key="wf_kakao",
        attempt=1,
    )

    assert transition.status == STEP_WAITING_EXTERNAL
    assert transition.external_ref == "kakao:8"
    assert service.start_calls == [
        {
            "account_id": 8,
            "supplier_setting_id": 12,
            "payment_method": "kakao_pay",
            "enable_post_actions": False,
        }
    ]


def test_kakao_adapter_does_not_restart_existing_active_pipeline():
    service = _FakeKakaoService(
        {
            "account_id": 9,
            "state": "scanner_processing",
            "scanner_status": "PENDING",
            "scanner_order_id": "scanner-1",
        }
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.start(inputs={"account_id": 9}, idempotency_key="wf_kakao", attempt=1)

    assert transition.status == STEP_WAITING_EXTERNAL
    assert transition.output["scanner_order_id"] == "scanner-1"
    assert service.start_calls == []
    assert service.submit_calls == []
    assert service.post_action_calls == [{"account_id": 9, "enabled": False}]


def test_kakao_adapter_auto_submits_scanner_when_link_is_ready():
    service = _FakeKakaoService(
        {
            "account_id": 10,
            "state": "link_ready",
            "payment_url": "https://pay.nicepay.co.kr/v1/checkout/pay/link",
        },
        submit_result={
            "account_id": 10,
            "state": "scanner_processing",
            "scanner_driver": "payment_submission",
            "scanner_status": "PENDING",
            "scanner_order_id": "submission-1",
            "scanner_submit_attempts": 1,
        },
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.start(
        inputs={
            "account_id": 10,
            "scanner_setting_id": 3,
            "scanner_kind": "scanner_546789",
            "auto_submit_scanner": True,
        },
        idempotency_key="wf_kakao",
        attempt=1,
    )

    assert transition.status == STEP_WAITING_EXTERNAL
    assert transition.output["scanner_order_id"] == "submission-1"
    assert service.submit_calls == [
        {"account_id": 10, "scanner_setting_id": 3, "scanner_kind": "scanner_546789"}
    ]


def test_kakao_adapter_pauses_for_untracked_plus_confirmation_timeout():
    adapter = KakaoUpgradeAdapter(
        _FakeKakaoService(
            {
                "account_id": 11,
                "state": "scanner_recovery_unconfirmed",
                "scanner_status": "DUPLICATE_ACCEPTED",
                "last_error_code": "untracked_plus_unconfirmed",
                "last_error_message": "上游已接收链接，但 30 分钟内尚未确认 Plus",
            }
        )
    )

    transition = adapter.start(inputs={"account_id": 11}, idempotency_key="wf_kakao", attempt=1)

    assert transition.status == STEP_NEEDS_ATTENTION
    assert transition.error["code"] == "untracked_plus_unconfirmed"
    assert "30 分钟" in transition.error["message"]


def test_kakao_adapter_manual_retry_checks_paused_plus_state():
    service = _FakeKakaoService(
        {
            "account_id": 12,
            "state": "plus_unconfirmed",
            "last_error_code": "plus_unconfirmed",
        },
        check_result={
            "account_id": 12,
            "state": "completed",
            "plus_status": "plus",
            "final_result": "plus",
            "completion_source": "normal_scanner",
        },
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.start(inputs={"account_id": 12}, idempotency_key="wf_kakao", attempt=2)

    assert transition.status == STEP_SUCCEEDED
    assert service.check_calls == [
        {
            "account_id": 12,
            "advance_pipeline": True,
            "enable_post_actions": False,
        }
    ]
    assert service.post_action_calls == [{"account_id": 12, "enabled": False}]


def test_kakao_adapter_does_not_restart_final_supplier_failure():
    service = _FakeKakaoService(
        {
            "account_id": 13,
            "state": "supplier_failed",
            "supplier_status": "FAILED",
            "supplier_order_id": "supplier-final-1",
            "last_error_code": "approve_blocked",
            "last_error_message": "OpenAI approve 未通过",
        },
        start_result={
            "account_id": 13,
            "state": "supplier_processing",
            "supplier_order_id": "supplier-should-not-open",
        },
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.resume(inputs={"account_id": 13}, external_ref="kakao:13", attempt=2)

    assert transition.status == STEP_NEEDS_ATTENTION
    assert transition.error["code"] == "approve_blocked"
    assert transition.output["supplier_order_id"] == "supplier-final-1"
    assert service.start_calls == []
    assert service.post_action_calls == [{"account_id": 13, "enabled": False}]


def test_kakao_adapter_allows_manual_restart_for_retryable_supplier_failure():
    service = _FakeKakaoService(
        {
            "account_id": 14,
            "state": "supplier_failed",
            "supplier_status": "FAILED",
            "supplier_order_id": "supplier-old",
            "last_error_code": "temporary_supplier_failure",
            "last_error_message": "供应商临时失败",
        },
        start_result={
            "account_id": 14,
            "state": "supplier_processing",
            "supplier_status": "PENDING",
            "supplier_order_id": "supplier-new",
        },
    )
    adapter = KakaoUpgradeAdapter(service)

    transition = adapter.resume(
        inputs={"account_id": 14, "supplier_setting_id": 5, "payment_method": "kakao_pay"},
        external_ref="kakao:14",
        attempt=2,
    )

    assert transition.status == STEP_WAITING_EXTERNAL
    assert transition.output["supplier_order_id"] == "supplier-new"
    assert service.start_calls == [
        {
            "account_id": 14,
            "supplier_setting_id": 5,
            "payment_method": "kakao_pay",
            "enable_post_actions": False,
        }
    ]
    assert service.post_action_calls == [{"account_id": 14, "enabled": False}]


def test_kakao_workflow_components_register_adapter_and_template():
    register_builtin_workflow_components()
    register_kakao_workflow_components()

    definitions = registered_workflow_definitions()
    kakao_definition = next(item for item in definitions if item["key"] == "register_kakao_codex_push")

    assert "kakao.upgrade" in list_step_adapters()
    assert [step["id"] for step in kakao_definition["steps"]] == ["register", "kakao", "codex", "push"]
    assert kakao_definition["steps"][1]["needs"] == ["register"]
    assert kakao_definition["steps"][2]["needs"] == ["kakao"]
    assert kakao_definition["sample_input"]["kakao"]["auto_submit_scanner"] is True
    assert kakao_definition["sample_input"]["registration"]["executor_type"] == "browser"
    assert kakao_definition["sample_input"]["registration"]["platform_proxy_mode"] == "direct"
    assert kakao_definition["sample_input"]["registration"]["extra"]["browser_visible"] is False
    assert kakao_definition["sample_input"]["codex"]["oauth_mode"] == "browser"
    assert kakao_definition["sample_input"]["codex"]["platform_proxy_mode"] == "direct"
    field_paths = {
        field["path"]
        for section in kakao_definition["ui_schema"]["sections"]
        for field in section["fields"]
    }
    assert "registration.platform_proxy_mode" in field_paths
    assert "registration.platform_proxy_value" in field_paths
    assert "registration.extra.browser_visible" in field_paths
    assert "codex.oauth_mode" in field_paths
    assert "codex.platform_proxy_mode" in field_paths
    assert "codex.platform_proxy_value" in field_paths

    kakao_section = next(section for section in kakao_definition["ui_schema"]["sections"] if section["title"] == "Kakao")
    kakao_fields = {field["path"]: field for field in kakao_section["fields"]}
    assert kakao_fields["kakao.payment_method"]["type"] == "select"
    assert kakao_fields["kakao.scanner_kind"]["options"][0]["value"] == ""
    assert kakao_fields["kakao.supplier_setting_id"]["advanced"] is True
    assert kakao_fields["kakao.scanner_setting_id"]["advanced"] is True

    registration_section = next(
        section for section in kakao_definition["ui_schema"]["sections"]
        if section["title"] == "注册"
    )
    registration_fields = {field["path"]: field for field in registration_section["fields"]}
    executor_values = [
        option["value"]
        for option in registration_fields["registration.executor_type"]["options"]
    ]
    assert executor_values == ["browser_protocol", "browser"]
    codex_section = next(section for section in kakao_definition["ui_schema"]["sections"] if section["title"] == "Codex")
    codex_fields = {field["path"]: field for field in codex_section["fields"]}
    assert [
        option["value"]
        for option in codex_fields["codex.oauth_mode"]["options"]
    ] == ["browser", "browser_protocol", "protocol"]


def test_kakao_workflow_template_is_visible_from_api(client):
    response = client.get("/api/workflows/definitions")

    assert response.status_code == 200
    definitions = response.json()["items"]
    kakao_definition = next(item for item in definitions if item["key"] == "register_kakao_codex_push")
    assert kakao_definition["name"] == "注册 → Kakao 升级 → Codex 授权 → 推送"
    assert kakao_definition["definition"]["sample_input"]["kakao"]["auto_submit_scanner"] is True
    assert kakao_definition["definition"]["sample_input"]["registration"]["platform_proxy_value"] == ""
    assert kakao_definition["definition"]["sample_input"]["codex"]["platform_proxy_value"] == ""
