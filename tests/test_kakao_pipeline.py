from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import select

from core.base_platform import Account
from core.db import (
    AccountCodexAuthModel,
    AccountPushDeliveryModel,
    KakaoPipelineModel,
    TaskModel,
    save_account,
)
from features.kakao_pipeline.client import CustomerApiClient, CustomerApiProblem
from features.kakao_pipeline.service import KakaoPipelineService
from features.kakao_pipeline.workstation_client import WorkstationScannerClient


def _create_account(email: str = "kakao@test.com") -> int:
    created = save_account(
        Account(
            platform="chatgpt",
            email=email,
            password="TestPass123!",
            extra={"access_token": "access-token-for-kakao-test"},
        )
    )
    return int(created.id)


def _create_session_account(email: str = "kakao-session@test.com") -> int:
    created = save_account(
        Account(
            platform="chatgpt",
            email=email,
            password="TestPass123!",
            extra={
                "access_token": "access-token-for-kakao-test",
                "session_token": "session-cookie-for-kakao-test",
            },
        )
    )
    return int(created.id)


def test_plus_confirmation_uses_initial_ladder_then_60_to_120_seconds(monkeypatch):
    from features.kakao_pipeline import service as kakao_service

    requested_bounds: list[tuple[int, int]] = []

    def fake_randint(lower: int, upper: int) -> int:
        requested_bounds.append((lower, upper))
        return 60

    monkeypatch.setattr(kakao_service.random, "randint", fake_randint)

    assert [kakao_service._next_plus_delay_seconds(count) for count in range(1, 6)] == [5, 10, 30, 30, 30]
    assert kakao_service._next_plus_delay_seconds(6) == 60
    assert kakao_service._next_plus_delay_seconds(20) == 60
    assert requested_bounds == [(60, 120), (60, 120)]


def test_pipeline_latest_event_time_is_not_the_poll_updated_time():
    pipeline = KakaoPipelineModel(
        account_id=123,
        state="supplier_processing",
        updated_at=datetime(2026, 8, 1, 12, 5, tzinfo=timezone.utc),
    )
    pipeline.set_events(
        [
            {
                "time": "2026-08-01T12:00:00+00:00",
                "level": "info",
                "message": "供应商状态: PENDING",
            }
        ]
    )

    serialized = KakaoPipelineService._serialize_pipeline(pipeline)

    assert serialized["updated_at"] == "2026-08-01T12:05:00+00:00"
    assert serialized["latest_event_at"] == "2026-08-01T12:00:00+00:00"


def test_customer_api_client_builds_extract_and_ready_link_requests(monkeypatch):
    calls = []

    class Response:
        status_code = 201

        def json(self):
            return {"ok": True, "data": {"order": {"id": "order-1"}}}

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("features.kakao_pipeline.client.requests.request", fake_request)
    client = CustomerApiClient("https://supplier.example", "cdk-secret")
    client.create_extraction("at-secret", payment_method="kakao_pay")
    client.create_scanner("at-secret", "https://pay.nicepay.co.kr/v1/checkout/pay/abc")
    client.create_scanner(
        "at-secret",
        "https://pay.nicepay.co.kr/v1/checkout/pay/session",
        session_cookie="session-cookie-secret",
    )

    assert calls[0][0] == "POST"
    assert calls[0][2]["headers"]["X-CDK-Key"] == "cdk-secret"
    assert calls[0][2]["json"]["mode"] == "EXTRACT"
    assert calls[1][2]["json"]["mode"] == "READY_LINK"
    assert calls[1][2]["json"]["payment_url"].startswith("https://pay.nicepay.co.kr/")
    assert calls[1][2]["allow_redirects"] is False
    assert calls[2][2]["json"]["session_cookie"] == "session-cookie-secret"
    assert "access_token" not in calls[2][2]["json"]


def test_customer_api_client_rejects_cross_origin_poll_url(monkeypatch):
    def should_not_request(*args, **kwargs):
        raise AssertionError("跨域 pollUrl 不应发起请求")

    monkeypatch.setattr("features.kakao_pipeline.client.requests.request", should_not_request)
    client = CustomerApiClient("https://supplier.example", "cdk-secret")

    try:
        client.get_order("https://attacker.example/orders/1", "customer-token")
    except ValueError as exc:
        assert "同源" in str(exc)
    else:
        raise AssertionError("跨域 pollUrl 应被拒绝")


def test_customer_api_client_checks_exact_cdk_quota(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "ok": True,
                "data": {
                    "productType": "KAKAO_AT",
                    "totalCount": 10,
                    "usedCount": 2,
                    "frozenCount": 1,
                    "availableCount": 7,
                    "status": "ACTIVE",
                },
            }

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("features.kakao_pipeline.client.requests.request", fake_request)
    result = CustomerApiClient("https://upi.example", "visible-cdk").check_cdk()

    assert result["availableCount"] == 7
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://upi.example/api/customer/cdk/check"
    assert calls[0][2]["json"] == {"code": "visible-cdk"}
    assert "X-CDK-Key" not in calls[0][2]["headers"]


