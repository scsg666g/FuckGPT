from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core import db
from core.account_graph import patch_account_graph
from core.base_mailbox import MailboxAccount
from core.base_platform import Account
from core.db import (
    AccountModel,
    MailboxAccountLinkModel,
    MailboxAllocationModel,
    MailboxProviderAccountModel,
    MailboxResourceModel,
    ProviderAccountModel,
    ProviderResourceModel,
)
from core.mailbox_lifecycle import (
    ALLOCATION_CANCELLED,
    ALLOCATION_FAILED,
    ALLOCATION_INTERRUPTED,
    MailboxAllocationLifecycle,
    MailboxUnavailableError,
    RESOURCE_ARCHIVED,
    RESOURCE_ALLOCATED,
    RESOURCE_AVAILABLE,
    RESOURCE_BOUND,
)
from core.db import save_account


def _mailbox(address: str, *, identifier: str = "mailbox-key", parent: str = "parent@outlook.com") -> MailboxAccount:
    return MailboxAccount(
        email=address,
        account_id=identifier,
        extra={
            "provider_account": {
                "provider_type": "mailbox",
                "provider_name": "api_mailbox",
                "login_identifier": parent,
                "credentials": {"email": parent, "api_url": "https://mail.example/code?token=secret"},
            },
            "provider_resource": {
                "provider_type": "mailbox",
                "provider_name": "api_mailbox",
                "resource_type": "mailbox",
                "resource_identifier": identifier,
                "handle": address,
            },
        },
    )


def _resource(identifier: str = "mailbox-key") -> MailboxResourceModel:
    with Session(db.engine) as session:
        return session.exec(
            select(MailboxResourceModel).where(MailboxResourceModel.resource_identifier == identifier)
        ).one()


