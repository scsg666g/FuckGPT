"""Account CRUD endpoint tests."""
from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect
from sqlmodel import Session, select

from application.account_exports import AccountExportsService
from core.account_graph import patch_account_graph
from core.base_platform import Account
from core.db import (
    AccountAuthCredentialModel,
    AccountCodexAuthModel,
    AccountModel,
    AccountPushDeliveryModel,
    AccountStatusModel,
    TaskModel,
    engine,
    save_account,
)
from domain.accounts import AccountExportSelection, AccountFilters, AccountQuery, AccountRecord
from infrastructure.accounts_repository import AccountsRepository


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _create_account(**overrides):
    payload = {
        "platform": "chatgpt",
        "email": "test@example.com",
        "password": "TestPass123!",
        **overrides,
    }
    save_account(Account(**payload))
    _, records = AccountsRepository().list(
        AccountQuery(platform=payload["platform"], email=payload["email"])
    )
    return records[0].id


_DEBUG_SECRET_VALUES = {
    "credential-value-secret",
    "credential-preview-secret",
    "metadata-refresh-secret",
    "metadata-codex-access-secret",
    "wrapped-client-secret",
    "provider-api-secret",
    "provider-api-preview",
    "provider-access-secret",
    "provider-wrapped-secret",
    "resource-client-secret",
    "resource-service-api-secret",
    "resource-authorization-secret",
    "overview-access-secret",
    "overview-password-secret",
    "overview-cookie-secret",
    "overview-wrapped-secret",
    "display-session-cookie-secret",
}


def _unsafe_debug_record() -> AccountRecord:
    account_view = {
        "identity": {"id": 4242, "email": "debug-redaction@test.com"},
        "status": {"display": "registered"},
        "custom_safe_marker": {"value": "account-view-stays-unchanged"},
    }
    return AccountRecord(
        id=4242,
        platform="chatgpt",
        email="debug-redaction@test.com",
        password="TopLevelPass!Keep",
        account_view=account_view,
        credentials=[
            {
                "id": 1,
                "scope": "platform",
                "credential_type": "token",
                "key": "access_token",
                "value": "credential-value-secret",
                "preview": "credential-preview-secret",
                "metadata": {
                    "refreshToken": "metadata-refresh-secret",
                    "codexAccessToken": "metadata-codex-access-secret",
                    "contactPhone": "+56996830313",
                    "usage": {"input_token_count": 123},
                    "wrapped": {
                        "key": "client_secret",
                        "value": "wrapped-client-secret",
                    },
                },
            }
        ],
        provider_accounts=[
            {
                "id": 2,
                "provider_name": "mail-provider",
                "credentials": {"api_key": "provider-api-secret"},
                "credential_previews": {"api_key": "provider-api-preview"},
                "metadata": {
                    "oauth": {"accessToken": "provider-access-secret"},
                    "phone_numbers": ["+56996830313"],
                    "wrapped": {
                        "name": "api_key",
                        "value": "provider-wrapped-secret",
                    },
                },
            }
        ],
        provider_resources=[
            {
                "id": 3,
                "provider_name": "mail-provider",
                "metadata": {
                    "clientSecret": "resource-client-secret",
                    "serviceApiKey": "resource-service-api-secret",
                    "headers": {"Authorization": "resource-authorization-secret"},
                    "mobile": "+14155552671",
                },
            }
        ],
        overview={
            "auth": {
                "access_token": "overview-access-secret",
                "password": "overview-password-secret",
                "cookieJar": "overview-cookie-secret",
            },
            "profile": {"phoneNumber": "+56996830313"},
            "usage": {"output_token_count": 456},
            "wrapped": {
                "credential_key": "session_token",
                "value": "overview-wrapped-secret",
            },
        },
        display_summary={
            "warnings": [
                {
                    "context": {
                        "sessionCookie": "display-session-cookie-secret",
                        "telephone": "+56996830313",
                    }
                }
            ]
        },
    )


class _StaticUnsafeDebugRepository:
    def __init__(self, record: AccountRecord):
        self.record = record

    def list(self, _query):
        return 1, [self.record]

    def get(self, account_id: int):
        return self.record if account_id == self.record.id else None