def test_workstation_scanner_client_uses_submission_protocol(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/api/payment-submissions/batch"):
            return Response({"submissions": [{"id": "submission-1", "state": "pending"}]})
        if url.endswith("/api/payment-submissions/status"):
            return Response({"ok": True, "items": [{"id": "submission-1", "state": "completed"}]})
        return Response({"unlimited": False, "remaining": 3, "limit": 10})

    monkeypatch.setattr("features.kakao_pipeline.workstation_client.requests.request", fake_request)
    client = WorkstationScannerClient("https://kakao.example", "payment-cdk")
    quota = client.check_cdk()
    submitted = client.submit_payment("https://pay.nicepay.co.kr/v1/checkout/pay/workstation")
    status = client.get_submission("submission-1")

    assert quota["remaining"] == 3
    assert submitted["submissions"][0]["id"] == "submission-1"
    assert status["data"]["state"] == "completed"
    assert calls[1][2]["json"] == {
        "payment_urls": ["https://pay.nicepay.co.kr/v1/checkout/pay/workstation"],
        "submit_cdk": "payment-cdk",
    }
    assert calls[2][2]["json"] == {"ids": ["submission-1"]}


def test_kakao_pipeline_supplier_to_scanner_flow(monkeypatch):
    account_id = _create_session_account()
    service = KakaoPipelineService()

    class FakeClient:
        def __init__(self, base_url, cdk_key):
            assert base_url
            assert cdk_key

        def create_extraction(self, access_token, *, payment_method):
            assert access_token == "access-token-for-kakao-test"
            return {
                "data": {
                    "order": {"id": "supplier-order", "status": "PENDING"},
                    "customerToken": "supplier-token",
                    "pollUrl": "/api/v1/customer/orders/supplier-order",
                }
            }

        def create_scanner(self, access_token, payment_url, *, payment_method, session_cookie=""):
            assert access_token == "access-token-for-kakao-test"
            assert session_cookie == "session-cookie-for-kakao-test"
            assert payment_url.startswith("https://pay.nicepay.co.kr/")
            return {
                "data": {
                    "order": {"id": "scanner-order", "status": "PENDING"},
                    "customerToken": "scanner-token",
                    "pollUrl": "/api/v1/customer/orders/scanner-order",
                }
            }

        def get_order(self, poll_url, customer_token):
            if "supplier" in poll_url:
                return {
                    "data": {
                        "status": "READY",
                        "qualification": {
                            "status": "QUALIFIED",
                            "zeroVerified": True,
                            "postPromoAmountKrw": 0,
                            "postTaxAmountKrw": 0,
                        },
                        "extraction": {
                            "status": "READY",
                            "paymentUrl": "https://pay.nicepay.co.kr/v1/checkout/pay/ready",
                            "stage": 9,
                            "stageTotal": 9,
                            "stageName": "完成",
                        },
                    }
                }
            return {
                "data": {
                    "status": "SUCCEEDED",
                    "subscription": {"status": "PLUS"},
                    "payment": {"qrUrl": "https://scanner.example/qr/1"},
                }
            }

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", FakeClient)
    monkeypatch.setenv("KAKAO_SUPPLIER_CDK_KEY", "supplier-cdk")
    monkeypatch.setenv("KAKAO_SCANNER_CDK_KEY", "scanner-cdk")

    created = service.start_extraction(account_id)
    assert created["state"] == "supplier_processing"
    assert created["latest_event_at"] == created["events"][-1]["time"]
    ready = service.poll_supplier(account_id)
    assert ready["state"] == "link_ready"
    assert ready["payment_url"].startswith("https://pay.nicepay.co.kr/")
    submitted = service.submit_scanner(account_id)
    assert submitted["state"] == "scanner_processing"
    scanned = service.poll_scanner(account_id)
    assert scanned["state"] == "scanner_succeeded"
    assert scanned["scan_url"] == "https://scanner.example/qr/1"

    detail = service.get_account_pipeline(account_id)
    encoded = str(detail)
    assert "supplier-token" not in encoded
    assert "scanner-token" not in encoded
    assert "customerToken" not in detail["supplier_response"]["data"]


def test_kakao_settings_support_visible_multiline_cdk_pool(client):
    response = client.put(
        "/api/kakao-pipeline/settings/supplier",
        json={
            "display_name": "临时供应商",
            "base_url": "http://127.0.0.1:8788",
            "cdk_keys": "supplier-cdk-one\nsupplier-cdk-two\nsupplier-cdk-one",
        },
    )
    assert response.status_code == 200
    payload = response.json()["item"]
    assert payload["display_name"] == "临时供应商"
    assert payload["has_cdk"] is True
    assert payload["cdk_count"] == 2
    assert payload["cdk_keys"] == ["supplier-cdk-one", "supplier-cdk-two"]

    listed = client.get("/api/kakao-pipeline/settings")
    assert listed.status_code == 200
    assert listed.json()["supplier"]["cdk_keys"] == ["supplier-cdk-one", "supplier-cdk-two"]
    assert listed.json()["scanner"]["base_url"] == "https://customer.i7wap.xyz"
    assert listed.json()["account_proxy"] == {"mode": "direct", "value": "", "preview": ""}

    missing_proxy = client.put(
        "/api/kakao-pipeline/settings/options/account-proxy",
        json={"mode": "manual", "value": ""},
    )
    assert missing_proxy.status_code == 400

    account_proxy = client.put(
        "/api/kakao-pipeline/settings/options/account-proxy",
        json={"mode": "manual", "value": "http://proxy-user:proxy-pass@127.0.0.1:7897"},
    )
    assert account_proxy.status_code == 200
    assert account_proxy.json()["account_proxy"] == {
        "mode": "manual",
        "value": "http://proxy-user:proxy-pass@127.0.0.1:7897",
        "preview": "http://***:***@127.0.0.1:7897",
    }
    assert client.get("/api/kakao-pipeline/settings").json()["account_proxy"]["mode"] == "manual"

    proxy_service = client.put(
        "/api/kakao-pipeline/settings/options/account-proxy",
        json={"mode": "proxy_service", "value": "http://ignored.example:8080"},
    )
    assert proxy_service.status_code == 200
    assert proxy_service.json()["account_proxy"] == {"mode": "proxy_service", "value": "", "preview": ""}

    migrated = client.put(
        "/api/kakao-pipeline/settings/scanner",
        json={"display_name": "扫码平台", "base_url": "https://upi.i7wap.xyz", "cdk_keys": "scanner-cdk"},
    )
    assert migrated.status_code == 200
    assert migrated.json()["item"]["base_url"] == "https://customer.i7wap.xyz"

    selected = client.put(
        "/api/kakao-pipeline/settings/default-scanner/select",
        json={"scanner_kind": "scanner_546789"},
    )
    assert selected.status_code == 200
    assert selected.json()["default_scanner_kind"] == "scanner_546789"
    assert client.get("/api/kakao-pipeline/settings").json()["default_scanner_kind"] == "scanner_546789"

    automatic = client.put(
        "/api/kakao-pipeline/settings/options/auto-upload",
        json={"enabled": True},
    )
    assert automatic.status_code == 200
    assert automatic.json()["auto_upload_after_extract"] is True
    assert client.get("/api/kakao-pipeline/settings").json()["auto_upload_after_extract"] is True

    cleared = client.put(
        "/api/kakao-pipeline/settings/supplier",
        json={"display_name": "临时供应商", "base_url": "http://127.0.0.1:8788", "cdk_keys": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["item"]["cdk_keys"] == []


def test_depleted_scanner_cdk_is_removed_and_upload_can_retry(monkeypatch):
    account_id = _create_account("retry-kakao@test.com")
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    ProviderDefinitionsRepository().ensure_seeded()
    service = KakaoPipelineService()
    service.save_setting(
        "scanner",
        {
            "display_name": "扫码平台",
            "base_url": "https://scanner.example",
            "cdk_keys": "used-cdk\nnext-cdk",
        },
    )

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        pipeline = KakaoPipelineModel(
            account_id=account_id,
            state="link_ready",
            payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/retry",
        )
        session.add(pipeline)
        session.commit()

    used_keys = []

    class FakeClient:
        def __init__(self, base_url, cdk_key):
            used_keys.append(cdk_key)
            self.cdk_key = cdk_key

        def create_scanner(self, access_token, payment_url, *, payment_method, session_cookie=""):
            if self.cdk_key == "used-cdk":
                raise CustomerApiProblem(403, "insufficient_cdk_uses", "CDK 可用次数不足 / Insufficient CDK uses")
            return {
                "data": {
                    "order": {"id": "scanner-retry", "status": "PENDING"},
                    "customerToken": "scanner-token",
                    "pollUrl": "/api/v1/customer/orders/scanner-retry",
                }
            }

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", FakeClient)

    try:
        service.submit_scanner(account_id)
    except CustomerApiProblem:
        pass
    else:
        raise AssertionError("第一条已用完的 CDK 应导致提交失败")

    failed = service.get_account_pipeline(account_id)
    assert failed["state"] == "scanner_failed"
    assert "已删除用完的 CDK" in failed["last_error_message"]
    assert service.list_settings()["scanner"]["cdk_keys"] == ["next-cdk"]

    retried = service.submit_scanner(account_id)
    assert retried["state"] == "scanner_processing"
    assert used_keys == ["used-cdk", "next-cdk"]


def test_546789_scanner_flow_uses_independent_driver(monkeypatch):
    account_id = _create_account("workstation-kakao@test.com")
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    ProviderDefinitionsRepository().ensure_seeded()
    service = KakaoPipelineService()
    setting = service.save_setting(
        "scanner_546789",
        {
            "display_name": "546789 扫码",
            "base_url": "https://kakao.example",
            "cdk_keys": "workstation-cdk",
        },
    )

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/workstation-flow",
            )
        )
        session.commit()

    class FakeWorkstationClient:
        def __init__(self, base_url, cdk_key=""):
            assert base_url == "https://kakao.example"
            if cdk_key:
                assert cdk_key == "workstation-cdk"

        def submit_payment(self, payment_url):
            assert payment_url.endswith("/workstation-flow")
            return {"submissions": [{"id": "submission-flow", "state": "pending"}]}

        def get_submission(self, submission_id):
            assert submission_id == "submission-flow"
            return {"ok": True, "data": {"id": submission_id, "state": "completed"}}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", FakeWorkstationClient)

    submitted = service.submit_scanner(
        account_id,
        scanner_setting_id=setting["id"],
        scanner_kind="scanner_546789",
    )
    assert submitted["state"] == "scanner_processing"
    assert submitted["scanner_driver"] == "payment_submission"
    assert submitted["scanner_order_id"] == "submission-flow"
    assert submitted["scan_url"].endswith("/submission-flow/qr.png")

    completed = service.poll_scanner(account_id)
    assert completed["state"] == "scanner_succeeded"
    assert completed["scanner_status"] == "COMPLETED"


def test_supplier_ready_poll_does_not_regress_an_existing_scanner_state(monkeypatch):
    account_id = _create_account("supplier-stale-ready@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                supplier_order_id="supplier-order",
                supplier_poll_url="/api/v1/customer/orders/supplier-order",
                supplier_customer_token="supplier-token",
                scanner_order_id="scanner-order",
            )
        )
        session.commit()

    class ShouldNotPoll:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("过期的供应商轮询不应再访问上游")

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", ShouldNotPoll)

    result = service.poll_supplier(account_id)

    assert result["state"] == "scanner_processing"
    assert result["scanner_order_id"] == "scanner-order"


def test_546789_unknown_immediately_compensates_and_tracks_new_submission(monkeypatch):
    account_id = _create_account("workstation-compensate@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/compensate",
                scanner_driver="payment_submission",
                scanner_name="546789",
                scanner_base_url="https://kakao.example",
                scanner_cdk_key="workstation-cdk",
                scanner_order_id="missing-submission",
                scanner_status="PENDING",
                scanner_submit_attempts=1,
            )
        )
        session.commit()

    calls: list[str] = []

    class FakeWorkstationClient:
        def __init__(self, _base_url, cdk_key=""):
            self.cdk_key = cdk_key

        def get_submission(self, submission_id):
            assert submission_id == "missing-submission"
            return {"ok": True, "data": {"id": submission_id, "state": "unknown"}}

        def submit_payment(self, payment_url):
            calls.append(payment_url)
            assert self.cdk_key == "workstation-cdk"
            return {"submissions": [{"id": "replacement-submission", "state": "queued"}]}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", FakeWorkstationClient)

    result = service.poll_scanner(account_id)

    assert result["state"] == "scanner_processing"
    assert result["scanner_order_id"] == "replacement-submission"
    assert result["scanner_status"] == "QUEUED"
    assert result["scanner_submit_attempts"] == 2
    assert result["scanner_compensation_attempted"] is True
    assert len(calls) == 1