def test_failed_allocation_keeps_history_and_returns_resource_immediately():
    lifecycle = MailboxAllocationLifecycle()
    allocation = lifecycle.allocate(
        mailbox_account=_mailbox("failed@example.com"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="task-1:worker_1",
        task_id="task-1",
        subtask_id="worker_1",
    )

    assert lifecycle.release(allocation.id, outcome=ALLOCATION_FAILED, reason="registration failed") is True
    assert _resource().status == RESOURCE_AVAILABLE
    assert lifecycle.is_available(provider_name="api_mailbox", resource_identifier="mailbox-key") is True

    with Session(db.engine) as session:
        history = session.get(MailboxAllocationModel, allocation.id)
        assert history.status == ALLOCATION_FAILED
        assert history.reason == "registration failed"

    second = lifecycle.allocate(
        mailbox_account=_mailbox("failed@example.com"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="task-2:worker_1",
    )
    assert second.resource_id == allocation.resource_id


def test_existing_account_allocation_is_archived_instead_of_returned_to_pool():
    lifecycle = MailboxAllocationLifecycle()
    allocation = lifecycle.allocate(
        mailbox_account=_mailbox("existing@example.com"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="existing:worker_1",
    )

    assert lifecycle.mark_existing_account(allocation.id, reason="login_password") is True

    resource = _resource()
    assert resource.status == RESOURCE_ARCHIVED
    assert lifecycle.is_available(provider_name="api_mailbox", resource_identifier="mailbox-key") is False
    assert resource.get_metadata()["existing_account"] is True
    assert resource.get_metadata()["account_status"] == "existing_account"
    with Session(db.engine) as session:
        history = session.get(MailboxAllocationModel, allocation.id)
        assert history.status == ALLOCATION_FAILED
        assert history.reason == "login_password"


def test_existing_account_flag_is_immediate_and_survives_release():
    lifecycle = MailboxAllocationLifecycle()
    allocation = lifecycle.allocate(
        mailbox_account=_mailbox("existing@example.com"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="existing-flag:worker_1",
    )

    assert lifecycle.flag_existing_account(allocation.id, reason="login_password") is True
    flagged = _resource()
    assert flagged.status == RESOURCE_ALLOCATED
    assert flagged.get_metadata()["existing_account"] is True

    assert lifecycle.release(allocation.id, outcome=ALLOCATION_FAILED, reason="otp failed") is True
    assert _resource().status == RESOURCE_ARCHIVED
    assert lifecycle.is_available(provider_name="api_mailbox", resource_identifier="mailbox-key") is False


def test_active_allocation_prevents_double_claim():
    lifecycle = MailboxAllocationLifecycle()
    lifecycle.allocate(
        mailbox_account=_mailbox("busy@example.com"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="task-1:worker_1",
    )

    with pytest.raises(MailboxUnavailableError, match="不能再次分配"):
        lifecycle.allocate(
            mailbox_account=_mailbox("busy@example.com"),
            provider="api_mailbox",
            platform="chatgpt",
            attempt_id="task-2:worker_1",
        )


def test_success_binds_one_account_and_keeps_provider_credentials_once():
    lifecycle = MailboxAllocationLifecycle()
    allocation = lifecycle.allocate(
        mailbox_account=_mailbox("alias+one@outlook.com"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="task-1:worker_1",
    )
    with Session(db.engine) as session:
        account = AccountModel(platform="chatgpt", email="gpt@example.com", password="secret")
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = int(account.id)

    lifecycle.succeed(allocation.id, account_id=account_id, account_email="gpt@example.com")

    assert _resource().status == RESOURCE_BOUND
    assert lifecycle.is_available(provider_name="api_mailbox", resource_identifier="mailbox-key") is False
    with Session(db.engine) as session:
        assert len(session.exec(select(MailboxProviderAccountModel)).all()) == 1
        link = session.exec(select(MailboxAccountLinkModel)).one()
        assert link.account_id == account_id
        assert link.resource_id == allocation.resource_id


def test_cancel_and_restart_interruption_return_resources():
    lifecycle = MailboxAllocationLifecycle()
    cancelled = lifecycle.allocate(
        mailbox_account=_mailbox("cancelled@example.com", identifier="cancelled"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="cancel:worker_1",
    )
    lifecycle.release(cancelled.id, outcome=ALLOCATION_CANCELLED)
    assert _resource("cancelled").status == RESOURCE_AVAILABLE

    interrupted = lifecycle.allocate(
        mailbox_account=_mailbox("interrupted@example.com", identifier="interrupted"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="interrupt:worker_1",
    )
    assert lifecycle.interrupt_active() == 1
    assert _resource("interrupted").status == RESOURCE_AVAILABLE
    with Session(db.engine) as session:
        assert session.get(MailboxAllocationModel, interrupted.id).status == ALLOCATION_INTERRUPTED


def test_deleted_gpt_account_archives_mailbox_instead_of_returning_it():
    lifecycle = MailboxAllocationLifecycle()
    allocation = lifecycle.allocate(
        mailbox_account=_mailbox("bound@example.com"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="task:worker_1",
    )
    with Session(db.engine) as session:
        account = AccountModel(platform="chatgpt", email="gpt@example.com", password="secret")
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = int(account.id)
    lifecycle.succeed(allocation.id, account_id=account_id, account_email="gpt@example.com")

    assert lifecycle.archive_account_mailbox(account_id) == 1
    assert _resource().status == RESOURCE_ARCHIVED
    with Session(db.engine) as session:
        link = session.exec(select(MailboxAccountLinkModel)).one()
        assert link.account_id is None
        assert link.account_id_snapshot == account_id
        assert link.archived_at is not None


def test_account_save_and_mailbox_binding_can_roll_back_as_one_transaction():
    lifecycle = MailboxAllocationLifecycle()
    first = lifecycle.allocate(
        mailbox_account=_mailbox("first@example.com", identifier="first"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="first:worker_1",
    )
    with Session(db.engine) as session:
        account = AccountModel(platform="chatgpt", email="gpt@example.com", password="original")
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = int(account.id)
    lifecycle.succeed(first.id, account_id=account_id, account_email="gpt@example.com")

    second = lifecycle.allocate(
        mailbox_account=_mailbox("second@example.com", identifier="second"),
        provider="api_mailbox",
        platform="chatgpt",
        attempt_id="second:worker_1",
    )
    with pytest.raises(ValueError, match="一对一"):
        with Session(db.engine) as session:
            saved = save_account(
                Account(platform="chatgpt", email="gpt@example.com", password="changed"),
                session=session,
                commit=False,
            )
            lifecycle.succeed_in_session(
                session,
                second.id,
                account_id=int(saved.id),
                account_email=saved.email,
            )
            session.commit()

    with Session(db.engine) as session:
        assert session.get(AccountModel, account_id).password == "original"
        assert session.get(MailboxAllocationModel, second.id).status == "active"


def test_legacy_json_migration_binds_success_and_releases_unlinked(monkeypatch, tmp_path):
    accounts_path = tmp_path / "mailbox_accounts.json"
    addresses_path = tmp_path / "mailbox_addresses.json"
    links_path = tmp_path / "account_mailbox_links.json"
    accounts_path.write_text(
        '[{"id":"mbx_1","provider":"api_mailbox","email":"parent@example.com",'
        '"credentials":{"email":"parent@example.com","api_url":"https://mail.example/code"}}]',
        encoding="utf-8",
    )
    addresses_path.write_text(
        '[{"id":"addr_bound","mailbox_account_id":"mbx_1","address":"bound@example.com","reserved":true},'
        '{"id":"addr_failed","mailbox_account_id":"mbx_1","address":"failed@example.com","reserved":true}]',
        encoding="utf-8",
    )
    with Session(db.engine) as session:
        account = AccountModel(platform="chatgpt", email="gpt@example.com", password="secret")
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = int(account.id)
    links_path.write_text(
        f'[{{"id":"link_1","platform":"chatgpt","account_id":{account_id},'
        '"account_email":"gpt@example.com","mailbox_address_id":"addr_bound","status":"active"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr("core.mailbox_store.MAILBOX_ACCOUNTS_FILE", accounts_path)
    monkeypatch.setattr("core.mailbox_store.MAILBOX_ADDRESSES_FILE", addresses_path)
    monkeypatch.setattr("core.mailbox_store.ACCOUNT_MAILBOX_LINKS_FILE", links_path)

    result = MailboxAllocationLifecycle().migrate_legacy_json()

    assert result == {"imported": 2, "bound": 1, "released": 1}
    resources = {item.address: item.status for item in [_resource("bound@example.com"), _resource("failed@example.com")]}
    assert resources == {"bound@example.com": RESOURCE_BOUND, "failed@example.com": RESOURCE_AVAILABLE}


def test_migration_recovers_bound_mailbox_from_account_graph_after_json_delete(monkeypatch, tmp_path):
    for name in ("mailbox_accounts.json", "mailbox_addresses.json", "account_mailbox_links.json"):
        (tmp_path / name).write_text("[]", encoding="utf-8")
    monkeypatch.setattr("core.mailbox_store.MAILBOX_ACCOUNTS_FILE", tmp_path / "mailbox_accounts.json")
    monkeypatch.setattr("core.mailbox_store.MAILBOX_ADDRESSES_FILE", tmp_path / "mailbox_addresses.json")
    monkeypatch.setattr("core.mailbox_store.ACCOUNT_MAILBOX_LINKS_FILE", tmp_path / "account_mailbox_links.json")
    with Session(db.engine) as session:
        account = AccountModel(platform="chatgpt", email="gpt@example.com", password="secret")
        session.add(account)
        session.flush()
        patch_account_graph(
            session,
            account,
            provider_accounts=[
                {
                    "provider_type": "mailbox",
                    "provider_name": "api_mailbox",
                    "login_identifier": "deleted@example.com",
                    "credentials": {"api_url": "https://mail.example/code"},
                }
            ],
            provider_resources=[
                {
                    "provider_type": "mailbox",
                    "provider_name": "api_mailbox",
                    "resource_type": "mailbox",
                    "resource_identifier": "deleted@example.com",
                    "handle": "deleted@example.com",
                }
            ],
        )
        session.commit()
        account_id = int(account.id)

    result = MailboxAllocationLifecycle().migrate_legacy_json()

    assert result == {"imported": 1, "bound": 1, "released": 0}
    assert _resource("deleted@example.com").status == RESOURCE_BOUND
    with Session(db.engine) as session:
        link = session.exec(select(MailboxAccountLinkModel)).one()
        assert link.account_id == account_id
        assert session.exec(select(ProviderAccountModel)).all() == []
        assert session.exec(select(ProviderResourceModel)).all() == []
