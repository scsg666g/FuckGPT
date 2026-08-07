"""Canonical mailbox allocation lifecycle backed by SQLite.

Provider adapters describe and operate a mailbox.  This module alone decides
whether the mailbox may be allocated again and owns every state transition.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core import db
from core.db import (
    MailboxAccountLinkModel,
    MailboxAllocationModel,
    DataMigrationModel,
    MailboxProviderAccountModel,
    MailboxResourceModel,
    ProviderAccountModel,
    ProviderResourceModel,
)


RESOURCE_AVAILABLE = "available"
RESOURCE_ALLOCATED = "allocated"
RESOURCE_BOUND = "bound"
RESOURCE_EXPIRED = "expired"
RESOURCE_ARCHIVED = "archived"

ALLOCATION_ACTIVE = "active"
ALLOCATION_SUCCEEDED = "succeeded"
ALLOCATION_FAILED = "failed"
ALLOCATION_CANCELLED = "cancelled"
ALLOCATION_INTERRUPTED = "interrupted"

TERMINAL_ALLOCATION_STATUSES = {
    ALLOCATION_SUCCEEDED,
    ALLOCATION_FAILED,
    ALLOCATION_CANCELLED,
    ALLOCATION_INTERRUPTED,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_address(value: Any) -> str:
    return _text(value).lower()


class MailboxUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class MailboxAllocation:
    id: str
    resource_id: int
    attempt_id: str
    address: str
    provider_name: str


class MailboxAllocationLifecycle:
    """Deep module for allocation, release, success linking, and recovery."""

    @staticmethod
    def material_from_account(mailbox_account: Any, provider: str = "") -> dict[str, Any]:
        extra = dict(getattr(mailbox_account, "extra", {}) or {})
        provider_account = dict(extra.get("provider_account") or {})
        provider_resource = dict(extra.get("provider_resource") or {})
        credentials = dict(provider_account.get("credentials") or {})
        resource_metadata = dict(provider_resource.get("metadata") or {})
        address = _text(
            provider_resource.get("handle")
            or resource_metadata.get("email")
            or getattr(mailbox_account, "email", "")
        )
        provider_name = _text(
            provider_resource.get("provider_name")
            or provider_account.get("provider_name")
            or provider
        )
        resource_identifier = _normalize_address(
            provider_resource.get("resource_identifier")
            or getattr(mailbox_account, "account_id", "")
            or address
        )
        parent_address = _text(
            credentials.get("email")
            or (provider_account.get("metadata") or {}).get("parent_email")
            or resource_metadata.get("parent_email")
            or address
        )
        if not provider_name or not resource_identifier or not address:
            raise ValueError("邮箱 provider 未返回完整的资源标识、地址和 provider 名称")
        return {
            "provider_name": provider_name,
            "resource_identifier": resource_identifier,
            "address": address,
            "parent_address": parent_address,
            "provider_account": provider_account,
            "provider_resource": provider_resource,
            "metadata": resource_metadata,
        }

    def is_available(self, *, provider_name: str, resource_identifier: str) -> bool:
        provider_name = _text(provider_name)
        resource_identifier = _normalize_address(resource_identifier)
        with Session(db.engine) as session:
            resource = session.exec(
                select(MailboxResourceModel)
                .where(MailboxResourceModel.provider_name == provider_name)
                .where(MailboxResourceModel.resource_identifier == resource_identifier)
            ).first()
            return resource is None or resource.status == RESOURCE_AVAILABLE

    def allocate(
        self,
        *,
        mailbox_account: Any,
        provider: str,
        platform: str,
        attempt_id: str,
        task_id: str = "",
        subtask_id: str = "",
    ) -> MailboxAllocation:
        material = self.material_from_account(mailbox_account, provider)
        attempt_id = _text(attempt_id) or f"manual:{uuid.uuid4().hex}"
        now = _utcnow()
        try:
            with Session(db.engine) as session:
                existing_allocation = session.exec(
                    select(MailboxAllocationModel).where(MailboxAllocationModel.attempt_id == attempt_id)
                ).first()
                if existing_allocation:
                    resource = session.get(MailboxResourceModel, existing_allocation.resource_id)
                    if existing_allocation.status != ALLOCATION_ACTIVE or resource is None:
                        raise MailboxUnavailableError(f"注册尝试 {attempt_id} 已结束，不能重复领取邮箱")
                    return self._allocation_value(existing_allocation, resource)

                resource = session.exec(
                    select(MailboxResourceModel)
                    .where(MailboxResourceModel.provider_name == material["provider_name"])
                    .where(MailboxResourceModel.resource_identifier == material["resource_identifier"])
                ).first()
                provider_account_payload = material["provider_account"]
                provider_login_identifier = _normalize_address(
                    provider_account_payload.get("login_identifier")
                    or (provider_account_payload.get("credentials") or {}).get("login_account")
                    or material["parent_address"]
                )
                provider_account = session.exec(
                    select(MailboxProviderAccountModel)
                    .where(MailboxProviderAccountModel.provider_name == material["provider_name"])
                    .where(MailboxProviderAccountModel.login_identifier == provider_login_identifier)
                ).first()
                if provider_account is None:
                    provider_account = MailboxProviderAccountModel(
                        provider_name=material["provider_name"],
                        login_identifier=provider_login_identifier,
                    )
                provider_account.display_name = _text(
                    provider_account_payload.get("display_name") or material["parent_address"]
                )
                provider_account.set_credentials(provider_account_payload.get("credentials") or {})
                provider_account.set_metadata(provider_account_payload.get("metadata") or {})
                provider_account.updated_at = now
                session.add(provider_account)
                session.flush()
                if resource is None:
                    resource = MailboxResourceModel(
                        provider_account_id=int(provider_account.id or 0),
                        provider_name=material["provider_name"],
                        resource_identifier=material["resource_identifier"],
                        address=material["address"],
                        parent_address=material["parent_address"],
                    )
                elif resource.status != RESOURCE_AVAILABLE:
                    raise MailboxUnavailableError(
                        f"邮箱 {resource.address} 当前状态为 {resource.status}，不能再次分配"
                    )
                resource.address = material["address"]
                resource.parent_address = material["parent_address"]
                resource.provider_account_id = int(provider_account.id or 0)
                resource.status = RESOURCE_ALLOCATED
                resource.set_provider_resource(material["provider_resource"])
                resource.set_metadata(material["metadata"])
                resource.updated_at = now
                session.add(resource)
                session.flush()

                allocation = MailboxAllocationModel(
                    id=f"mba_{uuid.uuid4().hex}",
                    resource_id=int(resource.id or 0),
                    attempt_id=attempt_id,
                    task_id=_text(task_id),
                    subtask_id=_text(subtask_id),
                    platform=_text(platform) or "chatgpt",
                    status=ALLOCATION_ACTIVE,
                    started_at=now,
                    updated_at=now,
                )
                session.add(allocation)
                session.commit()
                session.refresh(resource)
                session.refresh(allocation)
                return self._allocation_value(allocation, resource)
        except IntegrityError as exc:
            raise MailboxUnavailableError(
                f"邮箱 {material['address']} 已被另一个注册尝试领取"
            ) from exc

    def succeed(
        self,
        allocation_id: str,
        *,
        account_id: int,
        account_email: str,
        platform: str = "chatgpt",
    ) -> None:
        with Session(db.engine) as session:
            self.succeed_in_session(
                session,
                allocation_id,
                account_id=account_id,
                account_email=account_email,
                platform=platform,
            )
            session.commit()

    def succeed_in_session(
        self,
        session: Session,
        allocation_id: str,
        *,
        account_id: int,
        account_email: str,
        platform: str = "chatgpt",
    ) -> None:
        now = _utcnow()
        allocation = session.get(MailboxAllocationModel, _text(allocation_id))
        if allocation is None:
            raise ValueError("邮箱分配记录不存在")
        if allocation.status == ALLOCATION_SUCCEEDED:
            return
        if allocation.status != ALLOCATION_ACTIVE:
            raise ValueError(f"邮箱分配已经结束: {allocation.status}")
        resource = session.get(MailboxResourceModel, allocation.resource_id)
        if resource is None:
            raise ValueError("邮箱资源不存在")

        existing_resource_link = session.exec(
            select(MailboxAccountLinkModel).where(MailboxAccountLinkModel.resource_id == resource.id)
        ).first()
        existing_account_link = session.exec(
            select(MailboxAccountLinkModel).where(MailboxAccountLinkModel.account_id == int(account_id))
        ).first()
        if existing_resource_link or existing_account_link:
            link = existing_resource_link or existing_account_link
            if not (
                link
                and link.resource_id == resource.id
                and link.account_id == int(account_id)
            ):
                raise ValueError("GPT 账户与验证邮箱必须保持一对一关系")
        else:
            session.add(
                MailboxAccountLinkModel(
                    resource_id=int(resource.id or 0),
                    allocation_id=allocation.id,
                    account_id=int(account_id),
                    account_id_snapshot=int(account_id),
                    account_email=_text(account_email),
                    platform=_text(platform) or allocation.platform or "chatgpt",
                    linked_at=now,
                )
            )

        allocation.status = ALLOCATION_SUCCEEDED
        allocation.account_id = int(account_id)
        allocation.finished_at = now
        allocation.updated_at = now
        resource.status = RESOURCE_BOUND
        resource.updated_at = now
        session.add(allocation)
        session.add(resource)
        session.flush()

    def release(self, allocation_id: str, *, outcome: str, reason: str = "") -> bool:
        if outcome not in {ALLOCATION_FAILED, ALLOCATION_CANCELLED, ALLOCATION_INTERRUPTED}:
            raise ValueError(f"无效的邮箱释放结果: {outcome}")
        now = _utcnow()
        with Session(db.engine) as session:
            allocation = session.get(MailboxAllocationModel, _text(allocation_id))
            if allocation is None:
                return False
            if allocation.status in TERMINAL_ALLOCATION_STATUSES:
                return allocation.status == outcome
            resource = session.get(MailboxResourceModel, allocation.resource_id)
            allocation.status = outcome
            allocation.reason = _text(reason)
            allocation.finished_at = now
            allocation.updated_at = now
            session.add(allocation)
            if resource and resource.status == RESOURCE_ALLOCATED:
                resource.status = (
                    RESOURCE_ARCHIVED
                    if resource.get_metadata().get("existing_account")
                    else RESOURCE_AVAILABLE
                )
                resource.updated_at = now
                session.add(resource)
            session.commit()
            return True

    def flag_existing_account(self, allocation_id: str, *, reason: str = "检测到已有账号") -> bool:
        """Persist the provider-side existing-account classification immediately.

        The allocation remains active so a successful password/OTP login can
        still bind the mailbox to the saved account.  Any later release or
        interruption will archive the resource instead of returning it to the
        new-registration pool.
        """
        now = _utcnow()
        with Session(db.engine) as session:
            allocation = session.get(MailboxAllocationModel, _text(allocation_id))
            if allocation is None or allocation.status != ALLOCATION_ACTIVE:
                return False
            resource = session.get(MailboxResourceModel, allocation.resource_id)
            if resource is None:
                return False
            metadata = resource.get_metadata()
            metadata.update(
                {
                    "existing_account": True,
                    "account_status": "existing_account",
                    "existing_account_reason": _text(reason),
                }
            )
            resource.set_metadata(metadata)
            resource.updated_at = now
            session.add(resource)
            session.commit()
            return True

    def mark_existing_account(self, allocation_id: str, *, reason: str = "检测到已有账号") -> bool:
        """Finish an allocation without returning its mailbox to registration.

        A provider-side ``login_password`` state proves that the address is no
        longer a fresh registration identity.  Archive it even when login
        authentication later fails, so another worker cannot retry account
        creation with a newly generated password.
        """
        now = _utcnow()
        with Session(db.engine) as session:
            allocation = session.get(MailboxAllocationModel, _text(allocation_id))
            if allocation is None:
                return False
            resource = session.get(MailboxResourceModel, allocation.resource_id)
            if allocation.status == ALLOCATION_ACTIVE:
                allocation.status = ALLOCATION_FAILED
                allocation.reason = _text(reason)
                allocation.finished_at = now
                allocation.updated_at = now
                session.add(allocation)
            if resource is not None and resource.status != RESOURCE_BOUND:
                metadata = resource.get_metadata()
                metadata.update(
                    {
                        "existing_account": True,
                        "account_status": "existing_account",
                        "existing_account_reason": _text(reason),
                    }
                )
                resource.set_metadata(metadata)
                resource.status = RESOURCE_ARCHIVED
                resource.updated_at = now
                session.add(resource)
            session.commit()
            return True

    def interrupt_active(self, *, reason: str = "进程异常退出") -> int:
        now = _utcnow()
        with Session(db.engine) as session:
            allocations = list(
                session.exec(
                    select(MailboxAllocationModel).where(MailboxAllocationModel.status == ALLOCATION_ACTIVE)
                ).all()
            )
            for allocation in allocations:
                allocation.status = ALLOCATION_INTERRUPTED
                allocation.reason = reason
                allocation.finished_at = now
                allocation.updated_at = now
                resource = session.get(MailboxResourceModel, allocation.resource_id)
                if resource and resource.status == RESOURCE_ALLOCATED:
                    resource.status = (
                        RESOURCE_ARCHIVED
                        if resource.get_metadata().get("existing_account")
                        else RESOURCE_AVAILABLE
                    )
                    resource.updated_at = now
                    session.add(resource)
                session.add(allocation)
            session.commit()
            return len(allocations)

    def migrate_legacy_json(self) -> dict[str, int]:
        """Import legacy mailbox JSON as history; never use it for availability."""

        from core.base_mailbox import MailboxAccount
        from core.mailbox_store import (
            ACCOUNT_MAILBOX_LINKS_FILE,
            MAILBOX_ACCOUNTS_FILE,
            MAILBOX_ADDRESSES_FILE,
        )

        def read_list(path) -> list[dict[str, Any]]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return []
            if isinstance(data, dict):
                data = data.get("items") or []
            return [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []

        imported, bound = self._migrate_legacy_account_graph()
        released = 0
        accounts = read_list(MAILBOX_ACCOUNTS_FILE)
        addresses = read_list(MAILBOX_ADDRESSES_FILE)
        links = read_list(ACCOUNT_MAILBOX_LINKS_FILE)
        account_by_id = {str(item.get("id") or ""): item for item in accounts}
        link_by_address = {
            str(item.get("mailbox_address_id") or ""): item
            for item in links
            if str(item.get("status") or "active") == "active"
        }
        for address in addresses:
            address_id = _text(address.get("id"))
            parent = account_by_id.get(_text(address.get("mailbox_account_id")))
            email = _text(address.get("address"))
            provider = _text((parent or {}).get("provider"))
            if not address_id or not parent or not email or not provider:
                continue
            attempt_id = f"legacy-mailbox:{address_id}"
            with Session(db.engine) as session:
                if session.exec(
                    select(MailboxAllocationModel).where(MailboxAllocationModel.attempt_id == attempt_id)
                ).first():
                    continue
            credentials = dict(parent.get("credentials") or {})
            credentials.setdefault("email", parent.get("email") or email)
            credentials.setdefault("login_account", parent.get("login_account") or parent.get("email") or email)
            identifier = _normalize_address(email) if provider == "api_mailbox" else address_id
            mailbox_account = MailboxAccount(
                email=email,
                account_id=identifier,
                extra={
                    "provider_account": {
                        "provider_type": "mailbox",
                        "provider_name": provider,
                        "login_identifier": parent.get("login_account") or parent.get("email") or email,
                        "display_name": parent.get("email") or email,
                        "credentials": credentials,
                        "metadata": dict(parent.get("metadata") or {}),
                    },
                    "provider_resource": {
                        "provider_type": "mailbox",
                        "provider_name": provider,
                        "resource_type": "mailbox",
                        "resource_identifier": identifier,
                        "handle": email,
                        "metadata": dict(address.get("metadata") or {}),
                    },
                },
            )
            try:
                allocation = self.allocate(
                    mailbox_account=mailbox_account,
                    provider=provider,
                    platform="chatgpt",
                    attempt_id=attempt_id,
                )
            except MailboxUnavailableError:
                continue
            imported += 1
            link = link_by_address.get(address_id)
            linked_account_id = int((link or {}).get("account_id") or 0)
            with Session(db.engine) as session:
                account_exists = bool(linked_account_id and session.get(db.AccountModel, linked_account_id))
            if link and account_exists:
                self.succeed(
                    allocation.id,
                    account_id=linked_account_id,
                    account_email=_text(link.get("account_email")) or email,
                    platform=_text(link.get("platform")) or "chatgpt",
                )
                bound += 1
            else:
                self.release(
                    allocation.id,
                    outcome=ALLOCATION_INTERRUPTED,
                    reason="从旧 JSON 迁移；不存在成功 GPT 账户关系，已立即回池",
                )
                released += 1
            if str(address.get("status") or "").lower() in {"disabled", "inactive"}:
                self.archive_resource(allocation.resource_id, reason="旧邮箱资源已禁用")
        return {"imported": imported, "bound": bound, "released": released}

    def _migrate_legacy_account_graph(self) -> tuple[int, int]:
        """Recover successful mailbox links even if mailbox JSON was deleted."""

        from core.base_mailbox import MailboxAccount

        imported = 0
        bound = 0
        with Session(db.engine) as session:
            graph_resources = list(
                session.exec(
                    select(ProviderResourceModel)
                    .where(ProviderResourceModel.provider_type == "mailbox")
                    .where(ProviderResourceModel.resource_type == "mailbox")
                    .order_by(ProviderResourceModel.account_id, ProviderResourceModel.id)
                ).all()
            )
        linked_accounts: set[int] = set()
        for graph_resource in graph_resources:
            account_id = int(graph_resource.account_id or 0)
            if not account_id or account_id in linked_accounts:
                continue
            with Session(db.engine) as session:
                account = session.get(db.AccountModel, account_id)
                if account is None or str(account.platform or "").lower() != "chatgpt":
                    continue
                if session.exec(
                    select(MailboxAccountLinkModel).where(MailboxAccountLinkModel.account_id == account_id)
                ).first():
                    linked_accounts.add(account_id)
                    continue
                graph_account = session.exec(
                    select(ProviderAccountModel)
                    .where(ProviderAccountModel.account_id == account_id)
                    .where(ProviderAccountModel.provider_type == "mailbox")
                    .where(ProviderAccountModel.provider_name == graph_resource.provider_name)
                ).first()
                account_email = account.email
                account_platform = account.platform
            provider = _text(graph_resource.provider_name)
            address = _text(graph_resource.handle or graph_resource.resource_identifier or account_email)
            identifier = _normalize_address(graph_resource.resource_identifier or address)
            if not provider or not address or not identifier:
                continue
            provider_account = {
                "provider_type": "mailbox",
                "provider_name": provider,
                "login_identifier": _text(getattr(graph_account, "login_identifier", "")) or address,
                "display_name": _text(getattr(graph_account, "display_name", "")) or address,
                "credentials": graph_account.get_credentials() if graph_account else {},
                "metadata": graph_account.get_metadata() if graph_account else {},
            }
            mailbox_account = MailboxAccount(
                email=address,
                account_id=identifier,
                extra={
                    "provider_account": provider_account,
                    "provider_resource": {
                        "provider_type": "mailbox",
                        "provider_name": provider,
                        "resource_type": "mailbox",
                        "resource_identifier": identifier,
                        "handle": address,
                        "metadata": graph_resource.get_metadata(),
                    },
                },
            )
            try:
                allocation = self.allocate(
                    mailbox_account=mailbox_account,
                    provider=provider,
                    platform=account_platform,
                    attempt_id=f"legacy-account:{account_id}:{int(graph_resource.id or 0)}",
                )
            except MailboxUnavailableError:
                continue
            imported += 1
            self.succeed(
                allocation.id,
                account_id=account_id,
                account_email=account_email,
                platform=account_platform,
            )
            with Session(db.engine) as session:
                session.exec(
                    delete(ProviderResourceModel)
                    .where(ProviderResourceModel.account_id == account_id)
                    .where(ProviderResourceModel.provider_type == "mailbox")
                )
                session.exec(
                    delete(ProviderAccountModel)
                    .where(ProviderAccountModel.account_id == account_id)
                    .where(ProviderAccountModel.provider_type == "mailbox")
                )
                session.commit()
            bound += 1
            linked_accounts.add(account_id)
        return imported, bound

    def migrate_legacy_json_once(self) -> dict[str, int]:
        migration_key = "mailbox-lifecycle-json-v1"
        with Session(db.engine) as session:
            marker = session.get(DataMigrationModel, migration_key)
            if marker is not None:
                return {"imported": 0, "bound": 0, "released": 0}
        result = self.migrate_legacy_json()
        with Session(db.engine) as session:
            marker = session.get(DataMigrationModel, migration_key)
            if marker is None:
                marker = DataMigrationModel(key=migration_key)
            marker.completed_at = _utcnow()
            marker.set_detail(result)
            session.add(marker)
            session.commit()
        return result

    def archive_account_mailbox(self, account_id: int) -> int:
        """Preserve successful history while detaching a soon-to-be-deleted account."""

        now = _utcnow()
        with Session(db.engine) as session:
            links = list(
                session.exec(
                    select(MailboxAccountLinkModel).where(MailboxAccountLinkModel.account_id == int(account_id))
                ).all()
            )
            for link in links:
                resource = session.get(MailboxResourceModel, link.resource_id)
                if resource:
                    resource.status = RESOURCE_ARCHIVED
                    resource.updated_at = now
                    session.add(resource)
                link.account_id = None
                link.archived_at = now
                session.add(link)
                allocation = session.get(MailboxAllocationModel, link.allocation_id)
                if allocation:
                    allocation.account_id = None
                    allocation.updated_at = now
                    session.add(allocation)
            session.commit()
            return len(links)

    def list_resources(self) -> list[dict[str, Any]]:
        with Session(db.engine) as session:
            resources = list(
                session.exec(
                    select(MailboxResourceModel).order_by(
                        MailboxResourceModel.updated_at.desc(),
                        MailboxResourceModel.id.desc(),
                    )
                ).all()
            )
            result: list[dict[str, Any]] = []
            for resource in resources:
                latest = session.exec(
                    select(MailboxAllocationModel)
                    .where(MailboxAllocationModel.resource_id == int(resource.id or 0))
                    .order_by(MailboxAllocationModel.started_at.desc())
                ).first()
                link = session.exec(
                    select(MailboxAccountLinkModel).where(
                        MailboxAccountLinkModel.resource_id == int(resource.id or 0)
                    )
                ).first()
                result.append(self._serialize_resource(resource, latest, link))
            return result

    def get_resource(self, resource_id: int) -> MailboxResourceModel | None:
        with Session(db.engine) as session:
            resource = session.get(MailboxResourceModel, int(resource_id))
            if resource is None:
                return None
            # Detach all fields before the session closes.
            session.expunge(resource)
            return resource

    def get_provider_account_for_resource(self, resource_id: int) -> MailboxProviderAccountModel | None:
        with Session(db.engine) as session:
            resource = session.get(MailboxResourceModel, int(resource_id))
            if resource is None:
                return None
            provider_account = session.get(MailboxProviderAccountModel, resource.provider_account_id)
            if provider_account is None:
                return None
            session.expunge(provider_account)
            return provider_account

    def get_resource_for_account(self, account_id: int) -> MailboxResourceModel | None:
        with Session(db.engine) as session:
            link = session.exec(
                select(MailboxAccountLinkModel).where(MailboxAccountLinkModel.account_id == int(account_id))
            ).first()
            if link is None:
                return None
            resource = session.get(MailboxResourceModel, link.resource_id)
            if resource is None:
                return None
            session.expunge(resource)
            return resource

    def archive_resource(self, resource_id: int, *, reason: str = "用户归档") -> bool:
        now = _utcnow()
        with Session(db.engine) as session:
            resource = session.get(MailboxResourceModel, int(resource_id))
            if resource is None:
                return False
            active = session.exec(
                select(MailboxAllocationModel)
                .where(MailboxAllocationModel.resource_id == int(resource_id))
                .where(MailboxAllocationModel.status == ALLOCATION_ACTIVE)
            ).first()
            if active:
                active.status = ALLOCATION_CANCELLED
                active.reason = _text(reason)
                active.finished_at = now
                active.updated_at = now
                session.add(active)
            resource.status = RESOURCE_ARCHIVED
            resource.updated_at = now
            session.add(resource)
            session.commit()
            return True

    def release_resource(self, resource_id: int, *, reason: str = "用户释放") -> bool:
        with Session(db.engine) as session:
            resource = session.get(MailboxResourceModel, int(resource_id))
            if resource is None:
                return False
            if resource.status == RESOURCE_BOUND:
                raise ValueError("已关联 GPT 账户的邮箱不能释放；删除账户后只会归档")
            active = session.exec(
                select(MailboxAllocationModel)
                .where(MailboxAllocationModel.resource_id == int(resource_id))
                .where(MailboxAllocationModel.status == ALLOCATION_ACTIVE)
            ).first()
            allocation_id = active.id if active else ""
        if allocation_id:
            self.release(allocation_id, outcome=ALLOCATION_CANCELLED, reason=reason)
        return True

    @staticmethod
    def public_resource_id(resource_id: int) -> str:
        return f"mbr_{int(resource_id)}"

    @staticmethod
    def parse_public_resource_id(value: str) -> int | None:
        raw = _text(value)
        if not raw.startswith("mbr_"):
            return None
        try:
            return int(raw[4:])
        except ValueError:
            return None

    @staticmethod
    def allocation_id_from_platform(platform: Any) -> str:
        identity = getattr(platform, "_last_identity", None)
        metadata = dict(getattr(identity, "metadata", {}) or {})
        return _text(metadata.get("mailbox_allocation_id"))

    @staticmethod
    def allocation_id_from_account(account: Any) -> str:
        extra = dict(getattr(account, "extra", {}) or {})
        identity = dict(extra.get("identity") or {})
        metadata = dict(identity.get("metadata") or {})
        return _text(metadata.get("mailbox_allocation_id"))

    @staticmethod
    def _allocation_value(
        allocation: MailboxAllocationModel,
        resource: MailboxResourceModel,
    ) -> MailboxAllocation:
        return MailboxAllocation(
            id=allocation.id,
            resource_id=int(resource.id or 0),
            attempt_id=allocation.attempt_id,
            address=resource.address,
            provider_name=resource.provider_name,
        )

    @classmethod
    def _serialize_resource(
        cls,
        resource: MailboxResourceModel,
        latest: MailboxAllocationModel | None,
        link: MailboxAccountLinkModel | None,
    ) -> dict[str, Any]:
        public_id = cls.public_resource_id(int(resource.id or 0))
        return {
            "id": public_id,
            "resource_kind": "address",
            "mailbox_account_id": public_id,
            "mailbox_address_id": public_id,
            "address": resource.address,
            "address_type": "alias"
            if resource.parent_address
            and resource.parent_address.lower() != resource.address.lower()
            else "primary",
            "provider": resource.provider_name,
            "parent_email": resource.parent_address,
            "login_account": resource.parent_address,
            "status": resource.status,
            "mailbox_status": "inactive"
            if resource.status in {RESOURCE_EXPIRED, RESOURCE_ARCHIVED}
            else "active",
            "reserved": resource.status in {RESOURCE_ALLOCATED, RESOURCE_BOUND},
            "reserved_for": {
                "task_id": latest.task_id,
                "subtask_id": latest.subtask_id,
                "platform": latest.platform,
            }
            if latest and latest.status == ALLOCATION_ACTIVE
            else {},
            "usage": {},
            "metadata": resource.get_metadata(),
            "chatgpt_account_id": int(link.account_id) if link and link.account_id else None,
            "chatgpt_account_email": link.account_email if link else "",
            "link_id": f"mbl_{link.id}" if link and link.id else "",
            "link_status": "archived" if link and link.archived_at else ("active" if link else ""),
            "allocation_id": latest.id if latest else "",
            "allocation_status": latest.status if latest else "",
            "allocation_reason": latest.reason if latest else "",
            "updated_at": resource.updated_at.isoformat() if resource.updated_at else "",
            "created_at": resource.created_at.isoformat() if resource.created_at else "",
        }