def test_546789_duplicate_compensation_switches_to_untracked_plus_confirmation(monkeypatch):
    account_id = _create_account("workstation-duplicate@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/duplicate",
                scanner_driver="payment_submission",
                scanner_name="546789",
                scanner_base_url="https://kakao.example",
                scanner_cdk_key="workstation-cdk",
                scanner_order_id="unknown-submission",
                scanner_status="PENDING",
                scanner_submit_attempts=1,
                scan_url="https://kakao.example/old-qr.png",
            )
        )
        session.commit()

    class DuplicateWorkstationClient:
        def __init__(self, _base_url, _cdk_key=""):
            pass

        def get_submission(self, submission_id):
            return {"ok": True, "data": {"id": submission_id, "state": "unknown"}}

        def submit_payment(self, _payment_url):
            raise CustomerApiProblem(409, "http_409", "支付链接已经提交，请勿重复点击")

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", DuplicateWorkstationClient)

    accepted = service.poll_scanner(account_id)

    assert accepted["state"] == "scanner_accepted_untracked"
    assert accepted["scanner_status"] == "DUPLICATE_ACCEPTED"
    assert accepted["scanner_order_id"] == ""
    assert accepted["scan_url"] == ""
    assert accepted["scanner_submit_attempts"] == 2
    assert accepted["scanner_compensation_attempted"] is True
    recovery_started = datetime.fromisoformat(accepted["scanner_recovery_started_at"])
    recovery_next = datetime.fromisoformat(accepted["scanner_recovery_next_check_at"])
    recovery_deadline = datetime.fromisoformat(accepted["scanner_recovery_deadline_at"])
    assert timedelta(seconds=29) <= recovery_next - recovery_started <= timedelta(seconds=31)
    assert timedelta(minutes=29, seconds=59) <= recovery_deadline - recovery_started <= timedelta(minutes=30, seconds=1)

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "plus", "state": "subscribed"},
            }
        },
    )

    completed = service.check_untracked_plus(account_id)

    assert completed["state"] == "completed"
    assert completed["completion_source"] == "duplicate_submission_untracked"
    assert completed["scanner_recovery_check_count"] == 1
    assert completed["scanner_recovery_next_check_at"] is None
    assert completed["post_actions"]["codex"]["status"] == "waiting"


def test_page_plus_check_persists_arm_before_untracked_delegation(monkeypatch):
    account_id = _create_account("page-untracked-arm@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_accepted_untracked",
                scanner_status="DUPLICATE_ACCEPTED",
                scanner_recovery_started_at=now,
                scanner_recovery_next_check_at=now,
                scanner_recovery_deadline_at=now + timedelta(minutes=30),
                codex_post_action_armed=False,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "plus", "state": "subscribed"},
            }
        },
    )

    completed = service.check_plus(
        account_id,
        advance_pipeline=True,
        enable_post_actions=True,
    )

    assert completed["state"] == "completed"
    assert completed["post_actions"]["codex"]["status"] == "pending"
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert pipeline.codex_post_action_armed is True


def test_untracked_plus_confirmation_stops_only_at_persisted_30_minute_deadline(monkeypatch):
    account_id = _create_account("untracked-plus-deadline@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_accepted_untracked",
                scanner_status="DUPLICATE_ACCEPTED",
                scanner_recovery_started_at=started_at,
                scanner_recovery_next_check_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                scanner_recovery_deadline_at=started_at + timedelta(minutes=30),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("到期后不应再次查询 Plus")),
    )

    result = service.advance_background(account_id, expected_state="scanner_accepted_untracked")

    assert result["state"] == "scanner_recovery_unconfirmed"
    assert result["last_error_code"] == "untracked_plus_unconfirmed"
    assert result["scanner_recovery_next_check_at"] is None
    assert "30 分钟" in result["last_error_message"]


def test_546789_compensation_is_never_submitted_more_than_once(monkeypatch):
    account_id = _create_account("workstation-one-compensation@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/no-third-submit",
                scanner_driver="payment_submission",
                scanner_name="546789",
                scanner_base_url="https://kakao.example",
                scanner_cdk_key="workstation-cdk",
                scanner_order_id="still-missing",
                scanner_status="PENDING",
                scanner_submit_attempts=2,
                scanner_compensation_attempted=True,
            )
        )
        session.commit()

    class NoThirdSubmitClient:
        def __init__(self, _base_url, _cdk_key=""):
            pass

        def get_submission(self, submission_id):
            return {"ok": True, "data": {"id": submission_id, "state": "unknown"}}

        def submit_payment(self, _payment_url):
            raise AssertionError("补偿已使用后不得第三次提交")

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", NoThirdSubmitClient)

    result = service.poll_scanner(account_id)

    assert result["state"] == "scanner_accepted_untracked"
    assert result["scanner_status"] == "SUBMIT_UNCONFIRMED"
    assert result["scanner_recovery_deadline_at"] is not None
    assert result["scanner_submit_attempts"] == 2


def test_546789_ambiguous_first_submit_immediately_uses_one_compensation(monkeypatch):
    account_id = _create_account("workstation-first-timeout@test.com")
    service = KakaoPipelineService()

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from sqlmodel import Session

    from core.db import engine

    ProviderDefinitionsRepository().ensure_seeded()
    setting = service.save_setting(
        "scanner_546789",
        {
            "display_name": "546789",
            "base_url": "https://kakao.example",
            "cdk_keys": "workstation-cdk",
        },
    )
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/first-timeout",
            )
        )
        session.commit()

    calls = 0

    class FirstTimeoutClient:
        def __init__(self, _base_url, _cdk_key=""):
            pass

        def submit_payment(self, _payment_url):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise CustomerApiProblem(503, "network_error", "连接超时")
            return {"submissions": [{"id": "compensated-submission", "state": "queued"}]}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", FirstTimeoutClient)

    result = service.submit_scanner(
        account_id,
        scanner_setting_id=setting["id"],
        scanner_kind="scanner_546789",
    )

    assert calls == 2
    assert result["state"] == "scanner_processing"
    assert result["scanner_order_id"] == "compensated-submission"
    assert result["scanner_submit_attempts"] == 2
    assert result["scanner_compensation_attempted"] is True


def test_546789_two_ambiguous_submits_enter_30_minute_observation(monkeypatch):
    account_id = _create_account("workstation-double-timeout@test.com")
    service = KakaoPipelineService()

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from sqlmodel import Session

    from core.db import engine

    ProviderDefinitionsRepository().ensure_seeded()
    setting = service.save_setting(
        "scanner_546789",
        {
            "display_name": "546789",
            "base_url": "https://kakao.example",
            "cdk_keys": "workstation-cdk",
        },
    )
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/double-timeout",
            )
        )
        session.commit()

    calls = 0

    class DoubleTimeoutClient:
        def __init__(self, _base_url, _cdk_key=""):
            pass

        def submit_payment(self, _payment_url):
            nonlocal calls
            calls += 1
            raise CustomerApiProblem(503, "network_error", "连接超时")

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", DoubleTimeoutClient)

    result = service.submit_scanner(
        account_id,
        scanner_setting_id=setting["id"],
        scanner_kind="scanner_546789",
    )

    assert calls == 2
    assert result["state"] == "scanner_accepted_untracked"
    assert result["scanner_status"] == "SUBMIT_UNCONFIRMED"
    assert result["scanner_submit_attempts"] == 2
    started = datetime.fromisoformat(result["scanner_recovery_started_at"])
    deadline = datetime.fromisoformat(result["scanner_recovery_deadline_at"])
    assert timedelta(minutes=29, seconds=59) <= deadline - started <= timedelta(minutes=30, seconds=1)


def test_i7_ambiguous_submit_enters_observation_and_cannot_resubmit(monkeypatch):
    account_id = _create_account("i7-submit-timeout@test.com")
    service = KakaoPipelineService()

    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from sqlmodel import Session

    from core.db import engine

    ProviderDefinitionsRepository().ensure_seeded()
    setting = service.save_setting(
        "scanner",
        {
            "display_name": "I7wap",
            "base_url": "https://scanner.example",
            "cdk_keys": "scanner-cdk",
        },
    )
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/i7-timeout",
            )
        )
        session.commit()

    class TimeoutCustomerClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_scanner(self, *_args, **_kwargs):
            raise CustomerApiProblem(503, "network_error", "连接超时")

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", TimeoutCustomerClient)

    result = service.submit_scanner(
        account_id,
        scanner_setting_id=setting["id"],
        scanner_kind="scanner",
    )

    assert result["state"] == "scanner_accepted_untracked"
    assert result["scanner_status"] == "SUBMIT_UNCONFIRMED"
    assert result["scanner_submit_attempts"] == 1

    try:
        service.submit_scanner(account_id, scanner_setting_id=setting["id"], scanner_kind="scanner")
    except ValueError as exc:
        assert "必须先成功提取" in str(exc)
    else:
        raise AssertionError("结果不确定后不应允许再次上传")


