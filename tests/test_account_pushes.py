from __future__ import annotations

import json

from sqlmodel import Session, select

from core.base_platform import Account
from core.db import AccountPushDeliveryModel, save_account, engine


def _create_pushable_account(*, with_codex: bool = True, email: str = "pushable@example.com", extra_updates: dict | None = None):
    extra = {
        "access_token": "platform-access-secret",
        "refresh_token": "platform-refresh-secret",
    }
    if with_codex:
        extra.update({
            "codex_access_token": "codex-access-secret",
            "codex_refresh_token": "codex-refresh-secret",
        })
    extra.update(extra_updates or {})
    return save_account(Account(
        platform="chatgpt",
        email=email,
        password="Password123!",
        extra=extra,
    ))


def _configure_nvtokens(client):
    response = client.post("/api/provider-settings", json={
        "provider_type": "push",
        "provider_key": "nvtokens",
        "display_name": "NexusVault",
        "auth_mode": "apikey",
        "enabled": True,
        "is_default": True,
        "config": {
            "nvtokens_endpoint": "https://nvtokens.test/api/inventory/cards/import",
            "nvtokens_payload_format": "codex",
            "nvtokens_timeout": "5",
        },
        "auth": {"nvtokens_api_key": "test-api-key"},
    })
    assert response.status_code == 200


def _configure_cpa(client, *, auto_push: bool = False):
    response = client.post("/api/provider-settings", json={
        "provider_type": "push",
        "provider_key": "cpa",
        "display_name": "CPA（CLIProxyAPI）",
        "auth_mode": "bearer",
        "enabled": True,
        "is_default": True,
        "config": {
            "cpa_api_url": "http://127.0.0.1:8317",
            "cpa_auto_push_after_codex_oauth": "true" if auto_push else "false",
            "cpa_timeout": "5",
        },
        "auth": {"cpa_api_key": "management-secret"},
    })
    assert response.status_code == 200


def test_push_requires_configured_target(client):
    account = _create_pushable_account()
    response = client.post("/api/accounts/push", json={
        "ids": [account.id],
        "select_all": False,
    })
    assert response.status_code == 400
    assert "推送目标" in response.json()["detail"]


