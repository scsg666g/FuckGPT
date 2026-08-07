from __future__ import annotations

import json

import pytest
import requests

from core.smsbower_sms import SMSBowerClient, SMSBowerError
from infrastructure.provider_definitions_repository import (
    ProviderDefinitionsRepository,
    SUPPORTED_SMS_PROVIDER_KEYS,
)
from providers.registry import create_provider, list_registered, load_all


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP failure url=https://smsbower.page/stubs/handler_api.php?api_key=secret-key")


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.payloads:
            raise RuntimeError("no fake payloads left")
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


def test_smsbower_get_number_parses_activation_and_sends_documented_params():
    logs = []
    session = FakeSession(["ACCESS_NUMBER:12345:+15551234567"])
    client = SMSBowerClient(api_key="secret-key", session=session, log_fn=logs.append)

    activation = client.get_number(
        "go",
        "0",
        max_price="0.5",
        min_price="0.1",
        provider_ids="1,2",
        except_provider_ids="3",
        phone_exception="+1555000",
        user_id="user-1",
    )

    assert activation.activation_id == "12345"
    assert activation.phone_number == "+15551234567"
    assert activation.provider == "smsbower"
    assert activation.service == "go"
    assert activation.country == "0"
    assert session.calls[0][0] == "https://smsbower.page/stubs/handler_api.php"
    assert session.calls[0][1]["params"] == {
        "api_key": "secret-key",
        "action": "getNumber",
        "service": "go",
        "country": "0",
        "maxPrice": "0.5",
        "minPrice": "0.1",
        "providerIds": "1,2",
        "exceptProviderIds": "3",
        "phoneException": "+1555000",
        "ref": "531498",
        "userID": "user-1",
    }
    assert any("买号成功" in item for item in logs)
    assert not any("secret-key" in item for item in logs)


def test_smsbower_get_number_v2_parses_json_activation():
    payload = json.dumps(
        {
            "activationId": "777",
            "phoneNumber": 15551234567,
            "activationCost": "0.34",
            "countryCode": "0",
            "activationOperator": "any",
        }
    )
    session = FakeSession([payload])
    client = SMSBowerClient(api_key="secret-key", session=session)

    activation = client.get_number_v2("go", "0")

    assert activation.activation_id == "777"
    assert activation.phone_number == "15551234567"
    assert activation.metadata["activationCost"] == "0.34"
    assert session.calls[0][1]["params"]["action"] == "getNumberV2"


def test_smsbower_number_api_config_can_route_regular_purchase_to_v2():
    payload = json.dumps(
        {
            "activationId": "778",
            "phoneNumber": 15551234568,
            "activationCost": "0.35",
            "activationOperator": "carrier",
        }
    )
    client = SMSBowerClient.from_config(
        {
            "smsbower_api_key": "secret-key",
            "smsbower_default_service": "DR",
            "smsbower_default_country": "0",
            "smsbower_number_api": "getNumberV2",
        }
    )
    client.session = FakeSession([payload])

    activation = client.get_number()

    assert activation.activation_id == "778"
    assert activation.service == "dr"
    assert client.session.calls[0][1]["params"]["action"] == "getNumberV2"


def test_smsbower_configured_price_bounds_are_used_and_can_be_overridden():
    session = FakeSession(["ACCESS_NUMBER:12345:+15551234567", "ACCESS_NUMBER:67890:+15557654321"])
    client = SMSBowerClient.from_config(
        {
            "smsbower_api_key": "secret-key",
            "smsbower_default_service": "go",
            "smsbower_default_country": "0",
            "smsbower_max_price": "0.50",
            "smsbower_min_price": "0.10",
        }
    )
    client.session = session

    client.get_number()
    client.get_number(max_price="0.40", min_price="0.20")

    assert session.calls[0][1]["params"]["maxPrice"] == "0.50"
    assert session.calls[0][1]["params"]["minPrice"] == "0.10"
    assert session.calls[0][1]["params"]["ref"] == "531498"
    assert session.calls[1][1]["params"]["maxPrice"] == "0.40"
    assert session.calls[1][1]["params"]["minPrice"] == "0.20"
    assert session.calls[1][1]["params"]["ref"] == "531498"