def test_supplier_processing_timeout_preserves_original_order(monkeypatch):
    account_id = _create_account("supplier-processing-timeout@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    started_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="supplier_processing",
                supplier_order_id="existing-supplier-order",
                supplier_processing_started_at=started_at,
                supplier_deadline_at=started_at + timedelta(minutes=15),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.CustomerApiClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("后台到期后不应请求上游")),
    )

    result = service.advance_background(account_id, expected_state="supplier_processing")

    assert result["state"] == "supplier_poll_failed"
    assert result["supplier_order_id"] == "existing-supplier-order"
    assert result["last_error_code"] == "supplier_processing_timeout"


def test_force_reset_can_clear_stuck_active_state():
    account_id = _create_account("force-reset@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/stuck-task",
            )
        )
        session.commit()

    reset = service.reset(account_id, force=True)

    assert reset["state"] == "idle"
    listed = service.list_accounts(search="force-reset@test.com")
    assert listed["items"][0]["pipeline"]["state"] == "idle"


def test_manual_plus_check_preserves_pipeline_stage(monkeypatch):
    account_id = _create_account("manual-plus-check@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="link_ready",
                payment_url="https://pay.nicepay.co.kr/v1/checkout/pay/manual-check",
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )

    checked = service.check_plus(account_id, advance_pipeline=False)

    assert checked["state"] == "link_ready"
    assert checked["payment_url"].endswith("/manual-check")


def test_pipeline_plus_check_advances_after_scanner_success(monkeypatch):
    account_id = _create_account("pipeline-plus-check@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded"))
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    checked = service.check_plus(account_id, advance_pipeline=True)

    assert checked["state"] == "plus_pending"
    assert checked["plus_check_count"] == 1
    assert checked["plus_check_started_at"] is not None
    assert checked["plus_next_check_at"] is not None
    started = datetime.fromisoformat(checked["plus_check_started_at"])
    next_check = datetime.fromisoformat(checked["plus_next_check_at"])
    deadline = datetime.fromisoformat(checked["plus_check_deadline_at"])
    assert timedelta(seconds=4) <= next_check - started <= timedelta(seconds=6)
    assert timedelta(minutes=9, seconds=59) <= deadline - started <= timedelta(minutes=10, seconds=1)
    assert next_check <= deadline


def test_pipeline_plus_query_failure_uses_same_persisted_ladder(monkeypatch):
    account_id = _create_account("failed-plus-query-interval@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded"))
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="temporary query failure"),
    )
    checked = service.check_plus(account_id, advance_pipeline=True)

    assert checked["state"] == "plus_pending"
    assert checked["plus_check_count"] == 1
    started = datetime.fromisoformat(checked["plus_check_started_at"])
    next_check = datetime.fromisoformat(checked["plus_next_check_at"])
    deadline = datetime.fromisoformat(checked["plus_check_deadline_at"])
    assert timedelta(seconds=4) <= next_check - started <= timedelta(seconds=6)
    assert next_check <= deadline
    assert service.list_background_work() == []


def test_pipeline_plus_check_relogins_invalid_account_before_rechecking(monkeypatch):
    account_id = _create_account("invalid-plus-relogin@test.com")
    service = KakaoPipelineService()
    service.set_account_proxy("manual", "http://127.0.0.1:7897")

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded"))
        session.commit()

    actions = []

    def execute_action(_runtime, command, **_kwargs):
        actions.append((command.action_id, dict(command.params)))
        if command.action_id == "relogin":
            _kwargs["log_fn"]("重新登录浏览器：已进入账号验证页面")
            return SimpleNamespace(ok=True, data={"checked_at": "2099-01-01T00:00:00Z"}, error="")
        if sum(action_id == "query_state" for action_id, _params in actions) == 1:
            return SimpleNamespace(ok=True, data={"valid": False}, error="")
        return SimpleNamespace(ok=True, data={"valid": True, "plan": "plus"}, error="")

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        execute_action,
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "plus", "state": "subscribed"},
            }
        },
    )

    result = service.check_plus(account_id, advance_pipeline=True)

    assert [action_id for action_id, _params in actions] == ["query_state", "relogin", "query_state"]
    assert all(params["platform_proxy_mode"] == "manual" for _action_id, params in actions)
    assert all(params["platform_proxy_value"] == "http://127.0.0.1:7897" for _action_id, params in actions)
    assert result["state"] == "completed"
    assert result["final_result"] == "plus"
    messages = [item["message"] for item in result["events"]]
    assert any("登录已失效" in message for message in messages)
    assert any("已进入账号验证页面" in message for message in messages)
    assert any("重新登录成功" in message for message in messages)


def test_pipeline_plus_check_pauses_when_invalid_account_relogin_fails(monkeypatch):
    account_id = _create_account("invalid-plus-relogin-failed@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded"))
        session.commit()

    actions = []

    def execute_action(_runtime, command, **_kwargs):
        actions.append(command.action_id)
        if command.action_id == "query_state":
            return SimpleNamespace(ok=True, data={"valid": False}, error="")
        return SimpleNamespace(
            ok=False,
            data={"failure_code": "credentials_invalid"},
            error="重新登录失败 [credentials_invalid]: 密码错误",
        )

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        execute_action,
    )

    result = service.check_plus(account_id, advance_pipeline=True)

    assert actions == ["query_state", "relogin"]
    assert result["state"] == "plus_unconfirmed"
    assert result["last_error_code"] == "plus_relogin_failed"
    assert "密码错误" in result["last_error_message"]
    assert result["plus_next_check_at"] is None
    assert service.list_background_work() == []


def test_pipeline_plus_check_does_not_relogin_indeterminate_account_state(monkeypatch):
    account_id = _create_account("unknown-plus-state@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded"))
        session.commit()

    actions = []

    def execute_action(_runtime, command, **_kwargs):
        actions.append(command.action_id)
        return SimpleNamespace(ok=True, data={"valid": None, "last_error": "rate limited"}, error="")

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        execute_action,
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "free", "state": "free"},
            }
        },
    )

    result = service.check_plus(account_id, advance_pipeline=True)

    assert actions == ["query_state"]
    assert result["state"] == "plus_pending"
    assert result["plus_check_count"] == 1


def test_pipeline_plus_check_uses_random_interval_from_sixth_check(monkeypatch):
    account_id = _create_account("sixth-plus-check-random@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="plus_pending",
                plus_check_count=5,
                plus_check_started_at=now - timedelta(minutes=2),
                plus_check_deadline_at=now + timedelta(minutes=28),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        "features.kakao_pipeline.service.random.randint",
        lambda lower, upper: 75 if (lower, upper) == (60, 120) else lower,
    )

    check_started = datetime.now(timezone.utc)
    checked = service.check_plus(account_id, advance_pipeline=True)
    next_check = datetime.fromisoformat(checked["plus_next_check_at"]).replace(tzinfo=timezone.utc)

    assert checked["state"] == "plus_pending"
    assert checked["plus_check_count"] == 6
    assert timedelta(seconds=74) <= next_check - check_started <= timedelta(seconds=76)


def test_normal_plus_confirmation_completes_only_with_fresh_query_result(monkeypatch):
    account_id = _create_account("fresh-normal-plus@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="scanner_succeeded", scanner_status="COMPLETED"))
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "plus", "state": "subscribed"},
            }
        },
    )

    result = service.check_plus(
        account_id,
        advance_pipeline=True,
        enable_post_actions=True,
    )

    assert result["state"] == "completed"
    assert result["completion_source"] == "normal_scanner"
    assert result["plus_next_check_at"] is None
    assert result["post_actions"]["codex"]["status"] == "pending"
    codex_task_id = result["post_actions"]["codex"]["task_id"]
    assert codex_task_id

    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        task = session.get(TaskModel, codex_task_id)
        assert pipeline.state == "completed"
        assert pipeline.codex_post_action_armed is True
        assert pipeline.codex_task_id == codex_task_id
        assert task is not None
        assert task.get_payload()["source"] == "kakao_pipeline"
        assert task.get_payload()["auto_push_after_oauth"] is False


