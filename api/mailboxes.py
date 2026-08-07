from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.mailbox_store import (
    ACCOUNT_MAILBOX_LINKS_FILE,
    MAILBOX_ACCOUNTS_FILE,
    MAILBOX_ADDRESSES_FILE,
    MailboxStore,
)


router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


class MailboxAccountRequest(BaseModel):
    provider: str = "local_ms_pool"
    email: str
    login_account: str = ""
    status: str = "active"
    credentials: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MailboxAccountPatchRequest(BaseModel):
    provider: Optional[str] = None
    email: Optional[str] = None
    login_account: Optional[str] = None
    status: Optional[str] = None
    credentials: Optional[dict[str, Any]] = None
    capabilities: Optional[dict[str, Any]] = None
    usage: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


class MailboxAddressReserveRequest(BaseModel):
    mailbox_account_id: str
    address: str = ""
    address_type: str = "primary"
    alias_index: int = 0
    reserved: bool = True
    reserved_for: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountMailboxLinkRequest(BaseModel):
    platform: str = "chatgpt"
    account_id: int
    account_email: str = ""
    mailbox_address_id: str
    purpose: str = "verification"


def _store() -> MailboxStore:
    return MailboxStore()


def _paths() -> dict[str, str]:
    return {
        "accounts": str(MAILBOX_ACCOUNTS_FILE),
        "addresses": str(MAILBOX_ADDRESSES_FILE),
        "links": str(ACCOUNT_MAILBOX_LINKS_FILE),
    }


@router.get("")
def get_mailboxes():
    store = _store()
    return {
        "resources": store.list_resources(),
        "accounts": store.list_accounts(),
        "addresses": store.list_addresses(),
        "links": store.list_links(),
        "paths": _paths(),
    }


@router.get("/resources")
def list_mailbox_resources():
    return {"items": _store().list_resources(), "paths": _paths()}


@router.get("/accounts")
def list_mailbox_accounts():
    return {"items": _store().list_accounts(), "path": str(MAILBOX_ACCOUNTS_FILE)}


@router.post("/accounts")
def create_mailbox_account(body: MailboxAccountRequest):
    try:
        return _store().create_account(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/accounts/{mailbox_account_id}")
def update_mailbox_account(mailbox_account_id: str, body: MailboxAccountPatchRequest):
    payload = body.model_dump(exclude_unset=True)
    try:
        item = _store().update_account(mailbox_account_id, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not item:
        raise HTTPException(404, "邮箱账号不存在")
    return item


@router.delete("/accounts/{mailbox_account_id}")
def delete_mailbox_account(mailbox_account_id: str):
    if not _store().delete_account(mailbox_account_id):
        raise HTTPException(404, "邮箱账号不存在")
    return {"ok": True}


@router.get("/addresses")
def list_mailbox_addresses():
    return {"items": _store().list_addresses(), "path": str(MAILBOX_ADDRESSES_FILE)}


@router.post("/addresses/reserve")
def reserve_mailbox_address(body: MailboxAddressReserveRequest):
    try:
        return _store().reserve_address(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/addresses/{mailbox_address_id}/release")
def release_mailbox_address(mailbox_address_id: str):
    item = _store().release_address(mailbox_address_id)
    if not item:
        raise HTTPException(404, "邮箱地址不存在")
    return item


@router.get("/addresses/{mailbox_address_id}/messages")
def list_mailbox_address_messages(mailbox_address_id: str, limit: int = 10):
    try:
        return {"items": _store().list_messages_for_address(mailbox_address_id=mailbox_address_id, limit=limit)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/links")
def list_account_mailbox_links():
    return {"items": _store().list_links(), "path": str(ACCOUNT_MAILBOX_LINKS_FILE)}


@router.get("/accounts/{account_id}/link")
def get_account_mailbox_link(account_id: int, platform: str = "chatgpt", purpose: str = "verification"):
    item = _store().get_link_for_account(platform=platform, account_id=account_id, purpose=purpose)
    if not item:
        raise HTTPException(404, "账号未绑定验证邮箱")
    return item


@router.post("/account-link")
def link_account_mailbox(body: AccountMailboxLinkRequest):
    try:
        return _store().link_account(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/accounts/{account_id}/link")
def unlink_account_mailbox(account_id: int, platform: str = "chatgpt", purpose: str = "verification"):
    if not _store().unlink_account(platform=platform, account_id=account_id, purpose=purpose):
        raise HTTPException(404, "账号未绑定验证邮箱")
    return {"ok": True}