def _assert_debug_payload_is_redacted(payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False)
    for secret in _DEBUG_SECRET_VALUES:
        assert secret not in encoded

    assert payload["password"] == "TopLevelPass!Keep"
    assert payload["account_view"] == _unsafe_debug_record().account_view

    credential = payload["credentials"][0]
    assert credential["key"] == "access_token"
    assert "value" not in credential
    assert "preview" not in credential
    assert credential["metadata"]["contactPhone"] == "+569****0313"
    assert credential["metadata"]["usage"]["input_token_count"] == 123
    assert "refreshToken" not in credential["metadata"]
    assert "codexAccessToken" not in credential["metadata"]
    assert "value" not in credential["metadata"]["wrapped"]

    provider = payload["provider_accounts"][0]
    assert "credentials" not in provider
    assert "credential_previews" not in provider
    assert provider["metadata"]["phone_numbers"] == ["+569****0313"]
    assert "accessToken" not in provider["metadata"]["oauth"]
    assert "value" not in provider["metadata"]["wrapped"]

    resource_metadata = payload["provider_resources"][0]["metadata"]
    assert "clientSecret" not in resource_metadata
    assert "serviceApiKey" not in resource_metadata
    assert "Authorization" not in resource_metadata["headers"]
    assert resource_metadata["mobile"] == "+141****2671"

    assert payload["overview"]["profile"]["phoneNumber"] == "+569****0313"
    assert payload["overview"]["usage"]["output_token_count"] == 456
    assert payload["overview"]["auth"] == {}
    assert "value" not in payload["overview"]["wrapped"]
    display_context = payload["display_summary"]["warnings"][0]["context"]
    assert "sessionCookie" not in display_context
    assert display_context["telephone"] == "+569****0313"


def test_account_schema_uses_structured_tables():
    tables = set(inspect(engine).get_table_names())
    assert {
        "accounts",
        "account_auth_credentials",
        "account_status",
        "account_subscription",
        "account_security_profile",
        "account_usage_snapshot",
        "account_codex_auth",
    }.issubset(tables)
    assert "account_overviews" not in tables
    assert "account_credentials" not in tables
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_save_account_returns_model_with_loaded_attributes_after_session_close():
    created = save_account(
        Account(
            platform="chatgpt",
            email="detached-model@test.com",
            password="FirstPass123!",
            user_id="acct-detached",
            extra={"access_token": "access-token"},
        )
    )

    created_id = int(created.id)
    assert created_id > 0
    assert created.email == "detached-model@test.com"
    with Session(engine) as session:
        status = session.get(AccountStatusModel, created_id)
        credentials = session.exec(
            select(AccountAuthCredentialModel)
            .where(AccountAuthCredentialModel.account_id == created_id)
        ).all()
    assert status.lifecycle_status == "registered"
    assert status.validity_status == "unknown"
    assert {(item.scope, item.key) for item in credentials} == {
        ("platform", "access_token"),
    }

    updated = save_account(
        Account(
            platform="chatgpt",
            email="detached-model@test.com",
            password="SecondPass123!",
            user_id="acct-detached",
            extra={"access_token": "updated-access-token"},
        )
    )

    assert int(updated.id) == created_id
    assert updated.email == "detached-model@test.com"
    assert updated.password == "SecondPass123!"
    with Session(engine) as session:
        access_token = session.exec(
            select(AccountAuthCredentialModel)
            .where(AccountAuthCredentialModel.account_id == created_id)
            .where(AccountAuthCredentialModel.key == "access_token")
        ).one()
    assert access_token.value == "updated-access-token"


def test_incremental_non_access_token_patch_does_not_replace_primary_credential():
    account_id = _create_account(
        email="primary-token@test.com",
        extra={"access_token": "access-secret"},
    )
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            credential_updates={"id_token": "identity-secret", "refresh_token": "refresh-secret"},
        )
        session.commit()
        credentials = session.exec(
            select(AccountAuthCredentialModel)
            .where(AccountAuthCredentialModel.account_id == account_id)
        ).all()

    by_key = {item.key: item for item in credentials}
    assert by_key["access_token"].is_primary is True
    assert by_key["id_token"].is_primary is False
    assert by_key["refresh_token"].is_primary is False