def test_push_codex_payload_and_records_delivery(client, monkeypatch):
    _configure_nvtokens(client)
    account = _create_pushable_account()
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("providers.push.nvtokens.httpx.post", fake_post)

    response = client.post("/api/accounts/push", json={
        "ids": [account.id],
        "select_all": False,
        "target_key": "nvtokens",
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["succeeded"] == 1
    assert payload["failed"] == 0
    assert captured["headers"]["x-api-key"] == "test-api-key"
    assert captured["json"] == {
        "data": {
            "access_token": "codex-access-secret",
            "refresh_token": "codex-refresh-secret",
            "email": "pushable@example.com",
            "type": "codex",
        }
    }

    with Session(engine) as session:
        delivery = session.exec(select(AccountPushDeliveryModel)).one()
        assert delivery.account_id == account.id
        assert delivery.target_key == "nvtokens"
        assert delivery.status == "success"
        assert delivery.attempt_count == 1
        assert delivery.http_status == 200
        assert delivery.pushed_at is not None

    listed = client.get("/api/accounts", params={"platform": "chatgpt"}).json()
    status = listed["items"][0]["push_deliveries"][0]
    assert status["target_key"] == "nvtokens"
    assert status["status"] == "success"
    assert status["last_attempt_at"].endswith("Z")
    assert status["pushed_at"].endswith("Z")
    assert "codex-access-secret" not in str(status)


def test_push_cpa_uploads_raw_codex_auth_file_and_records_delivery(client, monkeypatch):
    _configure_cpa(client)
    account = _create_pushable_account(extra_updates={
        "codex_account_id": "acct-cpa",
        "codex_id_token": "codex-id-secret",
    })
    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_post(url, *, headers, content, timeout):
        captured.update(
            url=url,
            headers=headers,
            payload=json.loads(content),
            timeout=timeout,
        )
        return FakeResponse()

    monkeypatch.setattr("providers.push.cpa.httpx.post", fake_post)

    response = client.post("/api/accounts/push", json={
        "ids": [account.id],
        "target_key": "cpa",
    })

    assert response.status_code == 200
    assert response.json()["payload_format"] == "codex"
    assert response.json()["succeeded"] == 1
    assert captured["url"] == (
        "http://127.0.0.1:8317/v0/management/auth-files"
        "?name=pushable%40example.com_codex.json"
    )
    assert captured["headers"]["authorization"] == "Bearer management-secret"
    assert captured["payload"] == {
        "type": "codex",
        "id_token": "codex-id-secret",
        "access_token": "codex-access-secret",
        "refresh_token": "codex-refresh-secret",
        "account_id": "acct-cpa",
        "last_refresh": captured["payload"]["last_refresh"],
        "email": "pushable@example.com",
        "expired": "",
        "account_note": "",
    }

    targets = client.get("/api/accounts/push-targets").json()["items"]
    assert targets == [{
        "key": "cpa",
        "label": "CPA（CLIProxyAPI）",
        "is_default": True,
        "payload_format": "codex",
    }]

    with Session(engine) as session:
        delivery = session.exec(select(AccountPushDeliveryModel)).one()
        assert delivery.target_key == "cpa"
        assert delivery.payload_format == "codex"
        assert delivery.status == "success"


def test_cpa_provider_test_checks_management_endpoint(client, monkeypatch):
    _configure_cpa(client)

    class FakeResponse:
        status_code = 405

    called = {}

    def fake_options(url, *, headers, timeout):
        called.update(url=url, headers=headers, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("providers.push.cpa.httpx.options", fake_options)
    response = client.post("/api/provider-settings/test", json={
        "provider_type": "push",
        "provider_key": "cpa",
        "config": {"cpa_api_url": "http://127.0.0.1:8317", "cpa_timeout": "5"},
        "auth": {"cpa_api_key": "management-secret"},
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "CPA 管理接口连接成功"}
    assert called["url"] == "http://127.0.0.1:8317/v0/management/auth-files"
    assert called["headers"]["authorization"] == "Bearer management-secret"


def test_codex_push_never_falls_back_to_platform_tokens(client, monkeypatch):
    _configure_nvtokens(client)
    account = _create_pushable_account(with_codex=False)
    remote_called = False

    def fake_post(*args, **kwargs):
        nonlocal remote_called
        remote_called = True
        raise AssertionError("缺少 Codex 凭据时不应请求远端")

    monkeypatch.setattr("providers.push.nvtokens.httpx.post", fake_post)

    response = client.post("/api/accounts/push", json={"ids": [account.id]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["succeeded"] == 0
    assert payload["failed"] == 1
    assert payload["results"][0]["error"] == "账号缺少 Codex access_token"
    assert remote_called is False

    with Session(engine) as session:
        delivery = session.exec(select(AccountPushDeliveryModel)).one()
        assert delivery.status == "failed"
        assert delivery.http_status == 0
        assert delivery.last_error == "账号缺少 Codex access_token"


def test_failed_push_records_safe_http_error(client, monkeypatch):
    _configure_nvtokens(client)
    account = _create_pushable_account()

    class FakeResponse:
        status_code = 401

    monkeypatch.setattr(
        "providers.push.nvtokens.httpx.post",
        lambda *args, **kwargs: FakeResponse(),
    )

    response = client.post("/api/accounts/push", json={"ids": [account.id]})
    assert response.status_code == 200
    assert response.json()["failed"] == 1

    with Session(engine) as session:
        delivery = session.exec(select(AccountPushDeliveryModel)).one()
        assert delivery.status == "failed"
        assert delivery.http_status == 401
        assert delivery.last_error == "远端返回 HTTP 401"
        assert "test-api-key" not in delivery.last_error

    listed = client.get("/api/accounts", params={"platform": "chatgpt"}).json()
    status = listed["items"][0]["push_deliveries"][0]
    assert status["last_attempt_at"].endswith("Z")
    assert status["pushed_at"] is None


def test_push_select_all_uses_complete_v2_filter_result(client, monkeypatch):
    _configure_nvtokens(client)
    matched = _create_pushable_account(
        email="push-filter-match@example.com",
        extra_updates={"region": "US", "account_source": "import", "import_method": "csv"},
    )
    _create_pushable_account(
        email="push-filter-ignore@example.com",
        extra_updates={"region": "JP", "account_source": "import", "import_method": "text"},
    )
    pushed_emails = []

    class FakeResponse:
        status_code = 200

    def fake_post(_url, *, json, **_kwargs):
        pushed_emails.append(json["data"]["email"])
        return FakeResponse()

    monkeypatch.setattr("providers.push.nvtokens.httpx.post", fake_post)

    response = client.post(
        "/api/accounts/push",
        json={
            "platform": "chatgpt",
            "select_all": True,
            "target_key": "nvtokens",
            "filters": {"source": "import", "import_method": "csv", "region": "US"},
        },
    )

    assert response.status_code == 200
    assert response.json()["succeeded"] == 1
    assert pushed_emails == [matched.email]
