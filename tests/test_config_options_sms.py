from __future__ import annotations


def test_config_options_exposes_smsbower_catalog(client):
    resp = client.get("/api/config/options")
    assert resp.status_code == 200
    data = resp.json()

    assert "sms_providers" in data
    assert "sms_settings" in data
    assert "sms_drivers" in data
    assert {"smsbower", "herosms", "smspool", "fivesim", "smstome"}.issubset(
        {item["value"] for item in data["sms_providers"]}
    )
    smsbower = next(item for item in data["sms_providers"] if item["value"] == "smsbower")
    field_keys = {item["key"] for item in smsbower["fields"]}
    fields = {item["key"]: item for item in smsbower["fields"]}

    assert "smsbower_max_price" in field_keys
    assert "smsbower_min_price" in field_keys
    assert "smsbower_buy_max_attempts" in field_keys
    assert "smsbower_buy_retry_interval" in field_keys
    assert "smsbower_request_max_attempts" in field_keys
    assert "smsbower_request_retry_delay" in field_keys
    assert "smsbower_request_retry_max_delay" in field_keys
    assert "smsbower_number_api" in field_keys
    assert "smsbower_otp_timeout_seconds" in field_keys
    assert "smsbower_phone_max_attempts" in field_keys
    assert {
        "smsbower_provider_ids",
        "smsbower_except_provider_ids",
        "smsbower_phone_exception",
        "smsbower_user_id",
    }.issubset(field_keys)
    assert "smsbower_ref" not in field_keys
    assert fields["smsbower_default_service"]["type"] == "async-select"
    assert fields["smsbower_default_service"]["default_value"] == "dr"
    assert fields["smsbower_default_country"]["label"] == "默认国家/地区"
    assert fields["smsbower_default_country"]["type"] == "async-select"
    assert fields["smsbower_number_api"]["default_value"] == "getNumber"
    assert len(fields["smsbower_number_api"]["options"]) == 2
    assert fields["smsbower_otp_timeout_seconds"]["default_value"] == "120"
    assert fields["smsbower_phone_max_attempts"]["default_value"] == "3"
    assert fields["smsbower_buy_max_attempts"]["default_value"] == "20"
    assert fields["smsbower_buy_retry_interval"]["default_value"] == "3"
    for provider in data["sms_providers"]:
        provider_fields = {item["key"]: item for item in provider["fields"]}
        field_key = f"{provider['value']}_phone_max_attempts"
        assert provider_fields[field_key]["default_value"] == "3"


def test_config_options_exposes_proxy_catalog(client):
    resp = client.get("/api/config/options")
    assert resp.status_code == 200
    data = resp.json()

    assert "proxy_providers" in data
    assert "proxy_settings" in data
    assert "proxy_drivers" in data
    provider_keys = {item["value"] for item in data["proxy_providers"]}
    assert {"api_extract", "rotating_gateway"}.issubset(provider_keys)
    for provider in data["proxy_providers"]:
        fields = {item["key"]: item for item in provider["fields"]}
        assert fields["proxy_acquire_max_attempts"]["default_value"] == "8"
        assert fields["proxy_acquire_retry_delay"]["default_value"] == "1"
        assert fields["proxy_replace_max_attempts"]["default_value"] == "3"
        assert fields["proxy_preflight_enabled"]["default_value"] == "true"
        assert fields["proxy_preflight_timeout"]["default_value"] == "8"
        assert fields["proxy_preflight_max_latency_ms"]["default_value"] == "5000"