def test_list_accounts_empty(client):
    resp = client.get("/api/accounts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


def test_list_accounts_after_create(client):
    _create_account()
    resp = client.get("/api/accounts")
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["email"] == "test@example.com"
    view = data["items"][0]["account_view"]
    assert view["identity"]["email"] == "test@example.com"
    assert view["status"] == {
        "lifecycle": "registered",
        "validity": "unknown",
        "display": "registered",
        "checked_at": None,
    }
    assert set(view) == {
        "identity",
        "status",
        "subscription",
        "security",
        "usage",
        "codex",
        "verification",
        "display",
    }
    assert view["display"]["metrics"] == {"primary": [], "secondary": []}
    assert view["display"]["sections"] == []


def test_list_accounts_recursively_redacts_debug_payloads(client, monkeypatch):
    from api import accounts as accounts_api

    record = _unsafe_debug_record()
    monkeypatch.setattr(
        accounts_api.service,
        "repository",
        _StaticUnsafeDebugRepository(record),
    )

    response = client.get("/api/accounts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    _assert_debug_payload_is_redacted(payload["items"][0])


def test_get_account_by_id(client):
    account_id = _create_account()
    resp = client.get(f"/api/accounts/{account_id}")
    assert resp.status_code == 200
    assert resp.json()["email"] == "test@example.com"
    assert resp.json()["account_view"]["identity"]["id"] == account_id


def test_get_account_recursively_redacts_debug_payloads(client, monkeypatch):
    from api import accounts as accounts_api

    record = _unsafe_debug_record()
    monkeypatch.setattr(
        accounts_api.service,
        "repository",
        _StaticUnsafeDebugRepository(record),
    )

    response = client.get(f"/api/accounts/{record.id}")

    assert response.status_code == 200
    _assert_debug_payload_is_redacted(response.json())


def test_account_view_never_exposes_auth_secrets_and_reports_codex_state(client):
    secrets = {
        "access_token": "platform-access-secret",
        "refresh_token": "platform-refresh-secret",
        "cookies": "session-cookie-secret",
        "codex_access_token": "codex-access-secret",
        "codex_refresh_token": "codex-refresh-secret",
        "codex_id_token": "codex-id-secret",
    }
    account_id = _create_account(
        email="secure-view@test.com",
        user_id="acct-platform",
        extra={
            **secrets,
            "account_id": "acct-platform",
            "codex_email": "codex-login@test.com",
            "codex_account_id": "acct-codex",
            "codex_plan_type": "plus",
            "codex_auth_path": "data/codex_auths/codex.json",
            "codex_expires_at": "2026-01-02T03:04:05Z",
            "codex_last_refresh": "2026-01-01T00:00:00Z",
        },
    )

    response = client.get(f"/api/accounts/{account_id}")

    assert response.status_code == 200
    payload = response.json()
    view = payload["account_view"]
    encoded_view = json.dumps(view, ensure_ascii=False)
    for secret in secrets.values():
        assert secret not in encoded_view
        assert secret not in response.text
    assert view["identity"]["account_id"] == "acct-platform"
    assert view["security"]["platform_auth"] == {
        "has_primary_credential": True,
        "has_access_token": True,
        "has_refresh_token": True,
        "has_session_token": False,
        "has_cookie": True,
    }
    assert view["codex"] == {
        "authorized": True,
        "email": "codex-login@test.com",
        "account_id": "acct-codex",
        "plan_type": "plus",
        "expires_at": "2026-01-02T03:04:05Z",
        "last_refresh": "2026-01-01T00:00:00Z",
        "auth_path": "data/codex_auths/codex.json",
        "has_access_token": True,
        "has_refresh_token": True,
    }
    assert all("value" not in item and "preview" not in item for item in payload["credentials"])

    with Session(engine) as session:
        codex = session.get(AccountCodexAuthModel, account_id)
        credentials = session.exec(
            select(AccountAuthCredentialModel)
            .where(AccountAuthCredentialModel.account_id == account_id)
        ).all()
    assert codex.codex_account_id == "acct-codex"
    assert codex.has_access_token is True
    assert codex.has_refresh_token is True
    assert {(item.scope, item.key) for item in credentials if item.scope == "codex"} == {
        ("codex", "codex_access_token"),
        ("codex", "codex_refresh_token"),
        ("codex", "codex_id_token"),
    }


def test_credentials_are_revealed_only_by_explicit_endpoint_and_support_scope_filter(client):
    secrets = {
        "access_token": "platform-access-visible-on-demand",
        "refresh_token": "platform-refresh-visible-on-demand",
        "cookies": "platform-cookie-visible-on-demand",
        "codex_access_token": "codex-access-visible-on-demand",
        "codex_refresh_token": "codex-refresh-visible-on-demand",
    }
    account_id = _create_account(
        email="credential-reveal@test.com",
        extra=secrets,
    )

    list_response = client.get("/api/accounts")
    detail_response = client.get(f"/api/accounts/{account_id}")
    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    for secret in secrets.values():
        assert secret not in list_response.text
        assert secret not in detail_response.text

    response = client.get(f"/api/accounts/{account_id}/credentials")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    items = response.json()["items"]
    assert {item["key"]: item["value"] for item in items} == secrets
    assert all(set(item) == {
        "scope",
        "provider_name",
        "credential_type",
        "key",
        "value",
        "is_primary",
        "source",
    } for item in items)

    platform_response = client.get(
        f"/api/accounts/{account_id}/credentials",
        params={"scope": "platform"},
    )
    assert platform_response.status_code == 200
    platform_items = platform_response.json()["items"]
    assert {item["key"] for item in platform_items} == {
        "access_token",
        "refresh_token",
        "cookies",
    }
    assert {item["scope"] for item in platform_items} == {"platform"}

    codex_response = client.get(
        f"/api/accounts/{account_id}/credentials",
        params={"scope": "codex"},
    )
    assert codex_response.status_code == 200
    codex_items = codex_response.json()["items"]
    assert {item["key"] for item in codex_items} == {
        "codex_access_token",
        "codex_refresh_token",
    }
    assert {item["scope"] for item in codex_items} == {"codex"}


def test_get_account_credentials_not_found(client):
    response = client.get("/api/accounts/99999/credentials")
    assert response.status_code == 404


def test_get_account_not_found(client):
    resp = client.get("/api/accounts/99999")
    assert resp.status_code == 404


def test_delete_account(client):
    account_id = _create_account()
    del_resp = client.delete(f"/api/accounts/{account_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True
    # Verify it's gone
    get_resp = client.get(f"/api/accounts/{account_id}")
    assert get_resp.status_code == 404


def test_update_account(client):
    account_id = _create_account()
    patch_resp = client.patch(
        f"/api/accounts/{account_id}",
        json={"password": "NewPass456!"},
    )
    assert patch_resp.status_code == 200


def test_filter_accounts_by_platform(client):
    _create_account(platform="chatgpt", email="a@test.com")
    _create_account(platform="cursor", email="b@test.com")
    resp = client.get("/api/accounts", params={"platform": "cursor"})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["platform"] == "cursor"


def test_v2_filters_search_all_identity_fields_beyond_first_page(client):
    for index in range(55):
        _create_account(email=f"ordinary-{index:02d}@test.com")
    target_id = _create_account(
        email="primary-address@test.com",
        user_id="user-deep-search",
        extra={
            "codex_email": "codex-deep-search@test.com",
            "codex_account_id": "codex-account-deep-search",
            "verification_mailbox": {
                "provider": "hotmail007",
                "email": "verify-deep-search@outlook.com",
                "account_id": "mailbox-deep-search",
            },
        },
    )

    for query in (
        "user-deep-search",
        "codex-deep-search",
        "codex-account-deep-search",
        "verify-deep-search",
        "mailbox-deep-search",
    ):
        response = client.get(
            "/api/accounts",
            params={"platform": "chatgpt", "search": query, "page_size": 50},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == target_id


def test_v2_filters_mailbox_security_source_region_and_sort(client):
    protocol_id = _create_account(
        email="protocol@test.com",
        extra={
            "checked_at": "2026-07-01T00:00:00Z",
            "phone_bound": True,
            "mfa_enabled": True,
            "region": "US",
            "account_source": "registration",
            "registration_executor": "protocol",
            "verification_mailbox": {
                "provider": "hotmail007",
                "email": "protocol@test.com",
                "account_id": "mb-protocol",
            },
            "codex_expires_at": "2026-09-01T00:00:00Z",
        },
    )
    browser_id = _create_account(
        email="browser@test.com",
        extra={
            "checked_at": "2026-07-02T00:00:00Z",
            "phone_bound": False,
            "mfa_enabled": False,
            "region": "JP",
            "account_source": "registration",
            "registration_executor": "headless",
            "verification_mailbox": {
                "provider": "api_mailbox",
                "email": "verify-browser@test.com",
                "account_id": "mb-browser",
            },
            "codex_expires_at": "2026-08-01T00:00:00Z",
        },
    )

    response = client.get(
        "/api/accounts",
        params={
            "platform": "chatgpt",
            "mailbox_bound": "bound",
            "mailbox_provider": "hotmail007",
            "mailbox_email_match": "same",
            "phone_state": "bound",
            "checked_state": "checked",
            "mfa_state": "enabled",
            "source": "protocol",
            "region": "US",
        },
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [protocol_id]

    sorted_response = client.get(
        "/api/accounts",
        params={"platform": "chatgpt", "sort_by": "expires_at", "sort_order": "asc"},
    )
    assert sorted_response.status_code == 200
    assert [item["id"] for item in sorted_response.json()["items"][:2]] == [browser_id, protocol_id]


def test_v2_filters_codex_authorization_push_target_status_and_recent_times(client):
    authorized_id = _create_account(
        email="authorized-filter@test.com",
        extra={
            "codex_access_token": "authorized-secret",
            "codex_last_refresh": "2026-07-10T12:00:00Z",
        },
    )
    failed_id = _create_account(
        email="failed-filter@test.com",
        extra={"codex_last_refresh": "2026-07-01T12:00:00Z"},
    )
    untouched_id = _create_account(email="untouched-filter@test.com")

    with Session(engine) as session:
        session.add(AccountPushDeliveryModel(
            account_id=authorized_id,
            target_key="alpha",
            target_label="Alpha",
            status="success",
            last_attempt_at=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
            pushed_at=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
            updated_at=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        ))
        session.add(AccountPushDeliveryModel(
            account_id=authorized_id,
            target_key="beta",
            target_label="Beta",
            status="failed",
            last_attempt_at=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
            updated_at=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
        ))
        session.add(AccountPushDeliveryModel(
            account_id=failed_id,
            target_key="alpha",
            target_label="Alpha",
            status="failed",
            last_attempt_at=datetime(2026, 7, 22, 12, tzinfo=timezone.utc),
            updated_at=datetime(2026, 7, 22, 12, tzinfo=timezone.utc),
        ))
        session.commit()

    def filtered_ids(**params):
        response = client.get("/api/accounts", params={"platform": "chatgpt", **params})
        assert response.status_code == 200
        return {item["id"] for item in response.json()["items"]}

    assert filtered_ids(codex_auth_state="authorized") == {authorized_id}
    assert filtered_ids(codex_auth_state="unauthorized") == {failed_id, untouched_id}
    assert filtered_ids(push_target="alpha", push_status="success") == {authorized_id}
    assert filtered_ids(push_target="beta", push_status="failed") == {authorized_id}
    assert filtered_ids(push_target="beta", push_status="not_pushed") == {failed_id, untouched_id}
    assert filtered_ids(push_status="failed") == {authorized_id, failed_id}
    assert filtered_ids(
        pushed_from="2026-07-22T00:00:00Z",
        pushed_to="2026-07-23T00:00:00Z",
    ) == {failed_id}
    assert filtered_ids(
        codex_refreshed_from="2026-07-10T00:00:00Z",
        codex_refreshed_to="2026-07-11T00:00:00Z",
    ) == {authorized_id}

    selected = AccountsRepository().select_for_export(AccountExportSelection(
        platform="chatgpt",
        select_all=True,
        filters=AccountFilters(push_target="alpha", push_status="success"),
    ))
    assert [record.id for record in selected] == [authorized_id]

    stats = client.get("/api/accounts/stats", params={"platform": "chatgpt"}).json()
    assert stats["codex_authorized"] == 1
    assert stats["codex_unauthorized"] == 2
    assert stats["push_failed"] == 2
    assert stats["push_not_pushed"] == 1
    assert stats["push_targets"] == [
        {"key": "alpha", "label": "Alpha"},
        {"key": "beta", "label": "Beta"},
    ]


def test_accounts_filter_and_stats_include_deactivated_status(client):
    deactivated_id = _create_account(email="deactivated-filter@test.com")
    invalid_id = _create_account(email="invalid-filter@test.com")
    _create_account(email="active-filter@test.com")

    with Session(engine) as session:
        deactivated = session.get(AccountModel, deactivated_id)
        invalid = session.get(AccountModel, invalid_id)
        patch_account_graph(
            session,
            deactivated,
            summary_updates={
                "validity_status": "deactivated",
                "last_error": "error_code: account_deactivated",
                "security_raw": {
                    "deactivation_reason": "OpenAI 返回 account_deactivated，账号已被停用/封号",
                    "deactivation_error": "error_code: account_deactivated",
                    "deactivation_detected_at": "2026-08-02T02:03:04Z",
                },
            },
        )
        patch_account_graph(session, invalid, summary_updates={"valid": False})
        session.commit()

    filtered = client.get("/api/accounts", params={"platform": "chatgpt", "status": "deactivated"})
    assert filtered.status_code == 200
    payload = filtered.json()
    assert {item["id"] for item in payload["items"]} == {deactivated_id}
    security = payload["items"][0]["account_view"]["security"]
    assert security["deactivation_reason"] == "OpenAI 返回 account_deactivated，账号已被停用/封号"
    assert security["deactivation_error"] == "error_code: account_deactivated"
    assert security["deactivation_detected_at"] == "2026-08-02T02:03:04Z"

    stats = client.get("/api/accounts/stats", params={"platform": "chatgpt"}).json()
    assert stats["deactivated"] == 1
    assert stats["invalid"] == 1


def test_v2_full_filter_selection_is_shared_by_export_and_batch_check(client, monkeypatch):
    matched_id = _create_account(
        email="matched@test.com",
        extra={"region": "US", "account_source": "import", "import_method": "csv"},
    )
    _create_account(
        email="ignored@test.com",
        extra={"region": "JP", "account_source": "import", "import_method": "text"},
    )

    export_response = client.post(
        "/api/accounts/export/csv",
        json={
            "platform": "chatgpt",
            "select_all": True,
            "filters": {"source": "import", "import_method": "csv", "region": "US"},
        },
    )
    assert export_response.status_code == 200
    assert "matched@test.com" in export_response.text
    assert "ignored@test.com" not in export_response.text

    check_response = client.post(
        "/api/accounts/check-all",
        json={
            "platform": "chatgpt",
            "select_all": True,
            "filters": {"source": "import", "import_method": "csv", "region": "US"},
        },
    )
    assert check_response.status_code == 200
    with Session(engine) as session:
        task = session.get(TaskModel, check_response.json()["task_id"])
        assert task.get_payload()["account_ids"] == [matched_id]


def test_account_stats(client):
    _create_account()
    resp = client.get("/api/accounts/stats")
    assert resp.status_code == 200


def test_check_all_uses_selected_account_ids(client):
    first_id = _create_account(email="first-check@test.com")
    _create_account(email="second-check@test.com")
    third_id = _create_account(email="third-check@test.com")

    resp = client.post(
        "/api/accounts/check-all",
        json={
            "platform": "chatgpt",
            "ids": [first_id, third_id],
            "select_all": False,
        },
    )

    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        payload = task.get_payload()
    assert set(payload["account_ids"]) == {first_id, third_id}
    assert payload["platform"] == "chatgpt"
    assert payload.get("relogin_invalid") is not True


def test_check_all_persists_platform_proxy_strategy(client):
    account_id = _create_account(email="proxy-check@test.com")

    resp = client.post(
        "/api/accounts/check-all",
        json={
            "platform": "chatgpt",
            "ids": [account_id],
            "select_all": False,
            "platform_proxy_mode": "manual",
            "platform_proxy_value": "socks5://user:pass@proxy.example:1080",
        },
    )

    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        payload = task.get_payload()
    assert payload["platform_proxy_mode"] == "manual"
    assert payload["platform_proxy_value"] == "socks5://user:pass@proxy.example:1080"


def test_check_all_persists_optional_invalid_relogin_settings(client):
    account_id = _create_account(email="invalid-relogin-check@test.com")

    resp = client.post(
        "/api/accounts/check-all",
        json={
            "platform": "chatgpt",
            "ids": [account_id],
            "select_all": False,
            "concurrency": 3,
            "request_timeout_seconds": 17,
            "platform_proxy_mode": "manual",
            "platform_proxy_value": "http://127.0.0.1:7897",
            "relogin_invalid": True,
            "relogin_params": {
                "browser_mode": "headless",
                "keep_browser_open": "false",
                "platform_proxy_mode": "manual",
                "platform_proxy_value": "http://127.0.0.1:7897",
            },
        },
    )

    assert resp.status_code == 200
    with Session(engine) as session:
        task = session.get(TaskModel, resp.json()["task_id"])
        payload = task.get_payload()
    assert payload["account_ids"] == [account_id]
    assert payload["concurrency"] == 3
    assert payload["request_timeout_seconds"] == 17
    assert payload["relogin_invalid"] is True
    assert payload["relogin_params"]["browser_mode"] == "headless"
    assert payload["relogin_params"]["platform_proxy_value"] == "http://127.0.0.1:7897"


def test_check_all_freezes_filtered_account_ids(client):
    _create_account(email="target-free@test.com")
    subscribed_id = _create_account(email="target-plus@test.com")
    _create_account(email="other-plus@test.com")

    with Session(engine) as session:
        model = session.get(AccountModel, subscribed_id)
        patch_account_graph(
            session,
            model,
            summary_updates={"plan": "plus", "plan_state": "subscribed"},
        )
        session.commit()

    resp = client.post(
        "/api/accounts/check-all",
        json={
            "platform": "chatgpt",
            "select_all": True,
            "search_filter": "target",
            "status_filter": "subscribed",
        },
    )

    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        payload = task.get_payload()
    assert payload["account_ids"] == [subscribed_id]


def test_codex_oauth_batch_freezes_selected_account_ids_and_params(client):
    first_id = _create_account(email="first-codex@test.com")
    _create_account(email="second-codex@test.com")
    third_id = _create_account(email="third-codex@test.com")

    resp = client.post(
        "/api/accounts/codex-oauth/authorize",
        json={
            "platform": "chatgpt",
            "ids": [first_id, third_id],
            "select_all": False,
            "concurrency": 3,
            "params": {
                "browser_mode": "headless",
                "keep_browser_open": "false",
                "platform_proxy_mode": "manual",
                "platform_proxy_value": "socks5://user:pass@proxy.example:1080",
            },
        },
    )

    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        payload = task.get_payload()
    assert task.type == "codex_oauth_batch"
    assert set(payload["account_ids"]) == {first_id, third_id}
    assert payload["action_id"] == "codex_oauth_authorize"
    assert payload["concurrency"] == 3
    assert payload["params"]["oauth_mode"] == "browser"
    assert payload["params"]["platform_proxy_mode"] == "manual"


def test_codex_oauth_batch_freezes_complete_v2_filter_result(client):
    matched_id = _create_account(
        email="codex-filter-match@test.com",
        extra={"region": "US", "account_source": "import", "import_method": "csv"},
    )
    _create_account(
        email="codex-filter-ignore@test.com",
        extra={"region": "JP", "account_source": "import", "import_method": "text"},
    )

    response = client.post(
        "/api/accounts/codex-oauth/authorize",
        json={
            "platform": "chatgpt",
            "select_all": True,
            "filters": {"source": "import", "import_method": "csv", "region": "US"},
            "concurrency": 2,
            "params": {"browser_mode": "headless"},
        },
    )

    assert response.status_code == 200
    with Session(engine) as session:
        task = session.get(TaskModel, response.json()["task_id"])
        assert task.get_payload()["account_ids"] == [matched_id]


def test_relogin_batch_freezes_selected_account_ids_and_params(client):
    first_id = _create_account(email="first-relogin-batch@test.com")
    _create_account(email="second-relogin-batch@test.com")
    third_id = _create_account(email="third-relogin-batch@test.com")

    resp = client.post(
        "/api/accounts/relogin/batch",
        json={
            "platform": "chatgpt",
            "ids": [first_id, third_id],
            "select_all": False,
            "concurrency": 4,
            "params": {
                "browser_mode": "headless",
                "keep_browser_open": "false",
                "platform_proxy_mode": "manual",
                "platform_proxy_value": "http://127.0.0.1:7897",
            },
        },
    )

    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        payload = task.get_payload()
    assert task.type == "relogin_batch"
    assert set(payload["account_ids"]) == {first_id, third_id}
    assert payload["action_id"] == "relogin"
    assert payload["concurrency"] == 4
    assert payload["params"]["platform_proxy_mode"] == "manual"


def test_relogin_batch_freezes_complete_v2_filter_result(client):
    matched_id = _create_account(
        email="relogin-filter-match@test.com",
        extra={"region": "US", "account_source": "import", "import_method": "csv"},
    )
    _create_account(
        email="relogin-filter-ignore@test.com",
        extra={"region": "JP", "account_source": "import", "import_method": "text"},
    )

    response = client.post(
        "/api/accounts/relogin/batch",
        json={
            "platform": "chatgpt",
            "select_all": True,
            "filters": {"source": "import", "import_method": "csv", "region": "US"},
            "concurrency": 2,
            "params": {"browser_mode": "headless"},
        },
    )

    assert response.status_code == 200
    with Session(engine) as session:
        task = session.get(TaskModel, response.json()["task_id"])
        assert task.get_payload()["account_ids"] == [matched_id]


def test_empty_export_selection_never_falls_back_to_all_accounts():
    _create_account(email="must-not-export@test.com")

    records = AccountsRepository().select_for_export(
        AccountExportSelection(platform="chatgpt", ids=[], select_all=False)
    )

    assert records == []


def test_export_any2api_multi_platform(client):
    _create_account(platform="kiro", email="k@test.com", password="")
    _create_account(platform="grok", email="g@test.com", password="")
    _create_account(platform="cursor", email="c@test.com", password="")
    resp = client.post("/api/accounts/export/any2api", json={"select_all": True})
    assert resp.status_code == 200
    assert "any2api_admin" in resp.headers.get("content-disposition", "")


def test_export_cpa_uses_standard_payload_schema():
    exp_timestamp = 1777166030
    expected_expired = datetime.fromtimestamp(
        exp_timestamp, tz=timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    access_token = _make_jwt({
        "exp": exp_timestamp,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-standard",
        },
    })
    id_token = _make_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-standard",
        },
    })
    repository = AccountsRepository()
    save_account(
        Account(
            platform="chatgpt",
            email="cpa@test.com",
            password="TestPass123!",
            user_id="acct-standard",
            extra={
                "access_token": access_token,
                "refresh_token": "rt_standard",
                "id_token": id_token,
            },
        )
    )
    service = AccountExportsService(repository)

    artifact = service.export_chatgpt_cpa(AccountExportSelection(platform="chatgpt", select_all=True))
    payload = json.loads(artifact.content)
    assert list(payload.keys()) == [
        "access_token",
        "account_id",
        "email",
        "expired",
        "id_token",
        "last_refresh",
        "refresh_token",
        "type",
    ]
    assert payload["access_token"] == access_token
    assert payload["account_id"] == "acct-standard"
    assert payload["email"] == "cpa@test.com"
    assert payload["expired"] == expected_expired
    assert payload["id_token"] == id_token
    assert payload["last_refresh"].endswith("+08:00")
    assert payload["refresh_token"] == "rt_standard"
    assert payload["type"] == "codex"


def test_export_cpa_falls_back_to_stored_user_id_for_account_id():
    repository = AccountsRepository()
    save_account(
        Account(
            platform="chatgpt",
            email="fallback@test.com",
            password="TestPass123!",
            user_id="acct-from-user-id",
            extra={
                "access_token": _make_jwt({"exp": 1777166030}),
                "refresh_token": "rt_fallback",
            },
        )
    )
    service = AccountExportsService(repository)

    artifact = service.export_chatgpt_cpa(AccountExportSelection(platform="chatgpt", select_all=True))
    payload = json.loads(artifact.content)
    assert payload["account_id"] == "acct-from-user-id"
    assert payload["refresh_token"] == "rt_fallback"


def test_export_codex_prefers_codex_oauth_credentials():
    access_token = _make_jwt({
        "exp": 1777166030,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-chatgpt",
        },
    })
    codex_access_token = _make_jwt({
        "exp": 1777169999,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-codex",
        },
    })
    save_account(
        Account(
            platform="chatgpt",
            email="codex@test.com",
            password="TestPass123!",
            extra={
                "access_token": access_token,
                "refresh_token": "rt_chatgpt",
                "id_token": "id_chatgpt",
                "codex_access_token": codex_access_token,
                "codex_refresh_token": "rt_codex",
                "codex_id_token": "id_codex",
                "codex_account_id": "acct-codex-stored",
                "codex_email": "codex-login@test.com",
                "codex_expires_at": "2026-01-02T03:04:05Z",
                "codex_last_refresh": "2026-01-01T00:00:00Z",
            },
        )
    )

    artifact = AccountExportsService(AccountsRepository()).export_chatgpt_codex(
        AccountExportSelection(platform="chatgpt", select_all=True)
    )
    payload = json.loads(artifact.content)

    assert artifact.filename == "codex@test.com_codex.json"
    assert payload == {
        "type": "codex",
        "id_token": "id_codex",
        "access_token": codex_access_token,
        "refresh_token": "rt_codex",
        "account_id": "acct-codex-stored",
        "last_refresh": "2026-01-01T00:00:00Z",
        "email": "codex-login@test.com",
        "expired": "2026-01-02T03:04:05Z",
        "account_note": "",
    }


def test_export_codex_endpoint_batches_as_zip(client):
    first_id = _create_account(email="first-codex@test.com")
    second_id = _create_account(email="second-codex@test.com")

    resp = client.post(
        "/api/accounts/export/codex",
        json={"platform": "chatgpt", "ids": [first_id, second_id]},
    )

    assert resp.status_code == 200
    assert "codex_tokens" in resp.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert sorted(archive.namelist()) == [
            "first-codex@test.com_codex.json",
            "second-codex@test.com_codex.json",
        ]


