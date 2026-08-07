from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.base_mailbox import MailboxAccount, create_mailbox


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAILBOX_ACCOUNTS_FILE = DATA_DIR / "mailbox_accounts.json"
MAILBOX_ADDRESSES_FILE = DATA_DIR / "mailbox_addresses.json"
ACCOUNT_MAILBOX_LINKS_FILE = DATA_DIR / "account_mailbox_links.json"

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, dict)]


def _write_list(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _normalize_status(value: Any, default: str = "active") -> str:
    status = _text(value).lower()
    return status or default


def _normalize_capabilities(provider: str, capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(capabilities or {})
    if provider == "local_ms_pool":
        result.setdefault("supports_alias", True)
        result.setdefault("max_alias_count", 6)
        result.setdefault("supports_graph", True)
    return result


def _parent_address(email: str) -> str:
    local, sep, domain = _text(email).lower().rpartition("@")
    if not sep:
        return _text(email).lower()
    return f"{local.split('+', 1)[0]}@{domain}"


class MailboxStore:
    def list_accounts(self) -> list[dict[str, Any]]:
        with _LOCK:
            return _read_list(MAILBOX_ACCOUNTS_FILE)

    def list_addresses(self) -> list[dict[str, Any]]:
        with _LOCK:
            return _read_list(MAILBOX_ADDRESSES_FILE)

    def list_links(self) -> list[dict[str, Any]]:
        with _LOCK:
            return _read_list(ACCOUNT_MAILBOX_LINKS_FILE)

    def list_resources(self) -> list[dict[str, Any]]:
        from core.mailbox_lifecycle import MailboxAllocationLifecycle

        canonical = MailboxAllocationLifecycle().list_resources()
        with _LOCK:
            accounts = _read_list(MAILBOX_ACCOUNTS_FILE)
            addresses = _read_list(MAILBOX_ADDRESSES_FILE)
            links = _read_list(ACCOUNT_MAILBOX_LINKS_FILE)

        account_by_id = {str(item.get("id") or ""): item for item in accounts}
        links_by_address: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            links_by_address.setdefault(str(link.get("mailbox_address_id") or ""), []).append(link)

        resources: list[dict[str, Any]] = []
        def _status(account: dict[str, Any] | None, address: dict[str, Any], link: dict[str, Any] | None) -> str:
            account_status = _normalize_status((account or {}).get("status"))
            address_status = _normalize_status(address.get("status"))
            if account_status in {"disabled", "inactive"} or address_status in {"disabled", "inactive"}:
                return "disabled"
            if link:
                return "registered"
            if address.get("reserved"):
                return "allocated"
            return "available"

        for address in addresses:
            account_id = str(address.get("mailbox_account_id") or "")
            account = account_by_id.get(account_id) or {}
            address_links = links_by_address.get(str(address.get("id") or ""), [])
            link = next((item for item in address_links if item.get("status", "active") == "active"), None)
            resources.append(
                {
                    "id": str(address.get("id") or ""),
                    "resource_kind": "address",
                    "mailbox_account_id": account_id,
                    "mailbox_address_id": str(address.get("id") or ""),
                    "address": address.get("address") or account.get("email") or "",
                    "address_type": address.get("address_type") or "primary",
                    "provider": account.get("provider") or "",
                    "parent_email": account.get("email") or "",
                    "login_account": account.get("login_account") or account.get("email") or "",
                    "status": _status(account, address, link),
                    "mailbox_status": account.get("status") or "active",
                    "reserved": bool(address.get("reserved")),
                    "reserved_for": dict(address.get("reserved_for") or {}),
                    "usage": dict(account.get("usage") or {}),
                    "metadata": {
                        "account": dict(account.get("metadata") or {}),
                        "address": dict(address.get("metadata") or {}),
                    },
                    "chatgpt_account_id": int(link.get("account_id") or 0) if link else None,
                    "chatgpt_account_email": link.get("account_email") if link else "",
                    "link_id": link.get("id") if link else "",
                    "link_status": link.get("status") if link else "",
                    "updated_at": address.get("updated_at") or account.get("updated_at") or "",
                    "created_at": address.get("created_at") or account.get("created_at") or "",
                }
            )

        order = {"registered": 0, "allocated": 1, "available": 2, "disabled": 3}
        resources.sort(key=lambda item: (order.get(str(item.get("status") or ""), 9), str(item.get("updated_at") or ""), str(item.get("address") or "")))
        canonical_addresses = {
            (
                str(item.get("provider") or "").strip().lower(),
                str(item.get("address") or "").strip().lower(),
            )
            for item in canonical
        }
        return canonical + [
            item
            for item in resources
            if (
                str(item.get("provider") or "").strip().lower(),
                str(item.get("address") or "").strip().lower(),
            )
            not in canonical_addresses
        ]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        account_id = _text(account_id)
        return next((item for item in self.list_accounts() if item.get("id") == account_id), None)

    def get_address(self, address_id: str) -> dict[str, Any] | None:
        address_id = _text(address_id)
        return next((item for item in self.list_addresses() if item.get("id") == address_id), None)

    def create_account(self, data: dict[str, Any]) -> dict[str, Any]:
        provider = _text(data.get("provider") or "local_ms_pool")
        email = _text(data.get("email"))
        login_account = _text(data.get("login_account") or email)
        if not email:
            raise ValueError("邮箱账号缺少 email")
        now = _now()
        item = {
            "id": _new_id("mbx"),
            "provider": provider,
            "email": email,
            "login_account": login_account,
            "status": _normalize_status(data.get("status")),
            "credentials": dict(data.get("credentials") or {}),
            "capabilities": _normalize_capabilities(provider, data.get("capabilities") or {}),
            "usage": dict(data.get("usage") or {}),
            "metadata": dict(data.get("metadata") or {}),
            "created_at": now,
            "updated_at": now,
        }
        item["credentials"].setdefault("email", email)
        if login_account:
            item["credentials"].setdefault("login_account", login_account)
        if provider == "local_ms_pool":
            item["usage"].setdefault("capacity", int(item["capabilities"].get("max_alias_count") or 6))
            item["usage"].setdefault("used_count", 0)
        with _LOCK:
            items = _read_list(MAILBOX_ACCOUNTS_FILE)
            items.append(item)
            _write_list(MAILBOX_ACCOUNTS_FILE, items)
        return item

    def update_account(self, account_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        account_id = _text(account_id)
        with _LOCK:
            items = _read_list(MAILBOX_ACCOUNTS_FILE)
            for index, item in enumerate(items):
                if item.get("id") != account_id:
                    continue
                next_item = dict(item)
                for key in ("provider", "email", "login_account", "status"):
                    if key in data:
                        next_item[key] = _text(data.get(key)) if key != "status" else _normalize_status(data.get(key))
                for key in ("credentials", "capabilities", "usage", "metadata"):
                    if key in data and isinstance(data.get(key), dict):
                        next_item[key] = dict(data[key])
                next_item["updated_at"] = _now()
                items[index] = next_item
                _write_list(MAILBOX_ACCOUNTS_FILE, items)
                return next_item
        return None

    def delete_account(self, account_id: str) -> bool:
        account_id = _text(account_id)
        from core.mailbox_lifecycle import MailboxAllocationLifecycle

        canonical_id = MailboxAllocationLifecycle.parse_public_resource_id(account_id)
        if canonical_id is not None:
            return MailboxAllocationLifecycle().archive_resource(canonical_id)
        with _LOCK:
            accounts = _read_list(MAILBOX_ACCOUNTS_FILE)
            addresses = _read_list(MAILBOX_ADDRESSES_FILE)
            links = _read_list(ACCOUNT_MAILBOX_LINKS_FILE)
            next_accounts = [item for item in accounts if item.get("id") != account_id]
            if len(next_accounts) == len(accounts):
                return False
            address_ids = {item.get("id") for item in addresses if item.get("mailbox_account_id") == account_id}
            _write_list(MAILBOX_ACCOUNTS_FILE, next_accounts)
            _write_list(MAILBOX_ADDRESSES_FILE, [item for item in addresses if item.get("mailbox_account_id") != account_id])
            _write_list(ACCOUNT_MAILBOX_LINKS_FILE, [item for item in links if item.get("mailbox_account_id") != account_id and item.get("mailbox_address_id") not in address_ids])
        return True

    def upsert_address(
        self,
        *,
        mailbox_account_id: str,
        address: str,
        address_type: str = "primary",
        status: str = "active",
        reserved: bool = False,
        reserved_for: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mailbox_account_id = _text(mailbox_account_id)
        address = _text(address)
        if not mailbox_account_id or not address:
            raise ValueError("邮箱地址缺少 mailbox_account_id 或 address")
        now = _now()
        with _LOCK:
            items = _read_list(MAILBOX_ADDRESSES_FILE)
            for index, item in enumerate(items):
                if item.get("mailbox_account_id") == mailbox_account_id and _text(item.get("address")).lower() == address.lower():
                    next_item = dict(item)
                    next_item.update(
                        {
                            "address_type": _text(address_type) or next_item.get("address_type") or "primary",
                            "status": _normalize_status(status, next_item.get("status") or "active"),
                            "reserved": bool(reserved),
                            "reserved_for": dict(reserved_for or {}),
                            "metadata": dict(metadata or next_item.get("metadata") or {}),
                            "updated_at": now,
                        }
                    )
                    items[index] = next_item
                    _write_list(MAILBOX_ADDRESSES_FILE, items)
                    return next_item
            item = {
                "id": _new_id("addr"),
                "mailbox_account_id": mailbox_account_id,
                "address": address,
                "address_type": _text(address_type) or "primary",
                "status": _normalize_status(status),
                "reserved": bool(reserved),
                "reserved_for": dict(reserved_for or {}),
                "last_otp_at": None,
                "metadata": dict(metadata or {}),
                "created_at": now,
                "updated_at": now,
            }
            items.append(item)
            _write_list(MAILBOX_ADDRESSES_FILE, items)
            return item

    def reserve_address(self, data: dict[str, Any]) -> dict[str, Any]:
        mailbox_account_id = _text(data.get("mailbox_account_id"))
        account = self.get_account(mailbox_account_id)
        if not account:
            raise ValueError("邮箱账号不存在")
        address = _text(data.get("address"))
        address_type = _text(data.get("address_type") or "primary")
        metadata = dict(data.get("metadata") or {})
        if not address:
            alias_index = int(data.get("alias_index") or metadata.get("alias_index") or 0)
            if alias_index > 0:
                local, sep, domain = _text(account.get("email")).rpartition("@")
                if not sep:
                    raise ValueError("邮箱格式无效，无法生成别名")
                address = f"{local.split('+', 1)[0]}+reg{alias_index}@{domain}"
                address_type = "alias"
                metadata["alias_index"] = alias_index
            else:
                address = _text(account.get("email"))
        reserved_for = dict(data.get("reserved_for") or {})
        item = self.upsert_address(
            mailbox_account_id=mailbox_account_id,
            address=address,
            address_type=address_type,
            reserved=bool(data.get("reserved", True)),
            reserved_for=reserved_for,
            metadata=metadata,
        )
        self._refresh_usage(mailbox_account_id)
        return item

    def release_address(self, address_id: str) -> dict[str, Any] | None:
        address_id = _text(address_id)
        from core.mailbox_lifecycle import MailboxAllocationLifecycle

        canonical_id = MailboxAllocationLifecycle.parse_public_resource_id(address_id)
        if canonical_id is not None:
            MailboxAllocationLifecycle().release_resource(canonical_id)
            return next(
                (item for item in MailboxAllocationLifecycle().list_resources() if item.get("id") == address_id),
                None,
            )
        mailbox_account_id = ""
        updated: dict[str, Any] | None = None
        with _LOCK:
            items = _read_list(MAILBOX_ADDRESSES_FILE)
            for index, item in enumerate(items):
                if item.get("id") != address_id:
                    continue
                mailbox_account_id = _text(item.get("mailbox_account_id"))
                next_item = dict(item)
                next_item["reserved"] = False
                next_item["reserved_for"] = {}
                next_item["updated_at"] = _now()
                items[index] = next_item
                _write_list(MAILBOX_ADDRESSES_FILE, items)
                links = _read_list(ACCOUNT_MAILBOX_LINKS_FILE)
                _write_list(
                    ACCOUNT_MAILBOX_LINKS_FILE,
                    [link for link in links if link.get("mailbox_address_id") != address_id],
                )
                updated = next_item
                break
        if updated and mailbox_account_id:
            self._refresh_usage(mailbox_account_id)
        return updated

    def link_account(
        self,
        *,
        platform: str,
        account_id: int,
        account_email: str,
        mailbox_address_id: str,
        purpose: str = "verification",
    ) -> dict[str, Any]:
        address = self.get_address(mailbox_address_id)
        if not address:
            raise ValueError("邮箱地址不存在")
        mailbox_account_id = _text(address.get("mailbox_account_id"))
        now = _now()
        link = {
            "id": _new_id("link"),
            "platform": _text(platform) or "chatgpt",
            "account_id": int(account_id),
            "account_email": _text(account_email),
            "mailbox_address_id": _text(mailbox_address_id),
            "mailbox_account_id": mailbox_account_id,
            "purpose": _text(purpose) or "verification",
            "status": "active",
            "created_at": now,
        }
        with _LOCK:
            links = [
                item
                for item in _read_list(ACCOUNT_MAILBOX_LINKS_FILE)
                if not (
                    item.get("platform") == link["platform"]
                    and int(item.get("account_id") or 0) == link["account_id"]
                    and item.get("purpose") == link["purpose"]
                )
            ]
            links.append(link)
            _write_list(ACCOUNT_MAILBOX_LINKS_FILE, links)
            addresses = _read_list(MAILBOX_ADDRESSES_FILE)
            for index, item in enumerate(addresses):
                if item.get("id") == mailbox_address_id:
                    next_item = dict(item)
                    next_item["reserved"] = True
                    next_item["reserved_for"] = {
                        "platform": link["platform"],
                        "account_id": link["account_id"],
                        "email": link["account_email"],
                    }
                    next_item["updated_at"] = now
                    addresses[index] = next_item
                    break
            _write_list(MAILBOX_ADDRESSES_FILE, addresses)
        self._refresh_usage(mailbox_account_id)
        return link

    def unlink_account(self, *, platform: str, account_id: int, purpose: str = "verification") -> bool:
        platform = _text(platform) or "chatgpt"
        purpose = _text(purpose) or "verification"
        with _LOCK:
            links = _read_list(ACCOUNT_MAILBOX_LINKS_FILE)
            removed = [
                item
                for item in links
                if item.get("platform") == platform and int(item.get("account_id") or 0) == int(account_id) and item.get("purpose") == purpose
            ]
            if not removed:
                return False
            removed_address_ids = {item.get("mailbox_address_id") for item in removed}
            _write_list(ACCOUNT_MAILBOX_LINKS_FILE, [item for item in links if item not in removed])
            addresses = _read_list(MAILBOX_ADDRESSES_FILE)
            touched_accounts = set()
            for index, item in enumerate(addresses):
                if item.get("id") in removed_address_ids:
                    next_item = dict(item)
                    next_item["reserved"] = False
                    next_item["reserved_for"] = {}
                    next_item["updated_at"] = _now()
                    addresses[index] = next_item
                    touched_accounts.add(_text(item.get("mailbox_account_id")))
            _write_list(MAILBOX_ADDRESSES_FILE, addresses)
        for mailbox_account_id in touched_accounts:
            self._refresh_usage(mailbox_account_id)
        return True

    def get_link_for_account(self, *, platform: str, account_id: int, purpose: str = "verification") -> dict[str, Any] | None:
        platform = _text(platform) or "chatgpt"
        purpose = _text(purpose) or "verification"
        return next(
            (
                item
                for item in self.list_links()
                if item.get("platform") == platform and int(item.get("account_id") or 0) == int(account_id) and item.get("purpose") == purpose
            ),
            None,
        )

    def resolve_mailbox_for_account(
        self,
        *,
        platform: str,
        account_id: int,
        purpose: str = "verification",
        proxy: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[Any, MailboxAccount, dict[str, Any]]:
        from core.mailbox_lifecycle import MailboxAllocationLifecycle

        canonical = MailboxAllocationLifecycle().get_resource_for_account(account_id)
        if canonical is not None:
            provider_account_model = MailboxAllocationLifecycle().get_provider_account_for_resource(int(canonical.id or 0))
            if provider_account_model is None:
                raise RuntimeError("邮箱 provider 登录账号不存在")
            provider_account = {
                "provider_type": "mailbox",
                "provider_name": provider_account_model.provider_name,
                "login_identifier": provider_account_model.login_identifier,
                "display_name": provider_account_model.display_name,
                "credentials": provider_account_model.get_credentials(),
                "metadata": provider_account_model.get_metadata(),
            }
            credentials = dict(provider_account.get("credentials") or {})
            mailbox_account = MailboxAccount(
                email=canonical.address,
                account_id=canonical.resource_identifier,
                extra={
                    "provider_account": provider_account,
                    "provider_resource": canonical.get_provider_resource(),
                },
            )
            mailbox = create_mailbox(
                canonical.provider_name,
                {**dict(extra or {}), **credentials},
                proxy=proxy,
            )
            public_id = MailboxAllocationLifecycle.public_resource_id(int(canonical.id or 0))
            return mailbox, mailbox_account, {
                "link": {
                    "platform": platform,
                    "account_id": int(account_id),
                    "mailbox_address_id": public_id,
                    "purpose": purpose,
                },
                "address": {"id": public_id, "address": canonical.address},
                "account": {"id": public_id, "provider": canonical.provider_name},
            }
        link = self.get_link_for_account(platform=platform, account_id=account_id, purpose=purpose)
        if not link:
            raise RuntimeError("该账号未绑定验证邮箱")
        address = self.get_address(str(link.get("mailbox_address_id") or ""))
        if not address:
            raise RuntimeError("账号绑定的邮箱地址不存在")
        account = self.get_account(str(link.get("mailbox_account_id") or ""))
        if not account:
            raise RuntimeError("账号绑定的邮箱账号不存在")
        provider = _text(account.get("provider"))
        credentials = dict(account.get("credentials") or {})
        credentials.setdefault("email", account.get("email") or address.get("address") or "")
        credentials.setdefault("login_account", account.get("login_account") or account.get("email") or "")
        mailbox_account = MailboxAccount(
            email=_text(address.get("address")),
            account_id=_text(address.get("id")),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": provider,
                    "login_identifier": account.get("login_account") or account.get("email") or "",
                    "display_name": address.get("address") or account.get("email") or "",
                    "credentials": credentials,
                    "metadata": {
                        **dict(account.get("metadata") or {}),
                        **dict(address.get("metadata") or {}),
                        "parent_email": account.get("email") or "",
                    },
                }
            },
        )
        runtime_extra = {**dict(extra or {}), **credentials}
        mailbox = create_mailbox(provider, runtime_extra, proxy=proxy)
        return mailbox, mailbox_account, {"link": link, "address": address, "account": account}

    def resolve_mailbox_for_address(
        self,
        *,
        mailbox_address_id: str,
        proxy: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[Any, MailboxAccount, dict[str, Any]]:
        from core.mailbox_lifecycle import MailboxAllocationLifecycle

        canonical_id = MailboxAllocationLifecycle.parse_public_resource_id(str(mailbox_address_id or ""))
        if canonical_id is not None:
            lifecycle = MailboxAllocationLifecycle()
            resource = lifecycle.get_resource(canonical_id)
            if resource is None:
                raise RuntimeError("邮箱资源不存在")
            provider_account_model = lifecycle.get_provider_account_for_resource(canonical_id)
            if provider_account_model is None:
                raise RuntimeError("邮箱 provider 登录账号不存在")
            provider_account = {
                "provider_type": "mailbox",
                "provider_name": provider_account_model.provider_name,
                "login_identifier": provider_account_model.login_identifier,
                "display_name": provider_account_model.display_name,
                "credentials": provider_account_model.get_credentials(),
                "metadata": provider_account_model.get_metadata(),
            }
            credentials = dict(provider_account.get("credentials") or {})
            mailbox_account = MailboxAccount(
                email=resource.address,
                account_id=resource.resource_identifier,
                extra={
                    "provider_account": provider_account,
                    "provider_resource": resource.get_provider_resource(),
                },
            )
            runtime_extra = {**dict(extra or {}), **credentials}
            mailbox = create_mailbox(resource.provider_name, runtime_extra, proxy=proxy)
            return mailbox, mailbox_account, {
                "address": {"id": mailbox_address_id, "address": resource.address},
                "account": {"id": mailbox_address_id, "provider": resource.provider_name},
            }
        address = self.get_address(str(mailbox_address_id or ""))
        if not address:
            raise RuntimeError("邮箱地址不存在")
        account = self.get_account(str(address.get("mailbox_account_id") or ""))
        if not account:
            raise RuntimeError("邮箱地址所属账号不存在")
        provider = _text(account.get("provider"))
        credentials = dict(account.get("credentials") or {})
        credentials.setdefault("email", account.get("email") or address.get("address") or "")
        credentials.setdefault("login_account", account.get("login_account") or account.get("email") or "")
        mailbox_account = MailboxAccount(
            email=_text(address.get("address")),
            account_id=_text(address.get("id")),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": provider,
                    "login_identifier": account.get("login_account") or account.get("email") or "",
                    "display_name": address.get("address") or account.get("email") or "",
                    "credentials": credentials,
                    "metadata": {
                        **dict(account.get("metadata") or {}),
                        **dict(address.get("metadata") or {}),
                        "parent_email": account.get("email") or "",
                    },
                }
            },
        )
        runtime_extra = {**dict(extra or {}), **credentials}
        mailbox = create_mailbox(provider, runtime_extra, proxy=proxy)
        return mailbox, mailbox_account, {"address": address, "account": account}

    def list_messages_for_address(
        self,
        *,
        mailbox_address_id: str,
        limit: int = 10,
        proxy: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        mailbox, mailbox_account, _context = self.resolve_mailbox_for_address(
            mailbox_address_id=mailbox_address_id,
            proxy=proxy,
            extra=extra,
        )
        if not hasattr(mailbox, "list_messages"):
            raise RuntimeError(f"{mailbox.__class__.__name__} 暂不支持获取邮件")
        return list(mailbox.list_messages(mailbox_account, limit=limit) or [])

    def _extract_mailbox_material(
        self,
        *,
        extra: dict[str, Any] | None = None,
        fallback_email: str = "",
        fallback_provider: str = "",
    ) -> dict[str, Any]:
        extra = dict(extra or {})
        identity = dict(extra.get("identity") or {})
        mailbox_snapshot = dict(identity.get("mailbox") or extra.get("verification_mailbox") or {})
        provider_account = dict(identity.get("provider_account") or extra.get("provider_account") or {})
        provider_resource = dict(identity.get("provider_resource") or extra.get("provider_resource") or {})
        if not provider_account:
            for item in extra.get("provider_accounts") or []:
                if isinstance(item, dict) and item.get("provider_type") == "mailbox":
                    provider_account = dict(item)
                    break
        if not provider_resource:
            for item in extra.get("provider_resources") or []:
                if isinstance(item, dict) and item.get("resource_type") == "mailbox":
                    provider_resource = dict(item)
                    break
        provider = _text(
            provider_account.get("provider_name")
            or provider_resource.get("provider_name")
            or mailbox_snapshot.get("provider")
            or fallback_provider
        )
        address = _text(mailbox_snapshot.get("email") or provider_resource.get("handle") or fallback_email)
        credentials = dict(provider_account.get("credentials") or {})
        parent_email = _text(
            credentials.get("email")
            or (provider_account.get("metadata") or {}).get("parent_email")
            or (provider_resource.get("metadata") or {}).get("parent_email")
            or address
        )
        return {
            "provider": provider,
            "address": address,
            "credentials": credentials,
            "parent_email": parent_email,
            "provider_account": provider_account,
            "provider_resource": provider_resource,
        }

    def _upsert_mailbox_resource(
        self,
        *,
        provider: str,
        address: str,
        credentials: dict[str, Any] | None = None,
        parent_email: str = "",
        provider_account: dict[str, Any] | None = None,
        provider_resource: dict[str, Any] | None = None,
        reserved_for: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        provider = _text(provider)
        address = _text(address)
        if not provider or not address:
            return None
        credentials = dict(credentials or {})
        parent_email = _text(parent_email or credentials.get("email") or address)
        provider_account = dict(provider_account or {})
        provider_resource = dict(provider_resource or {})
        with _LOCK:
            accounts = _read_list(MAILBOX_ACCOUNTS_FILE)
            mailbox_account = next(
                (
                    item
                    for item in accounts
                    if item.get("provider") == provider and _parent_address(item.get("email", "")) == _parent_address(parent_email)
                ),
                None,
            )
            if mailbox_account is None:
                mailbox_account = self.create_account(
                    {
                        "provider": provider,
                        "email": parent_email,
                        "login_account": credentials.get("login_account") or parent_email,
                        "credentials": credentials,
                        "metadata": dict(provider_account.get("metadata") or {}),
                    }
                )
            else:
                merged = dict(mailbox_account.get("credentials") or {})
                merged.update({key: value for key, value in credentials.items() if value not in (None, "")})
                self.update_account(str(mailbox_account["id"]), {"credentials": merged})
                mailbox_account = self.get_account(str(mailbox_account["id"])) or mailbox_account
        address_item = self.upsert_address(
            mailbox_account_id=str(mailbox_account["id"]),
            address=address,
            address_type="alias" if _parent_address(address) == _parent_address(parent_email) and address.lower() != parent_email.lower() else "primary",
            reserved=True,
            reserved_for=dict(reserved_for or {}),
            metadata=dict(provider_resource.get("metadata") or {}),
        )
        self._refresh_usage(str(mailbox_account["id"]))
        return {"account": mailbox_account, "address": address_item}

    def record_allocated_mailbox(
        self,
        *,
        platform: str,
        mailbox_account: Any,
        provider: str = "",
        reserved_for: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if mailbox_account is None:
            return None
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {})
        material = self._extract_mailbox_material(
            extra={
                **mailbox_extra,
                "identity": {
                    "mailbox": {
                        "provider": provider,
                        "email": getattr(mailbox_account, "email", "") or "",
                        "account_id": str(getattr(mailbox_account, "account_id", "") or ""),
                    },
                    "provider_account": mailbox_extra.get("provider_account"),
                    "provider_resource": mailbox_extra.get("provider_resource"),
                },
            },
            fallback_email=getattr(mailbox_account, "email", "") or "",
            fallback_provider=provider,
        )
        allocation_reserved_for = {"platform": _text(platform) or "chatgpt", "status": "allocated"}
        allocation_reserved_for.update(dict(reserved_for or {}))
        return self._upsert_mailbox_resource(
            provider=material["provider"],
            address=material["address"],
            credentials=material["credentials"],
            parent_email=material["parent_email"],
            provider_account=material["provider_account"],
            provider_resource=material["provider_resource"],
            reserved_for=allocation_reserved_for,
        )

    def record_registration_link(self, *, account_id: int, platform_account: Any) -> dict[str, Any] | None:
        material = self._extract_mailbox_material(
            extra=dict(getattr(platform_account, "extra", {}) or {}),
            fallback_email=getattr(platform_account, "email", ""),
        )
        resource = self._upsert_mailbox_resource(
            provider=material["provider"],
            address=material["address"],
            credentials=material["credentials"],
            parent_email=material["parent_email"],
            provider_account=material["provider_account"],
            provider_resource=material["provider_resource"],
            reserved_for={
                "platform": getattr(platform_account, "platform", "chatgpt"),
                "account_id": int(account_id),
                "email": getattr(platform_account, "email", "") or material["address"],
            },
        )
        if not resource:
            return None
        return self.link_account(
            platform=getattr(platform_account, "platform", "chatgpt") or "chatgpt",
            account_id=int(account_id),
            account_email=getattr(platform_account, "email", "") or material["address"],
            mailbox_address_id=str(resource["address"]["id"]),
            purpose="verification",
        )

    def _refresh_usage(self, mailbox_account_id: str) -> None:
        mailbox_account_id = _text(mailbox_account_id)
        if not mailbox_account_id:
            return
        with _LOCK:
            accounts = _read_list(MAILBOX_ACCOUNTS_FILE)
            addresses = _read_list(MAILBOX_ADDRESSES_FILE)
            used = sum(1 for item in addresses if item.get("mailbox_account_id") == mailbox_account_id and item.get("reserved"))
            for index, item in enumerate(accounts):
                if item.get("id") != mailbox_account_id:
                    continue
                next_item = deepcopy(item)
                usage = dict(next_item.get("usage") or {})
                usage["used_count"] = used
                if "capacity" not in usage:
                    usage["capacity"] = int((next_item.get("capabilities") or {}).get("max_alias_count") or max(used, 1))
                next_item["usage"] = usage
                next_item["updated_at"] = _now()
                accounts[index] = next_item
                _write_list(MAILBOX_ACCOUNTS_FILE, accounts)
                return