def test_smsbower_configured_purchase_filters_are_used_and_can_be_overridden():
    session = FakeSession(["ACCESS_NUMBER:12345:+15551234567", "ACCESS_NUMBER:67890:+15557654321"])
    client = SMSBowerClient.from_config(
        {
            "smsbower_api_key": "secret-key",
            "smsbower_default_service": "DR",
            "smsbower_default_country": "0",
            "smsbower_provider_ids": "1,2",
            "smsbower_except_provider_ids": "3",
            "smsbower_phone_exception": "7918,7900111",
            "smsbower_user_id": "reseller-1",
            "smsbower_ref": "custom-ref",
        }
    )
    client.session = session

    client.get_number()
    client.get_number(provider_ids="9", ref="override-ref")

    assert session.calls[0][1]["params"] == {
        "api_key": "secret-key",
        "action": "getNumber",
        "service": "dr",
        "country": "0",
        "providerIds": "1,2",
        "exceptProviderIds": "3",
        "phoneException": "7918,7900111",
        "userID": "reseller-1",
        "ref": "531498",
    }
    assert session.calls[1][1]["params"]["providerIds"] == "9"
    assert session.calls[1][1]["params"]["ref"] == "531498"


def test_smsbower_catalog_options_use_api_codes_and_readable_labels():
    countries = json.dumps(
        {
            "status": "success",
            "countries": [
                {"title": "Russia", "iso": "RU", "prefix": 7, "activate_org_code": 0},
                {"title": "United States", "iso": "US", "prefix": 1, "activate_org_code": 187},
            ],
        }
    )
    services = json.dumps(
        {
            "status": "success",
            "services": [
                {"title": "OpenAI (ChatGPT)", "activate_org_code": "dr", "is_active": 1},
                {"title": "Disabled", "activate_org_code": "xx", "is_active": 0},
            ],
        }
    )
    client = SMSBowerClient(api_key="secret-key", session=FakeSession([countries, services]))

    assert client.list_country_options() == [
        {
            "value": "0", "label": "Russia (RU · +7)", "english_name": "Russia",
            "localized_name": "", "region_code": "RU", "dial_code": "+7",
        },
        {
            "value": "187", "label": "United States (US · +1)", "english_name": "United States",
            "localized_name": "", "region_code": "US", "dial_code": "+1",
        },
    ]
    assert client.list_service_options() == [
        {"value": "dr", "label": "OpenAI (ChatGPT) (dr)"},
    ]


def test_smsbower_status_and_set_status_responses_are_normalized():
    session = FakeSession(
        [
            "STATUS_WAIT_CODE",
            "STATUS_WAIT_RETRY:111111",
            "STATUS_OK:654321",
            "ACCESS_CANCEL",
            "ACCESS_ACTIVATION",
        ]
    )
    client = SMSBowerClient(api_key="secret-key", session=session)

    assert client.get_status("123").status == "waiting"

    retry = client.get_status("123")
    assert retry.status == "waiting_retry"
    assert retry.last_code == "111111"

    ok = client.get_status("123")
    assert ok.status == "ok"
    assert ok.code == "654321"

    assert client.cancel("123") == "ACCESS_CANCEL"
    assert client.finish("123") == "ACCESS_ACTIVATION"
    assert session.calls[-2][1]["params"] == {
        "api_key": "secret-key",
        "action": "setStatus",
        "id": "123",
        "status": 8,
    }
    assert session.calls[-1][1]["params"]["status"] == 6