def test_default_workflow_owned_plus_completion_does_not_start_page_codex(monkeypatch):
    account_id = _create_account("workflow-owned-plus@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_succeeded",
                scanner_status="COMPLETED",
                codex_post_action_armed=False,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "plus", "state": "subscribed"},
            }
        },
    )

    result = service.check_plus(
        account_id,
        advance_pipeline=True,
        enable_post_actions=False,
    )

    assert result["state"] == "completed"
    assert result["post_actions"]["codex"]["status"] == "waiting"
    assert result["post_actions"]["codex"]["task_id"] is None
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert pipeline.codex_post_action_armed is False
        assert pipeline.codex_task_id == ""


def test_completed_pipeline_cannot_regress_during_account_refresh(monkeypatch):
    account_id = _create_account("completed-no-regression@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    completed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completion_source="normal_scanner",
                completed_at=completed_at,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "free", "state": "free"},
            }
        },
    )

    result = service.check_plus(account_id, advance_pipeline=True)

    assert result["state"] == "completed"
    assert result["plus_status"] == "plus"
    assert result["final_result"] == "plus"
    assert result["completion_source"] == "normal_scanner"
    assert datetime.fromisoformat(result["completed_at"]) == completed_at.replace(tzinfo=None)


def test_scanner_processing_timeout_preserves_order_for_manual_poll(monkeypatch):
    account_id = _create_account("scanner-processing-timeout@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                scanner_driver="customer_api",
                scanner_order_id="existing-order",
                scanner_processing_started_at=started_at,
                scanner_deadline_at=started_at + timedelta(minutes=30),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.CustomerApiClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("后台到期后不应请求上游")),
    )

    result = service.advance_background(account_id, expected_state="scanner_processing")

    assert result["state"] == "scanner_poll_failed"
    assert result["scanner_order_id"] == "existing-order"
    assert result["last_error_code"] == "scanner_processing_timeout"


def test_manual_scanner_poll_reuses_existing_order_after_timeout(monkeypatch):
    account_id = _create_account("manual-existing-scanner-order@test.com")
    service = KakaoPipelineService()

    from sqlmodel import Session

    from core.db import engine

    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_poll_failed",
                scanner_driver="customer_api",
                scanner_base_url="https://scanner.example",
                scanner_cdk_key="cdk",
                scanner_order_id="existing-order",
                scanner_poll_url="/api/v1/customer/orders/existing-order",
                scanner_customer_token="customer-token",
                scanner_processing_started_at=datetime.now(timezone.utc) - timedelta(minutes=31),
                scanner_deadline_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        session.commit()

    class ExistingOrderClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_order(self, poll_url, customer_token):
            assert poll_url.endswith("existing-order")
            assert customer_token == "customer-token"
            return {"data": {"status": "COMPLETED", "subscription": {"status": "PLUS"}}}

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", ExistingOrderClient)

    result = service.poll_scanner(account_id)

    assert result["state"] == "scanner_succeeded"
    assert result["scanner_order_id"] == "existing-order"


def test_legacy_plus_pending_older_than_10_minutes_is_persistently_paused(monkeypatch):
    account_id = _create_account("legacy-plus-pending@test.com")
    service = KakaoPipelineService()

    from datetime import timedelta
    from sqlmodel import Session

    from core.db import engine

    old_time = datetime.now(timezone.utc) - timedelta(minutes=11)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="plus_pending",
                scanner_status="COMPLETED",
                updated_at=old_time,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("过期窗口不应再查询 Plus")),
    )

    result = service.advance_background(account_id, expected_state="plus_pending")

    assert result["state"] == "plus_unconfirmed"
    assert result["last_error_code"] == "plus_unconfirmed"
    assert result["plus_next_check_at"] is None
    assert result["plus_check_paused_at"] is not None
    assert account_id not in {item["account_id"] for item in service.list_background_work()}


def test_manual_plus_check_does_not_restart_paused_10_minute_window(monkeypatch):
    account_id = _create_account("manual-paused-plus@test.com")
    service = KakaoPipelineService()

    from datetime import timedelta
    from sqlmodel import Session

    from core.db import engine

    started_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    legacy_deadline_at = started_at + timedelta(minutes=30)
    expected_deadline_at = started_at + timedelta(minutes=10)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="plus_unconfirmed",
                scanner_status="COMPLETED",
                plus_check_count=8,
                plus_check_started_at=started_at,
                plus_check_deadline_at=legacy_deadline_at,
                plus_check_paused_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.PlatformRuntime.execute_action",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    monkeypatch.setattr(
        service.accounts,
        "get_account",
        lambda _account_id: {
            "account_view": {
                "status": {"checked_at": "2099-01-01T00:00:00Z"},
                "subscription": {"plan": "free", "state": "free"},
            }
        },
    )

    result = service.check_plus(account_id, advance_pipeline=True)

    assert result["state"] == "plus_unconfirmed"
    assert datetime.fromisoformat(result["plus_check_started_at"]) == started_at.replace(tzinfo=None)
    assert datetime.fromisoformat(result["plus_check_deadline_at"]) == expected_deadline_at.replace(tzinfo=None)
    assert result["plus_next_check_at"] is None


def test_546789_expired_keeps_link_and_quota_removal_is_isolated(monkeypatch):
    account_id = _create_account("expired-workstation@test.com")
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    ProviderDefinitionsRepository().ensure_seeded()
    service = KakaoPipelineService()
    service.save_setting(
        "scanner",
        {"display_name": "I7wap", "base_url": "https://customer.example", "cdk_keys": "i7wap-cdk"},
    )
    setting = service.save_setting(
        "scanner_546789",
        {"display_name": "546789", "base_url": "https://kakao.example", "cdk_keys": "empty-cdk\nusable-cdk"},
    )

    class QuotaClient:
        def __init__(self, base_url, cdk_key=""):
            self.cdk_key = cdk_key

        def check_cdk(self):
            remaining = 0 if self.cdk_key == "empty-cdk" else 2
            return {"unlimited": False, "remaining": remaining, "limit": 5}

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", QuotaClient)
    checked = service.check_cdks("scanner_546789")
    assert checked["removed"] == ["empty-cdk"]
    assert service.list_settings()["scanner_546789"]["cdk_keys"] == ["usable-cdk"]
    assert service.list_settings()["scanner"]["cdk_keys"] == ["i7wap-cdk"]

    from sqlmodel import Session

    from core.db import KakaoPipelineModel, engine

    payment_url = "https://pay.nicepay.co.kr/v1/checkout/pay/expired-workstation"
    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=account_id, state="link_ready", payment_url=payment_url))
        session.commit()

    class ExpiredClient:
        def __init__(self, base_url, cdk_key=""):
            pass

        def submit_payment(self, payment_url):
            return {"submissions": [{"id": "expired-submission", "state": "pending"}]}

        def get_submission(self, submission_id):
            return {"ok": True, "data": {"id": submission_id, "state": "expired"}}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", ExpiredClient)
    service.submit_scanner(account_id, scanner_setting_id=setting["id"], scanner_kind="scanner_546789")
    expired = service.poll_scanner(account_id)
    assert expired["state"] == "scanner_failed"
    assert expired["last_error_code"] == "payment_link_expired"
    assert expired["payment_url"] == payment_url


def test_successful_extraction_can_auto_submit_to_default_scanner(monkeypatch):
    account_id = _create_account("auto-upload-kakao@test.com")
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    ProviderDefinitionsRepository().ensure_seeded()
    service = KakaoPipelineService()
    service.save_setting(
        "scanner_546789",
        {
            "display_name": "546789 自动扫码",
            "base_url": "https://kakao.example",
            "cdk_keys": "auto-cdk",
        },
    )
    service.set_default_scanner("scanner_546789")
    service.set_auto_upload(True)

    class FakeSupplierClient:
        def __init__(self, base_url, cdk_key):
            pass

        def create_extraction(self, access_token, *, payment_method):
            return {
                "data": {
                    "order": {"id": "auto-supplier", "status": "PENDING"},
                    "customerToken": "auto-token",
                    "pollUrl": "/api/v1/customer/orders/auto-supplier",
                }
            }

        def get_order(self, poll_url, customer_token):
            return {
                "data": {
                    "status": "READY",
                    "qualification": {
                        "zeroVerified": True,
                        "postPromoAmountKrw": 0,
                        "postTaxAmountKrw": 0,
                    },
                    "extraction": {
                        "paymentUrl": "https://pay.nicepay.co.kr/v1/checkout/pay/auto-upload",
                    },
                }
            }

    class FakeWorkstationClient:
        def __init__(self, base_url, cdk_key=""):
            assert base_url == "https://kakao.example"

        def submit_payment(self, payment_url):
            assert payment_url.endswith("/auto-upload")
            return {"submissions": [{"id": "auto-submission", "state": "pending"}]}

        def qr_url(self, submission_id):
            return f"https://kakao.example/api/payment-submissions/{submission_id}/qr.png"

    monkeypatch.setattr("features.kakao_pipeline.service.CustomerApiClient", FakeSupplierClient)
    monkeypatch.setattr("features.kakao_pipeline.service.WorkstationScannerClient", FakeWorkstationClient)
    monkeypatch.setenv("KAKAO_SUPPLIER_CDK_KEY", "supplier-cdk")

    service.start_extraction(account_id)
    result = service.poll_supplier(account_id)

    assert result["state"] == "scanner_processing"
    assert result["scanner_driver"] == "payment_submission"
    assert result["scanner_order_id"] == "auto-submission"
    assert result["scanner_name"] == "546789 自动扫码"


