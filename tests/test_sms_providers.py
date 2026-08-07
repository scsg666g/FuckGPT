from __future__ import annotations

import json

from core.fivesim_sms import FiveSimClient
from core.herosms_sms import HeroSMSClient
from core.smspool_sms import SMSPoolClient
from core.smstome_sms import SMSToMeClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payloads.pop(0))


def test_herosms_uses_compatible_api_with_own_provider_identity():
    session = FakeSession([{"activationId": "h-1", "phoneNumber": "+15550001111"}])
    client = HeroSMSClient.from_config(
        {
            "herosms_api_key": "hero-secret",
            "herosms_default_country": "12",
            "herosms_default_service": "DR",
            "herosms_operator": "any",
            "herosms_fixed_price": "true",
        }
    )
    client.session = session

    activation = client.get_number()

    assert activation.provider == "herosms"
    assert activation.service == "dr"
    assert session.calls[0][0] == "https://hero-sms.com/stubs/handler_api.php"
    assert session.calls[0][1]["params"]["action"] == "getNumberV2"
    assert session.calls[0][1]["params"]["country"] == "12"
    assert session.calls[0][1]["params"]["operator"] == "any"
    assert session.calls[0][1]["params"]["fixedPrice"] == "true"


def test_smspool_purchase_status_cancel_and_catalog_contracts():
    session = FakeSession(
        [
            {"success": 1, "orderid": "p-1", "phonenumber": "+447700900001"},
            {"success": 1, "status": "pending", "sms": None},
            {"success": 1, "sms": "Your code is 654321"},
            {"success": 1, "message": "cancelled"},
            [{"ID": 9, "name": "United Kingdom", "short_name": "GB"}],
            [{"ID": 671, "name": "OpenAI / ChatGPT"}],
        ]
    )
    client = SMSPoolClient(
        api_key="pool-secret",
        default_country="9",
        default_service="671",
        max_price="0.20",
        session=session,
    )

    activation = client.get_number()
    assert activation.provider == "smspool"
    assert session.calls[0][1]["params"] == {
        "key": "pool-secret",
        "country": "9",
        "service": "671",
        "max_price": "0.20",
        "pricing_option": "0",
    }
    assert client.get_status("p-1").status == "waiting"
    assert client.get_status("p-1").code == "654321"
    client.cancel("p-1")
    assert session.calls[3][0].endswith("/sms/cancel")
    assert client.list_country_options() == [{
        "value": "9", "label": "United Kingdom (GB)", "english_name": "United Kingdom",
        "localized_name": "", "region_code": "GB", "dial_code": "",
    }]
    assert client.list_service_options() == [{"value": "671", "label": "OpenAI / ChatGPT (671)"}]


def test_fivesim_purchase_poll_and_lifecycle_contracts():
    session = FakeSession(
        [
            {"id": 77, "phone": "+84901234567", "status": "PENDING"},
            {"id": 77, "status": "RECEIVED", "sms": [{"text": "OpenAI code 112233"}]},
            {"id": 77, "status": "FINISHED"},
        ]
    )
    client = FiveSimClient(
        api_key="five-secret",
        default_country="vietnam",
        operator="any",
        max_price="4.5",
        session=session,
    )

    activation = client.get_number()
    assert activation.provider == "fivesim"
    assert session.calls[0][0].endswith("/v1/user/buy/activation/vietnam/any/openai")
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer five-secret"
    assert session.calls[0][1]["params"] == {"maxPrice": "4.5"}
    assert client.get_status("77").code == "112233"
    client.finish("77")
    assert session.calls[2][0].endswith("/v1/user/finish/77")


def test_fivesim_country_catalog_uses_buy_endpoint_slug_not_internal_id():
    client = FiveSimClient(session=FakeSession([{"vietnam": {"id": 999, "text_en": "Vietnam", "iso": {"VN": 1}}}]))

    assert client.list_country_options() == [{
        "value": "vietnam", "label": "Vietnam (vietnam)", "english_name": "Vietnam",
        "localized_name": "", "region_code": "VN", "dial_code": "",
    }]


def test_smstome_phone_pool_ignores_existing_messages_and_reads_new_otp(tmp_path):
    phone_page = """
        <html><article><a href='/poland/phone/48555123456/sms/10'>+48555123456</a></article></html>
    """
    old_inbox = """
        <table><tr><th>From</th><th>Time</th><th>Message</th></tr>
        <tr><td>Old</td><td>10 minutes ago</td><td>Old code 111111</td></tr></table>
    """
    new_inbox = """
        <table><tr><th>From</th><th>Time</th><th>Message</th></tr>
        <tr><td>OpenAI</td><td>just now</td><td>Your code is 22-33-44</td></tr>
        <tr><td>Old</td><td>10 minutes ago</td><td>Old code 111111</td></tr></table>
    """
    session = FakeSession([phone_page, old_inbox, new_inbox])
    client = SMSToMeClient(
        default_country="poland",
        state_file=str(tmp_path / "smstome-state.json"),
        max_pages_per_country=1,
        session=session,
    )

    activation = client.get_number()
    assert activation.provider == "smstome"
    assert activation.phone_number == "+48555123456"
    assert json.loads((tmp_path / "smstome-state.json").read_text())["used_numbers"] == ["+48555123456"]
    assert client.mark_sms_sent(activation.activation_id) == "BASELINE_RECORDED"
    assert client.get_status(activation.activation_id).code == "223344"
