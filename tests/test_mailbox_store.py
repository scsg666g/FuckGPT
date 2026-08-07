from __future__ import annotations

import pytest

from core.base_identity import MailboxIdentityProvider
from core.base_mailbox import MailboxAccount
from core.base_platform import Account
from core.mailbox_store import MailboxStore


class FakeMailbox:
    def __init__(self):
        self.seen = []

    def get_current_ids(self, account):
        self.seen.append(("ids", account.email))
        return set()


def _patch_mailbox_paths(monkeypatch, tmp_path):
    accounts = tmp_path / "mailbox_accounts.json"
    addresses = tmp_path / "mailbox_addresses.json"
    links = tmp_path / "account_mailbox_links.json"
    monkeypatch.setattr("core.mailbox_store.MAILBOX_ACCOUNTS_FILE", accounts)
    monkeypatch.setattr("core.mailbox_store.MAILBOX_ADDRESSES_FILE", addresses)
    monkeypatch.setattr("core.mailbox_store.ACCOUNT_MAILBOX_LINKS_FILE", links)
    return accounts, addresses, links


def test_mailbox_store_links_and_resolves_plaintext_credentials(monkeypatch, tmp_path):
    _patch_mailbox_paths(monkeypatch, tmp_path)
    created = FakeMailbox()
    captured = {}

    def fake_create_mailbox(provider, extra, proxy=None):
        captured.update({"provider": provider, "extra": extra, "proxy": proxy})
        return created

    monkeypatch.setattr("core.mailbox_store.create_mailbox", fake_create_mailbox)

    store = MailboxStore()
    account = store.create_account(
        {
            "provider": "local_ms_pool",
            "email": "parent@outlook.com",
            "credentials": {
                "email": "parent@outlook.com",
                "password": "mail-password",
                "client_id": "client-id",
                "refresh_token": "refresh-token",
            },
        }
    )
    address = store.reserve_address(
        {
            "mailbox_account_id": account["id"],
            "address": "parent+reg1@outlook.com",
            "reserved_for": {"platform": "chatgpt", "account_id": 7},
        }
    )
    store.link_account(
        platform="chatgpt",
        account_id=7,
        account_email="registered@example.com",
        mailbox_address_id=address["id"],
    )

    mailbox, mailbox_account, context = store.resolve_mailbox_for_account(
        platform="chatgpt",
        account_id=7,
        proxy="http://proxy.example",
    )

    assert mailbox is created
    assert isinstance(mailbox_account, MailboxAccount)
    assert mailbox_account.email == "parent+reg1@outlook.com"
    assert captured["provider"] == "local_ms_pool"
    assert captured["extra"]["refresh_token"] == "refresh-token"
    assert captured["proxy"] == "http://proxy.example"
    assert context["address"]["id"] == address["id"]


def test_record_registration_link_from_platform_account_metadata(monkeypatch, tmp_path):
    _patch_mailbox_paths(monkeypatch, tmp_path)
    store = MailboxStore()

    link = store.record_registration_link(
        account_id=42,
        platform_account=Account(
            platform="chatgpt",
            email="registered@example.com",
            password="Secret123!",
            extra={
                "identity": {
                    "mailbox": {
                        "provider": "local_ms_pool",
                        "email": "parent+reg2@outlook.com",
                    },
                    "provider_account": {
                        "provider_type": "mailbox",
                        "provider_name": "local_ms_pool",
                        "login_identifier": "parent@outlook.com",
                        "credentials": {
                            "email": "parent@outlook.com",
                            "password": "mail-password",
                        },
                    },
                    "provider_resource": {
                        "provider_type": "mailbox",
                        "provider_name": "local_ms_pool",
                        "resource_type": "mailbox",
                        "handle": "parent+reg2@outlook.com",
                        "metadata": {"alias_index": 2},
                    },
                },
            },
        ),
    )

    assert link is not None
    assert link["account_id"] == 42
    assert store.list_accounts()[0]["credentials"]["password"] == "mail-password"
    assert store.list_addresses()[0]["address"] == "parent+reg2@outlook.com"
    assert store.list_addresses()[0]["address_type"] == "alias"