def test_kakao_account_list_uses_local_accounts_without_exposing_tokens(client):
    account_id = _create_account("list-kakao@test.com")
    response = client.get("/api/kakao-pipeline/accounts")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == account_id
    assert payload["items"][0]["account_view"]["security"]["phone_bound"] is False
    assert payload["items"][0]["pipeline"]["latest_event_at"] is None
    assert "access-token-for-kakao-test" not in response.text


def test_kakao_account_list_exposes_phone_and_codex_status_without_secrets(monkeypatch):
    from domain.accounts import AccountRecord

    account_id = _create_account("status@test.com")
    service = KakaoPipelineService()
    monkeypatch.setattr(
        service.accounts.repository,
        "_load_records",
        lambda _session, _models: [
            AccountRecord(
                id=account_id,
                platform="chatgpt",
                email="status@test.com",
                password="must-not-leak-password",
                account_view={
                    "identity": {"email": "status@test.com"},
                    "status": {"validity": "valid", "checked_at": "2026-08-01T08:00:00"},
                    "subscription": {"plan": "plus", "state": "subscribed"},
                    "security": {
                        "phone_bound": True,
                        "phone_number_masked": "+86****1234",
                    },
                    "codex": {
                        "authorized": True,
                        "has_access_token": True,
                        "access_token": "must-not-leak",
                    },
                },
            )
        ],
    )

    payload = service.list_accounts()

    item = payload["items"][0]
    assert item["account_view"]["security"] == {
        "phone_bound": True,
        "phone_number_masked": "+86****1234",
    }
    assert "must-not-leak" not in str(payload)


def test_kakao_account_list_requires_complete_codex_token_pair():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("strict-codex-status@test.com")
    with Session(engine) as session:
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=False,
            )
        )
        session.commit()

    service = KakaoPipelineService()
    incomplete = service.list_accounts()["items"][0]["pipeline"]["post_actions"]["codex"]
    assert incomplete["authorized"] is False

    with Session(engine) as session:
        auth = session.get(AccountCodexAuthModel, account_id)
        assert auth is not None
        auth.has_refresh_token = True
        session.add(auth)
        session.commit()

    complete = service.list_accounts()["items"][0]["pipeline"]["post_actions"]["codex"]
    assert complete["authorized"] is True


def test_succeeded_codex_task_without_matching_account_result_is_not_authorized():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("missing-codex-result@test.com")
    task = TaskModel(
        id="codex-missing-account-result",
        type="codex_oauth_batch",
        platform="chatgpt",
        status="succeeded",
    )
    task.set_result({"data": {"accounts": [{"account_id": account_id + 1, "ok": True}]}})
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
                codex_post_action_armed=True,
                codex_task_id=task.id,
                codex_attempt_count=1,
            )
        )
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=True,
            )
        )
        session.add(task)
        session.commit()

    codex = KakaoPipelineService().get_account_pipeline(account_id)["post_actions"]["codex"]

    assert codex["authorized"] is True
    assert codex["status"] == "failed"
    assert "complete credentials" in codex["error"]


def test_kakao_post_action_errors_are_redacted_before_api_serialization():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("redacted-task-error@test.com")
    secret_error = (
        "token endpoint failed for private.user@example.com +8613800138000: "
        "https://proxy-user:proxy-pass@example.com/callback?code=oauth-code&state=oauth-state "
        "Authorization: Bearer bearer-secret access_token='access-secret' "
        'refresh_token="refresh-secret" password=hunter2'
    )
    task = TaskModel(
        id="codex-secret-error",
        type="codex_oauth_batch",
        platform="chatgpt",
        status="failed",
        error=secret_error,
    )
    task.set_result(
        {
            "data": {
                "accounts": [
                    {"account_id": account_id, "ok": False, "error": secret_error},
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
                codex_task_id=task.id,
                codex_attempt_count=1,
            )
        )
        session.add(task)
        session.commit()

    error = KakaoPipelineService().get_account_pipeline(account_id)["post_actions"]["codex"]["error"]

    assert "***" in error
    for secret in (
        "private.user@example.com",
        "+8613800138000",
        "proxy-user",
        "proxy-pass",
        "oauth-code",
        "oauth-state",
        "bearer-secret",
        "access-secret",
        "refresh-secret",
        "hunter2",
    ):
        assert secret not in error


def test_old_delivery_does_not_complete_a_new_failed_linked_push():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("stale-push-delivery@test.com")
    now = datetime.now(timezone.utc)
    push_task = TaskModel(
        id="new-linked-push",
        type="account_push",
        platform="chatgpt",
        status="failed",
        error="new push failed",
        started_at=now,
        finished_at=now,
    )
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=now - timedelta(minutes=2),
                codex_post_action_armed=True,
                codex_skipped_at=now - timedelta(minutes=1),
                codex_push_task_id=push_task.id,
                codex_push_attempt_count=1,
            )
        )
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=True,
            )
        )
        session.add(push_task)
        session.add(
            AccountPushDeliveryModel(
                account_id=account_id,
                target_key="nvtokens",
                target_label="NexusVault",
                status="success",
                last_attempt_at=now - timedelta(minutes=5),
                pushed_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(minutes=5),
            )
        )
        session.commit()

    service = KakaoPipelineService()
    stale = service.get_account_pipeline(account_id)["post_actions"]["push"]
    assert stale["status"] == "failed"
    assert stale["error"] == "new push failed"

    with Session(engine) as session:
        delivery = session.exec(
            select(AccountPushDeliveryModel).where(
                AccountPushDeliveryModel.account_id == account_id
            )
        ).one()
        delivery.last_attempt_at = now + timedelta(seconds=1)
        delivery.pushed_at = now + timedelta(seconds=1)
        delivery.updated_at = now + timedelta(seconds=1)
        session.add(delivery)
        session.commit()

    retried = service.get_account_pipeline(account_id)["post_actions"]["push"]
    assert retried["status"] == "success"


def test_current_delivery_after_plus_and_auth_skips_duplicate_linked_push(monkeypatch):
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("current-delivery@test.com")
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=now - timedelta(minutes=5),
                codex_post_action_armed=True,
                codex_skipped_at=now - timedelta(minutes=4),
                codex_push_task_id="missing-linked-push",
                codex_push_attempt_count=1,
                codex_push_enqueue_error="automatic enqueue failed",
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
        session.add(
            AccountPushDeliveryModel(
                account_id=account_id,
                target_key="nvtokens",
                target_label="NexusVault",
                status="success",
                pushed_at=now - timedelta(minutes=1),
                last_attempt_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.get_nvtokens_auto_push_state",
        lambda: {"enabled": True, "reason": ""},
    )
    monkeypatch.setattr(
        "features.kakao_pipeline.service.enqueue_nvtokens_push_after_codex_oauth",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )

    result = KakaoPipelineService().advance_background(
        account_id,
        expected_state="codex_post_action",
    )

    assert result["post_actions"]["push"]["status"] == "success"
    assert result["post_actions"]["push"]["task_id"] is None
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert pipeline.codex_push_skip_reason == "already_delivered"
        assert pipeline.codex_push_task_id == ""
        assert pipeline.codex_push_enqueue_error == ""


def test_manual_current_delivery_overrides_an_automatic_push_skip(monkeypatch):
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("manual-after-disabled@test.com")
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=now - timedelta(minutes=5),
                codex_post_action_armed=True,
                codex_skipped_at=now - timedelta(minutes=4),
                codex_push_skip_reason="auto_push_disabled",
                codex_post_action_done_at=now - timedelta(minutes=3),
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
        session.add(
            AccountPushDeliveryModel(
                account_id=account_id,
                target_key="nvtokens",
                target_label="NexusVault",
                status="success",
                pushed_at=now - timedelta(minutes=1),
                last_attempt_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
        session.commit()

    monkeypatch.setattr(
        "features.kakao_pipeline.service.get_nvtokens_auto_push_state",
        lambda: {"enabled": False, "reason": "auto_push_disabled"},
    )

    push = KakaoPipelineService().get_account_pipeline(account_id)["post_actions"]["push"]

    assert push["status"] == "success"
    assert push["pushed_at"] is not None


@pytest.mark.parametrize(
    ("completed_delta", "refresh_delta", "delivery_delta"),
    [
        (timedelta(minutes=1), timedelta(minutes=10), timedelta(minutes=5)),
        (timedelta(minutes=10), timedelta(minutes=1), timedelta(minutes=5)),
    ],
)
def test_delivery_older_than_plus_or_current_auth_does_not_skip_push(
    monkeypatch,
    completed_delta,
    refresh_delta,
    delivery_delta,
):
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account(f"stale-current-delivery-{completed_delta.seconds}@test.com")
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=now - completed_delta,
                codex_post_action_armed=True,
                codex_skipped_at=now - timedelta(seconds=30),
            )
        )
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=True,
                last_refresh=now - refresh_delta,
            )
        )
        session.add(
            AccountPushDeliveryModel(
                account_id=account_id,
                target_key="nvtokens",
                target_label="NexusVault",
                status="success",
                pushed_at=now - delivery_delta,
                last_attempt_at=now - delivery_delta,
                updated_at=now - delivery_delta,
            )
        )
        session.commit()

    calls = []
    monkeypatch.setattr(
        "features.kakao_pipeline.service.get_nvtokens_auto_push_state",
        lambda: {"enabled": True, "reason": ""},
    )

    def enqueue(account_id: int, **kwargs):
        calls.append((account_id, kwargs))
        return {"enqueued": False, "reason": "enqueue_failed", "error": "temporary failure"}

    monkeypatch.setattr(
        "features.kakao_pipeline.service.enqueue_nvtokens_push_after_codex_oauth",
        enqueue,
    )

    result = KakaoPipelineService().advance_background(
        account_id,
        expected_state="codex_post_action",
    )

    assert calls and calls[0][1]["source"] == "kakao_pipeline"
    assert result["post_actions"]["push"]["status"] == "failed"
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert pipeline.codex_push_skip_reason == ""


