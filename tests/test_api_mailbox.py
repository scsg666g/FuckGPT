from __future__ import annotations

import json

from core.api_mailbox import ApiMailboxPool, parse_api_mailbox_rows


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self.payload, str):
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return FakeResponse(payload)


def test_parse_api_mailbox_rows_accepts_email_and_full_api_url():
    rows = parse_api_mailbox_rows(
        "user+tag@outlook.com----https://mail.example/api/code?email=user%2Btag%40outlook.com&pass=secret&json=1"
    )

    assert len(rows) == 1
    assert rows[0].email == "user+tag@outlook.com"
    assert rows[0].api_url.endswith("&json=1")


def test_parse_api_mailbox_rows_accepts_flysms_pickup_url():
    rows = parse_api_mailbox_rows(
        "lyses-danish-7c@icloud.com---tok_secret---"
        "https://flysms.xyz/icloud/pickup#email=lyses-danish-7c%40icloud.com&key=tok_secret"
    )

    assert len(rows) == 1
    assert rows[0].email == "lyses-danish-7c@icloud.com"
    assert rows[0].api_url == "https://flysms.xyz/icloud/api/pickup/messages/latest"
    assert rows[0].token == "tok_secret"
    assert rows[0].referer == "https://flysms.xyz/icloud/pickup"
    assert rows[0].provider == "flysms"


def test_api_mailbox_rejects_token_format_for_unknown_provider():
    try:
        parse_api_mailbox_rows("user@example.com---tok_secret---https://mail.example/pickup")
    except ValueError as exc:
        assert "仅支持 flysms" in str(exc)
    else:
        raise AssertionError("unknown token mailbox row should fail")


def test_api_mailbox_rejects_invalid_row_format():
    try:
        parse_api_mailbox_rows("user@example.com")
    except ValueError as exc:
        assert "邮箱----完整 API URL" in str(exc)
    else:
        raise AssertionError("invalid API mailbox row should fail")


def test_api_mailbox_ignores_baseline_code_and_returns_new_code(tmp_path):
    session = FakeSession([
        {"data": {"verification_code": "111111"}},
        {"data": {"verification_code": "111111"}},
        {"data": {"verification_code": "654321"}},
    ])
    mailbox = ApiMailboxPool(
        pool_text="user@example.com----https://mail.example/api/code?token=secret",
        state_file=str(tmp_path / "state.json"),
        poll_interval=0,
        session=session,
    )
    account = mailbox.get_email()
    before_ids = mailbox.get_current_ids(account)

    code = mailbox.wait_for_code(account, timeout=1, before_ids=before_ids)

    assert code == "654321"
    assert session.calls[0][0] == "https://mail.example/api/code?token=secret"


def test_api_mailbox_extracts_labelled_plain_text_without_using_email_digits(tmp_path):
    session = FakeSession([
        "email=user927958@example.com; verification code: 482615",
    ])
    mailbox = ApiMailboxPool(
        pool_text="user927958@example.com----https://mail.example/code",
        state_file=str(tmp_path / "state.json"),
        allow_reuse=True,
        poll_interval=0,
        session=session,
    )
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, timeout=1) == "482615"


def test_api_mailbox_reads_flysms_latest_message_with_auth_headers(tmp_path):
    session = FakeSession([
        {
            "email": "lyses-danish-7c@icloud.com",
            "message": {
                "mailbox": "INBOX",
                "uid": 4295005876,
                "subject": "Your temporary ChatGPT verification code",
                "from": "ChatGPT <noreply@tm.openai.com>",
                "to": "lyses-danish-7c@icloud.com",
                "date": "2026-07-28T10:31:06.000Z",
                "text": "Enter this temporary verification code to continue: 488272 Please ignore this email.",
                "html": "<!doctype html><html><body>488272</body></html>",
            },
        }
    ])
    mailbox = ApiMailboxPool(
        pool_text=(
            "lyses-danish-7c@icloud.com---tok_secret---"
            "https://flysms.top/icloud/pickup#email=lyses-danish-7c%40icloud.com&key=tok_secret"
        ),
        state_file=str(tmp_path / "state.json"),
        poll_interval=0,
        session=session,
    )
    account = mailbox.get_email()

    assert mailbox.wait_for_code(account, timeout=1) == "488272"
    url, kwargs = session.calls[0]
    headers = kwargs["headers"]
    assert url == "https://flysms.top/icloud/api/pickup/messages/latest"
    assert headers["Authorization"] == "Bearer tok_secret"
    assert headers["X-Mailbox-Email"] == "lyses-danish-7c@icloud.com"
    assert headers["Referer"] == "https://flysms.top/icloud/pickup"


def test_api_mailbox_account_metadata_keeps_runtime_api_url(tmp_path):
    api_url = "https://mail.example/api/code?token=secret"
    mailbox = ApiMailboxPool(
        pool_text=f"user@example.com----{api_url}",
        state_file=str(tmp_path / "state.json"),
    )

    account = mailbox.get_email()

    assert account.extra["provider_account"]["provider_name"] == "api_mailbox"
    assert account.extra["provider_account"]["credentials"]["api_url"] == api_url


def test_api_mailbox_account_metadata_keeps_flysms_runtime_credentials(tmp_path):
    mailbox = ApiMailboxPool(
        pool_text="user@icloud.com---tok_secret---https://flysms.xyz/icloud/pickup#email=user%40icloud.com&key=tok_secret",
        state_file=str(tmp_path / "state.json"),
    )

    account = mailbox.get_email()
    credentials = account.extra["provider_account"]["credentials"]

    assert credentials["api_url"] == "https://flysms.xyz/icloud/api/pickup/messages/latest"
    assert credentials["token"] == "tok_secret"
    assert credentials["referer"] == "https://flysms.xyz/icloud/pickup"
    assert credentials["provider"] == "flysms"


def test_api_mailbox_legacy_used_ledger_no_longer_decides_availability(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"used": {"user@example.com": {"email": "user@example.com"}}}),
        encoding="utf-8",
    )
    mailbox = ApiMailboxPool(
        pool_text="user@example.com----https://mail.example/code",
        state_file=str(state_file),
    )

    assert mailbox.get_email().email == "user@example.com"
    assert json.loads(state_file.read_text(encoding="utf-8"))["used"]