def test_record_allocated_mailbox_persists_provider_resource_without_link(monkeypatch, tmp_path):
    _patch_mailbox_paths(monkeypatch, tmp_path)
    store = MailboxStore()

    allocation = store.record_allocated_mailbox(
        platform="chatgpt",
        mailbox_account=MailboxAccount(
            email="allocated@hotmail.com",
            account_id="hm-key",
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "hotmail007",
                    "login_identifier": "allocated@hotmail.com",
                    "credentials": {
                        "email": "allocated@hotmail.com",
                        "password": "mail-password",
                        "refresh_token": "refresh-token",
                    },
                    "metadata": {"source": "hotmail007"},
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "hotmail007",
                    "resource_type": "mailbox",
                    "resource_identifier": "hm-key",
                    "handle": "allocated@hotmail.com",
                    "metadata": {"product_id": "prod-1"},
                },
            },
        ),
        provider="hotmail007",
    )

    assert allocation is not None
    assert store.list_accounts()[0]["provider"] == "hotmail007"
    assert store.list_accounts()[0]["credentials"]["refresh_token"] == "refresh-token"
    assert store.list_addresses()[0]["address"] == "allocated@hotmail.com"
    assert store.list_addresses()[0]["reserved"] is True
    assert store.list_addresses()[0]["reserved_for"] == {"platform": "chatgpt", "status": "allocated"}
    assert store.list_links() == []


def test_list_resources_summarizes_mailbox_status_and_chatgpt_link(monkeypatch, tmp_path):
    _patch_mailbox_paths(monkeypatch, tmp_path)
    store = MailboxStore()
    account = store.create_account(
        {
            "provider": "local_ms_pool",
            "email": "parent@outlook.com",
            "credentials": {"email": "parent@outlook.com", "password": "mail-password"},
        }
    )
    first = store.reserve_address(
        {
            "mailbox_account_id": account["id"],
            "alias_index": 1,
            "reserved_for": {"platform": "chatgpt", "status": "allocated"},
        }
    )
    second = store.reserve_address(
        {
            "mailbox_account_id": account["id"],
            "alias_index": 2,
            "reserved_for": {"platform": "chatgpt", "status": "allocated"},
        }
    )
    store.link_account(
        platform="chatgpt",
        account_id=77,
        account_email="registered@example.com",
        mailbox_address_id=second["id"],
    )

    resources = store.list_resources()
    by_address = {item["address"]: item for item in resources}

    assert by_address["parent+reg1@outlook.com"]["status"] == "allocated"
    assert by_address["parent+reg1@outlook.com"]["chatgpt_account_id"] is None
    assert by_address["parent+reg2@outlook.com"]["status"] == "registered"
    assert by_address["parent+reg2@outlook.com"]["chatgpt_account_id"] == 77
    assert by_address["parent+reg2@outlook.com"]["provider"] == "local_ms_pool"


def test_list_resources_only_returns_actual_mailbox_addresses(monkeypatch, tmp_path):
    _patch_mailbox_paths(monkeypatch, tmp_path)
    store = MailboxStore()
    store.create_account(
        {
            "provider": "local_ms_pool",
            "email": "parent@outlook.com",
            "credentials": {"email": "parent@outlook.com", "password": "mail-password"},
        }
    )

    assert store.list_resources() == []


def test_list_messages_for_address_uses_stored_mailbox_credentials(monkeypatch, tmp_path):
    _patch_mailbox_paths(monkeypatch, tmp_path)
    captured = {}

    class FakeMailbox:
        def list_messages(self, account, limit=10):
            captured["account"] = account
            captured["limit"] = limit
            return [{"subject": "hello", "preview": "body"}]

    def fake_create_mailbox(provider, extra, proxy=None):
        captured["provider"] = provider
        captured["extra"] = extra
        captured["proxy"] = proxy
        return FakeMailbox()

    monkeypatch.setattr("core.mailbox_store.create_mailbox", fake_create_mailbox)
    store = MailboxStore()
    account = store.create_account(
        {
            "provider": "local_ms_pool",
            "email": "parent@outlook.com",
            "credentials": {"email": "parent@outlook.com", "password": "mail-password"},
        }
    )
    address = store.reserve_address(
        {
            "mailbox_account_id": account["id"],
            "address": "parent+reg1@outlook.com",
        }
    )

    messages = store.list_messages_for_address(mailbox_address_id=address["id"], limit=3, proxy="http://proxy.example")

    assert messages == [{"subject": "hello", "preview": "body"}]
    assert captured["provider"] == "local_ms_pool"
    assert captured["extra"]["password"] == "mail-password"
    assert captured["proxy"] == "http://proxy.example"
    assert captured["account"].email == "parent+reg1@outlook.com"
    assert captured["limit"] == 3