def test_kakao_codex_endpoint_rejects_non_plus_account(client):
    account_id = _create_account("codex-gate@test.com")

    response = client.post(f"/api/kakao-pipeline/accounts/{account_id}/codex")

    assert response.status_code == 400
    assert "Plus" in response.text


def test_kakao_page_routes_persistently_enable_post_actions(client, monkeypatch):
    import api.kakao_pipeline as kakao_api

    calls: list[tuple[str, int, bool | None]] = []

    def start(
        account_id: int,
        supplier_setting_id: int | None = None,
        payment_method: str = "kakao_pay",
        *,
        enable_post_actions: bool = False,
    ):
        calls.append(("extract", account_id, enable_post_actions))
        return {"account_id": account_id, "state": "supplier_processing"}

    def check(
        account_id: int,
        *,
        advance_pipeline: bool = False,
        enable_post_actions: bool | None = None,
    ):
        calls.append(("plus", account_id, enable_post_actions))
        return {"account_id": account_id, "state": "plus_checking"}

    monkeypatch.setattr(kakao_api.service, "start_extraction", start)
    monkeypatch.setattr(kakao_api.service, "check_plus", check)

    extracted = client.post(
        "/api/kakao-pipeline/accounts/77/extract",
        json={"payment_method": "kakao_pay"},
    )
    checked = client.post(
        "/api/kakao-pipeline/accounts/77/plus/check",
        json={"advance_pipeline": True},
    )

    assert extracted.status_code == 200
    assert checked.status_code == 200
    assert calls == [("extract", 77, True), ("plus", 77, True)]


def test_kakao_codex_start_is_idempotent_while_task_is_active():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("codex-idempotent@test.com")
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    service = KakaoPipelineService()
    first = service.start_codex(account_id)
    second = service.start_codex(account_id)

    assert first["state"] == "completed"
    assert first["post_actions"]["codex"]["status"] == "pending"
    assert second["post_actions"]["codex"]["task_id"] == first["post_actions"]["codex"]["task_id"]
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert pipeline.codex_attempt_count == 1


def test_existing_valid_codex_auth_is_skipped_without_browser_task():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("codex-existing@test.com")
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
            )
        )
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=True,
            )
        )
        session.commit()

    result = KakaoPipelineService().start_codex(account_id)

    assert result["post_actions"]["codex"] == {
        "status": "skipped",
        "task_id": None,
        "authorized": True,
        "error": "",
        "attempt_count": 0,
    }
    assert result["post_actions"]["push"]["status"] == "skipped"
    with Session(engine) as session:
        pipeline = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert pipeline.codex_post_action_armed is True
        assert pipeline.codex_skipped_at is not None
        assert pipeline.codex_task_id == ""


def test_kakao_codex_manual_retry_uses_a_new_deterministic_task_id():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("codex-retry@test.com")
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    service = KakaoPipelineService()
    first = service.start_codex(account_id)
    first_id = first["post_actions"]["codex"]["task_id"]
    with Session(engine) as session:
        task = session.get(TaskModel, first_id)
        assert task is not None
        task.status = "failed"
        task.error = "oauth failed"
        task.finished_at = datetime.now(timezone.utc)
        session.add(task)
        session.commit()

    retried = service.start_codex(account_id)

    second_id = retried["post_actions"]["codex"]["task_id"]
    assert retried["post_actions"]["codex"]["status"] == "pending"
    assert second_id != first_id
    assert second_id.endswith("_2")


def test_kakao_reset_protects_and_force_cancels_active_codex_task():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("codex-reset@test.com")
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                plus_status="plus",
                final_result="plus",
                completed_at=datetime.now(timezone.utc),
            )
        )
        session.add(
            AccountCodexAuthModel(
                account_id=account_id,
                has_access_token=True,
                has_refresh_token=False,
            )
        )
        session.commit()

    service = KakaoPipelineService()
    started = service.start_codex(account_id)
    task_id = started["post_actions"]["codex"]["task_id"]

    with pytest.raises(ValueError, match="Codex authorization"):
        service.reset(account_id)

    reset = service.reset(account_id, force=True)

    assert reset["state"] == "idle"
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        auth = session.get(AccountCodexAuthModel, account_id)
        assert task is not None and task.status == "cancelled"
        assert auth is not None and auth.has_access_token is True


def test_kakao_archive_views_use_sql_pagination_and_only_hydrate_current_page(monkeypatch):
    from sqlmodel import Session

    from core.db import engine

    idle_id = _create_account("archive-idle@test.com")
    failed_id = _create_account("archive-failed@test.com")
    completed_id = _create_account("archive-completed@test.com")
    completed_older_result_id = _create_account("archive-completed-older@test.com")
    tail_pending_id = _create_account("archive-tail-pending@test.com")
    archived_id = _create_account("archive-hidden@test.com")
    purged_id = _create_account("archive-purged@test.com")
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(KakaoPipelineModel(account_id=failed_id, state="supplier_failed"))
        session.add(
            KakaoPipelineModel(
                account_id=completed_id,
                state="completed",
                final_result="plus",
                completed_at=now,
            )
        )
        session.add(
            KakaoPipelineModel(
                account_id=completed_older_result_id,
                state="completed",
                final_result="plus",
                completed_at=now - timedelta(minutes=5),
            )
        )
        session.add(
            KakaoPipelineModel(
                account_id=tail_pending_id,
                state="completed",
                final_result="plus",
                completed_at=now,
                codex_post_action_armed=True,
                codex_post_action_done_at=None,
            )
        )
        session.add(
            KakaoPipelineModel(
                account_id=archived_id,
                state="supplier_failed",
                archived_at=now,
                archive_disposition="abandoned",
            )
        )
        session.add(
            KakaoPipelineModel(
                account_id=purged_id,
                state="idle",
                archived_at=now - timedelta(minutes=5),
                archive_disposition="abandoned",
                purged_at=now,
            )
        )
        session.commit()

    service = KakaoPipelineService()
    original_load = service.accounts.repository._load_records
    hydrated: list[list[int]] = []

    def tracked_load(session, models):
        hydrated.append([int(model.id or 0) for model in models])
        return original_load(session, models)

    monkeypatch.setattr(service.accounts.repository, "_load_records", tracked_load)

    workspace = service.list_accounts()
    completed = service.list_accounts(view="completed")
    archived = service.list_accounts(view="archived")
    all_page = service.list_accounts(view="all", page=2, page_size=2)

    assert {item["id"] for item in workspace["items"]} == {
        idle_id,
        failed_id,
        tail_pending_id,
    }
    assert [item["id"] for item in completed["items"]] == [
        completed_id,
        completed_older_result_id,
    ]
    assert [item["id"] for item in archived["items"]] == [archived_id, purged_id]
    assert all_page["total"] == 7
    assert len(all_page["items"]) == 2
    assert len(hydrated[-1]) == 2
    assert service.list_accounts(search=str(completed_id), view="all")["items"][0]["id"] == completed_id
    assert service.list_accounts(search="ARCHIVE-COMPLETED@TEST", view="all")["total"] == 1