def test_smsbower_wait_for_code_polls_until_status_ok():
    session = FakeSession(["STATUS_WAIT_CODE", "STATUS_OK:222333"])
    client = SMSBowerClient(api_key="secret-key", session=session, poll_interval=0)

    assert client.wait_for_code("123", timeout=1) == "222333"
    assert len(session.calls) == 2


def test_smsbower_retries_transient_proxy_reset_and_then_succeeds(monkeypatch):
    sleeps = []
    logs = []
    proxy_error = requests.exceptions.ProxyError(
        "HTTPSConnectionPool: Max retries exceeded "
        "(Caused by ConnectionResetError(10054, 'remote reset')) api_key=secret-key"
    )
    session = FakeSession([proxy_error, proxy_error, "STATUS_OK:222333"])
    client = SMSBowerClient(
        api_key="secret-key",
        session=session,
        request_max_attempts=4,
        request_retry_delay=1,
        request_retry_max_delay=2,
        log_fn=logs.append,
    )
    monkeypatch.setattr("core.network_retry.time.sleep", lambda seconds: sleeps.append(seconds))

    status = client.get_status("123")

    assert status.code == "222333"
    assert len(session.calls) == 3
    assert sleeps == [1, 2]
    assert any("自动重试" in item for item in logs)
    assert all("secret-key" not in item for item in logs)


def test_smsbower_does_not_retry_provider_business_error(monkeypatch):
    sleeps = []
    session = FakeSession(["BAD_KEY", "STATUS_OK:should-not-be-used"])
    client = SMSBowerClient(api_key="secret-key", session=session, request_max_attempts=4)
    monkeypatch.setattr("core.network_retry.time.sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(SMSBowerError, match="BAD_KEY"):
        client.get_status("123")

    assert len(session.calls) == 1
    assert sleeps == []


def test_smsbower_balance_supports_plain_and_prefixed_response():
    plain = SMSBowerClient(api_key="secret-key", session=FakeSession(["12.34"]))
    prefixed = SMSBowerClient(api_key="secret-key", session=FakeSession(["ACCESS_BALANCE:56.78"]))

    assert plain.get_balance() == 12.34
    assert prefixed.get_balance() == 56.78


def test_smsbower_errors_redact_api_key():
    session = FakeSession(["BAD_KEY"])
    client = SMSBowerClient(api_key="secret-key", session=session)

    with pytest.raises(SMSBowerError) as excinfo:
        client.get_number("go", "0")

    assert "BAD_KEY" in str(excinfo.value)
    assert "secret-key" not in str(excinfo.value)

    http_session = FakeSession([])
    http_session.payloads = [FakeResponse("ignored", status_code=500)]
    client = SMSBowerClient(api_key="secret-key", session=http_session)

    with pytest.raises(SMSBowerError) as http_excinfo:
        client.get_status("123")

    assert "secret-key" not in str(http_excinfo.value)
    assert "api_key=***" in str(http_excinfo.value)


def test_smsbower_is_exposed_in_provider_catalog_and_registry():
    repository = ProviderDefinitionsRepository()
    repository.ensure_seeded()
    load_all()

    definitions = repository.list_by_type("sms", enabled_only=True)
    drivers = repository.list_driver_templates("sms")

    assert {item.provider_key for item in definitions} == set(SUPPORTED_SMS_PROVIDER_KEYS)
    assert {item["driver_type"] for item in drivers} == set(SUPPORTED_SMS_PROVIDER_KEYS)
    smsbower = next(item for item in definitions if item.provider_key == "smsbower")
    field_keys = {field["key"] for field in smsbower.get_fields()}
    assert {
        "smsbower_request_max_attempts",
        "smsbower_request_retry_delay",
        "smsbower_request_retry_max_delay",
        "smsbower_phone_max_attempts",
    }.issubset(field_keys)
    assert "smsbower" in list_registered("sms")
    provider = create_provider("sms", "smsbower", {"smsbower_api_key": "secret-key"})
    assert isinstance(provider, SMSBowerClient)