class _FailingCurrentIdsMailbox:
    def __init__(self, mailbox_account: MailboxAccount):
        self.mailbox_account = mailbox_account

    def get_email(self):
        return self.mailbox_account

    def get_current_ids(self, account):
        raise RuntimeError("ids failed")


def test_identity_provider_prerecords_mailbox_before_current_ids_failure(monkeypatch, tmp_path):
    _patch_mailbox_paths(monkeypatch, tmp_path)
    mailbox_account = MailboxAccount(
        email="failed-after-buy@hotmail.com",
        account_id="hm-key-failed",
        extra={
            "provider_account": {
                "provider_type": "mailbox",
                "provider_name": "hotmail007",
                "login_identifier": "failed-after-buy@hotmail.com",
                "credentials": {"email": "failed-after-buy@hotmail.com", "password": "mail-password"},
            },
            "provider_resource": {
                "provider_type": "mailbox",
                "provider_name": "hotmail007",
                "resource_type": "mailbox",
                "resource_identifier": "hm-key-failed",
                "handle": "failed-after-buy@hotmail.com",
            },
        },
    )
    logs = []
    provider = MailboxIdentityProvider(
        mailbox=_FailingCurrentIdsMailbox(mailbox_account),
        extra={"mail_provider": "hotmail007", "_platform_name": "chatgpt", "_log_fn": logs.append},
    )

    with pytest.raises(RuntimeError, match="ids failed"):
        provider.resolve()

    resources = MailboxStore().list_resources()
    assert resources[0]["address"] == "failed-after-buy@hotmail.com"
    assert resources[0]["status"] == "available"
    assert resources[0]["allocation_status"] == "failed"
    assert MailboxStore().list_links() == []
    assert logs == ["邮箱资源已分配: failed-after-buy@hotmail.com"]


def test_mailbox_api_manages_json_resources(client, monkeypatch, tmp_path):
    accounts_path, addresses_path, links_path = _patch_mailbox_paths(monkeypatch, tmp_path)
    monkeypatch.setattr("api.mailboxes.MAILBOX_ACCOUNTS_FILE", accounts_path)
    monkeypatch.setattr("api.mailboxes.MAILBOX_ADDRESSES_FILE", addresses_path)
    monkeypatch.setattr("api.mailboxes.ACCOUNT_MAILBOX_LINKS_FILE", links_path)

    created = client.post(
        "/api/mailboxes/accounts",
        json={
            "provider": "local_ms_pool",
            "email": "parent@outlook.com",
            "credentials": {"password": "mail-password"},
        },
    )
    assert created.status_code == 200
    mailbox_account_id = created.json()["id"]

    address = client.post(
        "/api/mailboxes/addresses/reserve",
        json={"mailbox_account_id": mailbox_account_id, "alias_index": 1},
    )
    assert address.status_code == 200
    mailbox_address_id = address.json()["id"]
    assert address.json()["address"] == "parent+reg1@outlook.com"

    linked = client.post(
        "/api/mailboxes/account-link",
        json={
            "platform": "chatgpt",
            "account_id": 99,
            "account_email": "registered@example.com",
            "mailbox_address_id": mailbox_address_id,
        },
    )
    assert linked.status_code == 200
    assert linked.json()["account_id"] == 99

    listed = client.get("/api/mailboxes")
    assert listed.status_code == 200
    assert len(listed.json()["accounts"]) == 1
    assert len(listed.json()["addresses"]) == 1
    assert len(listed.json()["links"]) == 1