def test_kakao_archive_schema_helpers_migrate_legacy_table_idempotently(monkeypatch):
    from sqlalchemy import create_engine, inspect
    from sqlmodel import SQLModel

    from core import db as db_module

    legacy_engine = create_engine("sqlite://")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE kakao_pipelines ("
            "id INTEGER PRIMARY KEY, "
            "account_id INTEGER NOT NULL UNIQUE, "
            "state TEXT NOT NULL DEFAULT 'idle', "
            "final_result TEXT NOT NULL DEFAULT '', "
            "updated_at DATETIME"
            ")"
        )

    monkeypatch.setattr(db_module, "engine", legacy_engine)
    SQLModel.metadata.create_all(legacy_engine)
    for column_name, column_type in (
        ("archived_at", "DATETIME"),
        ("archive_reason", "TEXT DEFAULT ''"),
        ("archive_disposition", "TEXT DEFAULT ''"),
        ("purged_at", "DATETIME"),
    ):
        db_module._ensure_column("kakao_pipelines", column_name, column_type)
    for _ in range(2):
        db_module._ensure_index(
            "kakao_pipelines",
            "ix_kakao_pipelines_archive_state_updated",
            ("archived_at", "state", "final_result", "updated_at", "id"),
        )
        db_module._ensure_index(
            "kakao_pipelines",
            "ix_kakao_pipelines_archive_purged",
            ("archived_at", "purged_at"),
        )
    SQLModel.metadata.create_all(legacy_engine)

    inspector = inspect(legacy_engine)
    columns = {item["name"] for item in inspector.get_columns("kakao_pipelines")}
    indexes = [item["name"] for item in inspector.get_indexes("kakao_pipelines")]
    assert {"archived_at", "archive_reason", "archive_disposition", "purged_at"} <= columns
    assert indexes.count("ix_kakao_pipelines_archive_state_updated") == 1
    assert indexes.count("ix_kakao_pipelines_archive_purged") == 1


def test_kakao_archive_creates_idle_row_and_restore_preserves_account():
    from sqlmodel import Session

    from core.db import AccountModel, engine

    account_id = _create_account("archive-create-idle@test.com")
    service = KakaoPipelineService()

    archived = service.archive_accounts([account_id], reason="operator cleanup")

    assert archived["ok"] is True
    assert archived["items"][0]["pipeline"]["archive_disposition"] == "abandoned"
    assert service.list_accounts(view="workspace")["total"] == 0
    assert service.list_accounts(view="archived")["items"][0]["id"] == account_id

    restored = service.restore_accounts([account_id])

    assert restored["ok"] is True
    assert restored["items"][0]["pipeline"]["archived_at"] is None
    assert service.list_accounts(view="workspace")["items"][0]["id"] == account_id
    with Session(engine) as session:
        assert session.get(AccountModel, account_id) is not None


def test_kakao_archive_active_requires_force_and_purge_waits_for_local_task():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("archive-active@test.com")
    task_id = "archive-running-task"
    with Session(engine) as session:
        session.add(TaskModel(id=task_id, type="codex_oauth_batch", status="running"))
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="scanner_processing",
                scanner_order_id="remote-order",
                codex_task_id=task_id,
            )
        )
        session.commit()

    service = KakaoPipelineService()
    refused = service.archive_accounts([account_id])
    assert refused["ok"] is False
    assert "force" in refused["items"][0]["error"]

    forced = service.archive_accounts([account_id], force=True)
    assert forced["ok"] is True
    assert forced["items"][0]["warnings"]
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        assert task is not None and task.status == "cancel_requested"

    blocked_purge = service.purge_archived_accounts([account_id])
    assert blocked_purge["ok"] is False
    assert "稍后再清除" in blocked_purge["items"][0]["error"]


def test_kakao_force_archive_retries_cancel_for_already_archived_task(monkeypatch):
    from application import tasks as task_module
    from core.db import engine
    from sqlmodel import Session

    account_id = _create_account("archive-retry-cancel@test.com")
    task_id = "archive-retry-running-task"
    with Session(engine) as session:
        session.add(TaskModel(id=task_id, type="codex_oauth_batch", status="running"))
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="completed",
                final_result="plus",
                codex_task_id=task_id,
            )
        )
        session.commit()

    real_request_cancel = task_module.request_cancel
    attempts: list[str] = []

    def fail_once(current_task_id: str):
        attempts.append(current_task_id)
        if len(attempts) == 1:
            raise RuntimeError("temporary cancel failure")
        return real_request_cancel(current_task_id)

    monkeypatch.setattr(task_module, "request_cancel", fail_once)
    service = KakaoPipelineService()

    first = service.archive_accounts([account_id], force=True)
    assert first["ok"] is True
    assert first["items"][0]["changed"] is True
    assert "取消失败" in first["items"][0]["warnings"][-1]

    retried = service.archive_accounts([account_id], force=True)
    assert retried["ok"] is True
    assert retried["items"][0]["changed"] is False
    assert attempts == [task_id, task_id]
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        assert task is not None and task.status == "cancel_requested"


def test_kakao_purge_keeps_tombstone_and_clears_sensitive_details():
    from sqlmodel import Session

    from core.db import AccountModel, engine

    account_id = _create_account("archive-purge@test.com")
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        pipeline = KakaoPipelineModel(
            account_id=account_id,
            state="supplier_failed",
            supplier_base_url="https://supplier.example",
            supplier_cdk_key="secret-cdk",
            supplier_customer_token="secret-token",
            supplier_order_id="secret-order",
            payment_url="https://pay.example/secret",
            scanner_cdk_key="scanner-secret",
            codex_task_id="old-task-detail",
            archived_at=now,
            archive_reason="privacy cleanup",
            archive_disposition="abandoned",
        )
        pipeline.set_supplier_response({"customerToken": "response-secret"})
        pipeline.set_scanner_response({"secret": "scanner-response-secret"})
        pipeline.set_events([{"message": "secret event"}])
        session.add(pipeline)
        session.commit()

    service = KakaoPipelineService()
    purged = service.purge_archived_accounts([account_id])

    assert purged["ok"] is True
    detail = service.get_account_pipeline(account_id)
    assert detail["purged_at"] is not None
    assert detail["archived_at"] is not None
    assert detail["archive_reason"] == "privacy cleanup"
    assert detail["state"] == "idle"
    assert detail["events"] == []
    assert detail["supplier_response"] == {}
    assert detail["scanner_response"] == {}
    assert "secret" not in str(detail).lower()
    assert service.restore_accounts([account_id])["ok"] is False
    with Session(engine) as session:
        assert session.get(AccountModel, account_id) is not None
        tombstone = session.exec(
            select(KakaoPipelineModel).where(KakaoPipelineModel.account_id == account_id)
        ).one()
        assert tombstone.supplier_cdk_key == ""
        assert tombstone.codex_task_id == ""


def test_archived_pipeline_is_not_reconciled_and_original_operations_reject_it():
    from sqlmodel import Session

    from core.db import engine

    account_id = _create_account("archive-guard@test.com")
    with Session(engine) as session:
        session.add(
            KakaoPipelineModel(
                account_id=account_id,
                state="supplier_processing",
                supplier_order_id="must-not-poll",
                archived_at=datetime.now(timezone.utc),
                archive_disposition="abandoned",
            )
        )
        session.commit()

    service = KakaoPipelineService()

    assert service.list_background_work() == []
    assert service.get_account_pipeline(account_id)["archived_at"] is not None
    with pytest.raises(ValueError, match="已归档"):
        service.advance_background(account_id, expected_state="supplier_processing")
    with pytest.raises(ValueError, match="已归档"):
        service.start_extraction(account_id)
    with pytest.raises(ValueError, match="已归档"):
        service.reset(account_id, force=True)


def test_kakao_archive_api_supports_archive_restore_and_purge(client):
    account_id = _create_account("archive-api@test.com")

    archived = client.post(
        "/api/kakao-pipeline/archive",
        json={
            "account_ids": [account_id],
            "reason": "api archive",
            "disposition": "auto",
        },
    )
    assert archived.status_code == 200
    assert archived.json()["items"][0]["pipeline"]["archive_disposition"] == "abandoned"

    restored = client.post(
        "/api/kakao-pipeline/archive/restore",
        json={"account_ids": [account_id]},
    )
    assert restored.status_code == 200
    assert restored.json()["items"][0]["pipeline"]["archived_at"] is None

    client.post(
        "/api/kakao-pipeline/archive",
        json={"account_ids": [account_id], "disposition": "abandoned"},
    )
    purged = client.post(
        "/api/kakao-pipeline/archive/purge",
        json={"account_ids": [account_id]},
    )
    assert purged.status_code == 200
    assert purged.json()["items"][0]["pipeline"]["purged_at"] is not None