def test_sms_provider_test_fetches_balance_without_purchase(client, monkeypatch):
    from core.smsbower_sms import SMSBowerClient

    calls = []

    def fake_get_balance(self):
        calls.append(self.api_key)
        return 12.34

    monkeypatch.setattr(SMSBowerClient, "get_balance", fake_get_balance)

    resp = client.post(
        "/api/provider-settings/test",
        json={
            "provider_type": "sms",
            "provider_key": "smsbower",
            "config": {},
            "auth": {"smsbower_api_key": "secret-key"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["balance"] == 12.34
    assert "账户余额：12.34" in data["message"]
    assert calls == ["secret-key"]


def test_sms_provider_options_load_services_and_countries(client, monkeypatch):
    from core.smsbower_sms import SMSBowerClient

    monkeypatch.setattr(
        SMSBowerClient,
        "list_service_options",
        lambda self: [{"value": "dr", "label": "OpenAI (ChatGPT) (dr)"}],
    )
    monkeypatch.setattr(
        SMSBowerClient,
        "list_country_options",
        lambda self: [{"value": "0", "label": "Russia (RU · +7)"}],
    )

    base = {
        "provider_type": "sms",
        "provider_key": "smsbower",
        "config": {},
        "auth": {"smsbower_api_key": "secret-key"},
    }
    services = client.post(
        "/api/provider-settings/options",
        json={**base, "field_key": "smsbower_default_service"},
    )
    countries = client.post(
        "/api/provider-settings/options",
        json={**base, "field_key": "smsbower_default_country"},
    )

    assert services.status_code == 200
    assert services.json()["options"] == [{"value": "dr", "label": "OpenAI (ChatGPT) (dr)"}]
    assert countries.status_code == 200
    assert countries.json()["options"] == [{"value": "0", "label": "Russia (RU · +7)"}]


def test_sms_provider_options_require_api_key(client):
    response = client.post(
        "/api/provider-settings/options",
        json={
            "provider_type": "sms",
            "provider_key": "smsbower",
            "field_key": "smsbower_default_country",
            "config": {},
            "auth": {},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "请先填写 SMSBower API Key", "options": []}


def test_smspool_options_dispatch_without_api_key(client, monkeypatch):
    from core.smspool_sms import SMSPoolClient

    monkeypatch.setattr(
        SMSPoolClient,
        "list_service_options",
        lambda self: [{"value": "671", "label": "OpenAI / ChatGPT (671)"}],
    )
    response = client.post(
        "/api/provider-settings/options",
        json={
            "provider_type": "sms",
            "provider_key": "smspool",
            "field_key": "smspool_default_service",
            "config": {},
            "auth": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["options"] == [{"value": "671", "label": "OpenAI / ChatGPT (671)"}]


def test_fivesim_provider_test_uses_registry_balance(client, monkeypatch):
    from core.fivesim_sms import FiveSimClient

    monkeypatch.setattr(FiveSimClient, "get_balance", lambda self: 8.75)
    response = client.post(
        "/api/provider-settings/test",
        json={
            "provider_type": "sms",
            "provider_key": "fivesim",
            "config": {},
            "auth": {"fivesim_api_key": "secret-token"},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["balance"] == 8.75
    assert "5sim 连接成功" in response.json()["message"]


def test_proxy_provider_test_masks_credentials_and_checks_origin(client, monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "fl=1\nip=203.0.113.10\nloc=JP\n"

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError

    calls = []

    def fake_get(url, *, proxies, timeout):
        calls.append({"url": url, "proxies": proxies, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    resp = client.post(
        "/api/provider-settings/test",
        json={
            "provider_type": "proxy",
            "provider_key": "rotating_gateway",
            "config": {
                "proxy_gateway_url": "socks5://user:pass@gate.example.com:1080",
            },
            "auth": {},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["origin"] == "203.0.113.10"
    assert isinstance(data["latency_ms"], int)
    assert data["check_endpoint"] == "https://www.cloudflare.com/cdn-cgi/trace"
    assert data["proxy"] == "socks5://***:***@gate.example.com:1080"
    assert "user:pass" not in data["message"]
    assert calls == [
        {
            "url": "https://www.cloudflare.com/cdn-cgi/trace",
            "proxies": {
                "http": "socks5://user:pass@gate.example.com:1080",
                "https": "socks5://user:pass@gate.example.com:1080",
            },
            "timeout": 8.0,
        }
    ]
