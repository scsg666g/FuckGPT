from __future__ import annotations


def _save(client, *, provider_key: str, enabled: bool, is_default: bool = False, setting_id: int | None = None):
    payload = {
        "provider_type": "sms",
        "provider_key": provider_key,
        "display_name": provider_key,
        "auth_mode": "api_key",
        "enabled": enabled,
        "is_default": is_default,
        "config": {f"{provider_key}_default_service": "dr"},
        "auth": {f"{provider_key}_api_key": f"{provider_key}-secret"},
        "metadata": {"source": "test"},
    }
    if setting_id is not None:
        payload["id"] = setting_id
    response = client.request("PUT" if setting_id is not None else "POST", "/api/provider-settings", json=payload)
    assert response.status_code == 200
    return response.json()["item"]


def test_disabled_setting_is_saved_without_becoming_default(client):
    item = _save(client, provider_key="smsbower", enabled=False)

    assert item["enabled"] is False
    assert item["is_default"] is False
    assert item["auth"] == {"smsbower_api_key": "smsbower-secret"}


def test_disabling_last_provider_preserves_configuration_and_allows_zero_enabled(client):
    created = _save(client, provider_key="smsbower", enabled=True)
    assert created["is_default"] is True

    disabled = _save(
        client,
        provider_key="smsbower",
        enabled=False,
        is_default=True,
        setting_id=created["id"],
    )
    settings = client.get("/api/provider-settings", params={"provider_type": "sms"}).json()

    assert disabled["enabled"] is False
    assert disabled["is_default"] is False
    assert disabled["config"] == {"smsbower_default_service": "dr"}
    assert disabled["auth"] == {"smsbower_api_key": "smsbower-secret"}
    assert not any(item["enabled"] or item["is_default"] for item in settings)

    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    assert ProviderSettingsRepository().get_default_provider_key("sms") == ""

    reenabled = _save(
        client,
        provider_key="smsbower",
        enabled=True,
        setting_id=created["id"],
    )
    assert reenabled["enabled"] is True
    assert reenabled["is_default"] is True
    assert reenabled["config"] == disabled["config"]
    assert reenabled["auth"] == disabled["auth"]


def test_disabling_default_promotes_another_enabled_provider(client):
    first = _save(client, provider_key="smsbower", enabled=True)
    second = _save(client, provider_key="herosms", enabled=True)

    _save(
        client,
        provider_key="smsbower",
        enabled=False,
        is_default=True,
        setting_id=first["id"],
    )
    settings = client.get("/api/provider-settings", params={"provider_type": "sms"}).json()
    by_key = {item["provider_key"]: item for item in settings}

    assert by_key["smsbower"]["enabled"] is False
    assert by_key["smsbower"]["is_default"] is False
    assert by_key["herosms"]["enabled"] is True
    assert by_key["herosms"]["is_default"] is True
    assert by_key["herosms"]["id"] == second["id"]
