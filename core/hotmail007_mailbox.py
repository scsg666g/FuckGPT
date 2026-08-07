"""Hotmail007 mailbox provider.

Core scope only:

* buy mailbox accounts from Hotmail007's new ``/open/buy`` API;
* fetch latest mail from ``/open/mail/latest`` for OTP/link extraction.

The buy loop intentionally has no artificial sleep. Hotmail007 stock can be
short-lived, so retries are bounded by max attempts / wall-clock timeout rather
than delayed polling.
"""
from __future__ import annotations

import hashlib
import html
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from core.api_mailbox import ApiMailboxPool, DEFAULT_CODE_PATTERN
from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link


DEFAULT_BASE_URL = "https://hotmail007.com/api"
DEFAULT_FOLDERS = ("inbox", "junkemail")
PERMANENT_MAILBOX_ERROR_MARKERS = (
    "code=27006",
    "mail authentication failed",
)


class Hotmail007BuyError(RuntimeError):
    def __init__(self, product_id: str, message: str):
        super().__init__(message)
        self.product_id = product_id


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _int_value(value: object, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        result = default
    result = max(result, minimum)
    if maximum is not None:
        result = min(result, maximum)
    return result


def _float_value(value: object, default: float, *, minimum: float = 0.0) -> float:
    try:
        result = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        result = default
    return max(result, minimum)


def _split_folders(value: object) -> tuple[str, ...]:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_FOLDERS
    folders: list[str] = []
    for item in re.split(r"[,，\s]+", raw):
        folder = item.strip().lower()
        if folder in {"inbox", "junkemail"} and folder not in folders:
            folders.append(folder)
    return tuple(folders or DEFAULT_FOLDERS)


def _split_product_ids(value: object) -> tuple[str, ...]:
    product_ids: list[str] = []
    for item in re.split(r"[,，\s]+", str(value or "").strip()):
        product_id = item.strip()
        if product_id and product_id not in product_ids:
            product_ids.append(product_id)
    return tuple(product_ids)


def _is_permanent_mailbox_error(error: object) -> bool:
    value = str(error or "").strip().lower()
    return bool(value) and any(marker in value for marker in PERMANENT_MAILBOX_ERROR_MARKERS)


@dataclass(frozen=True)
class Hotmail007AccountEntry:
    email: str
    password: str
    refresh_token: str
    client_id: str
    raw: str = ""

    @property
    def key(self) -> str:
        return self.email.strip().lower()

    @property
    def api_account(self) -> str:
        return f"{self.email}:{self.password}:{self.refresh_token}:{self.client_id}"

    def credentials(self) -> dict[str, str]:
        return {
            "email": self.email,
            "password": self.password,
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "api_account": self.api_account,
        }


def parse_hotmail007_account(value: object) -> Hotmail007AccountEntry:
    """Parse Hotmail007 account credential strings.

    The documented mail API expects ``email:password:refreshToken:clientId``.
    Purchased rows are still treated defensively because vendors often expose
    the same material with ``----`` or pipe delimiters in dashboards.
    """

    raw = str(value or "").strip().strip("\ufeff")
    if not raw:
        raise ValueError("Hotmail007 返回了空账号")

    separators = ("----", "|", "\t", ",", "，")
    parts: list[str]
    if "----" in raw:
        parts = [item.strip() for item in raw.split("----")]
    else:
        parts = []
        for sep in separators[1:]:
            if sep in raw:
                parts = [item.strip() for item in raw.split(sep)]
                break
        if not parts:
            # Split on the first three colons so tokens containing ':' after
            # the fourth field do not destroy the email/password positions.
            parts = [item.strip() for item in raw.split(":", 3)]

    if len(parts) < 4 or "@" not in parts[0]:
        raise ValueError("Hotmail007 账号格式无效，应为 email:password:refreshToken:clientId")

    return Hotmail007AccountEntry(
        email=parts[0],
        password=parts[1],
        refresh_token=parts[2],
        client_id=parts[3],
        raw=raw,
    )


class Hotmail007Mailbox(BaseMailbox):
    """Mailbox provider backed by Hotmail007 purchase + latest-mail APIs."""

    def __init__(
        self,
        *,
        client_key: str = "",
        product_id: str | int = "",
        base_url: str = DEFAULT_BASE_URL,
        buy_quantity: int | str = 1,
        buy_max_attempts: int | str = 200,
        request_timeout: float | str = 8,
        folders: str | tuple[str, ...] = DEFAULT_FOLDERS,
        include_junk: bool | str = True,
        proxy: str | None = None,
        session: requests.Session | None = None,
        log_fn=None,
    ):
        self.client_key = str(client_key or "").strip()
        self.product_ids = _split_product_ids(product_id)
        self.product_id = self.product_ids[0] if self.product_ids else ""
        self.base_url = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/") + "/"
        self.buy_quantity = _int_value(buy_quantity, 1, minimum=1, maximum=50)
        self.buy_max_attempts = _int_value(buy_max_attempts, 200, minimum=1)
        self.request_timeout = _float_value(request_timeout, 8, minimum=0.5)
        resolved_folders = _split_folders(folders)
        if not _truthy(include_junk):
            resolved_folders = tuple(folder for folder in resolved_folders if folder != "junkemail") or ("inbox",)
        self.folders = resolved_folders
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()
        self._cache: list[Hotmail007AccountEntry] = []
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._log_fn = log_fn if callable(log_fn) else None
        self._prefetch_enabled = False
        self._prefetch_total = 0
        self._prefetch_reserved = 0
        self._prefetch_delivered = 0
        self._prefetch_queue_max = 0
        self._prefetch_stop = False
        self._prefetch_active = 0
        self._prefetch_errors: list[str] = []
        self._prefetch_threads: list[threading.Thread] = []

    @classmethod
    def from_config(cls, config: dict) -> "Hotmail007Mailbox":
        return cls(
            client_key=config.get("hotmail007_client_key", ""),
            product_id=config.get("hotmail007_product_id", ""),
            base_url=config.get("hotmail007_base_url", DEFAULT_BASE_URL),
            buy_quantity=config.get("hotmail007_buy_quantity", 1),
            buy_max_attempts=config.get("hotmail007_buy_max_attempts", 200),
            request_timeout=config.get("hotmail007_request_timeout", 8),
            folders=config.get("hotmail007_folders", ",".join(DEFAULT_FOLDERS)),
            include_junk=config.get("hotmail007_include_junk", "true"),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
            log_fn=config.get("_log_fn"),
        )

    def set_logger(self, log_fn) -> None:
        self._log_fn = log_fn if callable(log_fn) else None

    def configure_prefetch(
        self,
        *,
        total_needed: int,
        buy_concurrency: int = 1,
        queue_max: int = 1,
    ) -> None:
        total = _int_value(total_needed, 0, minimum=0)
        if total <= 1:
            return
        concurrency = _int_value(buy_concurrency, 1, minimum=1, maximum=20)
        queue_limit = _int_value(queue_max, concurrency, minimum=1, maximum=100)
        worker_count = min(concurrency, total)
        with self._condition:
            if self._prefetch_enabled:
                return
            self._prefetch_enabled = True
            self._prefetch_total = total
            self._prefetch_reserved = 0
            self._prefetch_delivered = 0
            self._prefetch_queue_max = queue_limit
            self._prefetch_stop = False
            self._prefetch_active = 0
            self._prefetch_errors = []
            self._prefetch_threads = []
            self._log(
                f"Hotmail007 预取已启用：需求 {total} 个，买号并发 {worker_count}，队列上限 {queue_limit}"
            )
            for index in range(worker_count):
                thread = threading.Thread(
                    target=self._prefetch_worker,
                    name=f"hotmail007-prefetch-{index + 1}",
                    daemon=True,
                )
                self._prefetch_threads.append(thread)
                thread.start()

    def shutdown_prefetch(self) -> None:
        with self._condition:
            self._prefetch_stop = True
            self._condition.notify_all()
            threads = list(self._prefetch_threads)
        for thread in threads:
            thread.join(timeout=2)

    def _log(self, message: str) -> None:
        if not self._log_fn:
            return
        try:
            self._log_fn(message)
        except Exception:
            pass

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(
            self._url(path),
            params=params,
            headers={"Accept": "application/json", "User-Agent": "FuckGPT/hotmail007"},
            proxies=self.proxy,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = response.json() or {}
        if not isinstance(payload, dict):
            raise RuntimeError("Hotmail007 返回的内容不是 JSON 对象")
        return payload

    @staticmethod
    def _ensure_success(payload: dict[str, Any], *, action: str) -> Any:
        try:
            code = int(payload.get("code") or 0)
        except (TypeError, ValueError):
            code = payload.get("code")
        if bool(payload.get("success")) and code == 0:
            return payload.get("data")
        message = str(payload.get("message") or "").strip()
        raise RuntimeError(f"Hotmail007 {action}失败: code={code}, message={message or 'unknown'}")

    def _buy_once(self, *, quantity: int | None = None) -> tuple[str, list[Hotmail007AccountEntry]]:
        if not self.client_key:
            raise RuntimeError("Hotmail007 clientKey 未配置")
        if not self.product_ids:
            raise RuntimeError("Hotmail007 productId 未配置")
        product_id = random.choice(self.product_ids)
        buy_quantity = _int_value(quantity, self.buy_quantity, minimum=1, maximum=50)

        try:
            payload = self._get_json(
                "/open/buy",
                {
                    "clientKey": self.client_key,
                    "productId": product_id,
                    "quantity": buy_quantity,
                },
            )
            data = self._ensure_success(payload, action="购买")
            accounts = []
            if isinstance(data, dict):
                accounts = list(data.get("accounts") or [])
            elif isinstance(data, list):
                accounts = list(data)
            if not accounts:
                raise RuntimeError("Hotmail007 购买成功但未返回账号")
            return product_id, [parse_hotmail007_account(item) for item in accounts]
        except Exception as exc:
            raise Hotmail007BuyError(product_id, str(exc).strip() or exc.__class__.__name__) from exc

    def _buy_batch_until_success(self, quantity: int | None = None) -> list[Hotmail007AccountEntry]:
        requested_quantity = _int_value(quantity, self.buy_quantity, minimum=1, maximum=50)
        last_error = ""
        attempts_made = 0
        self.raise_if_cancelled()
        self._log(
            "Hotmail007 开始循环购买邮箱"
            f"（productId={','.join(self.product_ids)}, quantity={requested_quantity}, "
            f"max_attempts={self.buy_max_attempts}, request_timeout={self.request_timeout:g}s）"
        )
        for attempt in range(1, self.buy_max_attempts + 1):
            self.raise_if_cancelled()
            attempts_made = attempt
            try:
                product_id, entries = self._buy_once(quantity=requested_quantity)
                if not entries:
                    raise RuntimeError("Hotmail007 购买成功但未返回账号")
                returned = entries[:requested_quantity]
                if len(returned) == 1:
                    self._log(
                        f"Hotmail007 购买成功：第 {attempt} 次尝试，productId={product_id}，获得邮箱 {returned[0].email}"
                    )
                else:
                    self._log(
                        f"Hotmail007 购买成功：第 {attempt} 次尝试，productId={product_id}，获得 {len(returned)} 个邮箱"
                    )
                return returned
            except Exception as exc:  # noqa: BLE001 - stock races are expected
                last_error = str(exc).strip() or exc.__class__.__name__
                product_id = getattr(exc, "product_id", "")
                if attempt <= 3 or attempt % 25 == 0:
                    product_part = f"，productId={product_id}" if product_id else ""
                    self._log(f"Hotmail007 第 {attempt} 次购买未成功{product_part}：{last_error}")
                continue
        self._log(
            f"Hotmail007 循环购买失败：已达到最大尝试次数 {attempts_made} 次，"
            f"最后错误：{last_error or 'none'}"
        )
        raise RuntimeError(
            f"Hotmail007 循环购买失败: attempts={attempts_made}, "
            f"last_error={last_error or 'none'}"
        )

    def _buy_until_success(self) -> Hotmail007AccountEntry:
        entries = self._buy_batch_until_success(self.buy_quantity)
        entry = entries[0]
        with self._condition:
            self._cache.extend(entries[1:])
        self._log(
            f"Hotmail007 获得邮箱 {entry.email}"
            + (f"，缓存剩余 {len(entries) - 1} 个" if len(entries) > 1 else "")
        )
        return entry

    def _prefetch_inflight_locked(self) -> int:
        return max(self._prefetch_reserved - self._prefetch_delivered - len(self._cache), 0)

    def _prefetch_available_slots_locked(self) -> int:
        return max(self._prefetch_queue_max - len(self._cache) - self._prefetch_inflight_locked(), 0)

    def _prefetch_worker(self) -> None:
        while True:
            with self._condition:
                while (
                    not self._prefetch_stop
                    and self._prefetch_queue_max > 0
                    and self._prefetch_available_slots_locked() <= 0
                ):
                    self._condition.wait(timeout=1)
                if self._prefetch_stop:
                    return
                if self.is_cancel_requested():
                    return
                remaining = self._prefetch_total - self._prefetch_reserved
                if remaining <= 0:
                    return
                available_slots = self._prefetch_available_slots_locked()
                if available_slots <= 0:
                    continue
                quantity = min(self.buy_quantity, remaining, available_slots)
                self._prefetch_reserved += quantity
                self._prefetch_active += 1
            try:
                entries = self._buy_batch_until_success(quantity)
                entries = entries[:quantity]
                missing = max(quantity - len(entries), 0)
                with self._condition:
                    if missing:
                        self._prefetch_reserved -= missing
                    self._cache.extend(entries)
                    self._prefetch_active -= 1
                    if entries:
                        self._log(
                            f"Hotmail007 预取入队 {len(entries)} 个，当前队列 {len(self._cache)} 个"
                        )
                    self._condition.notify_all()
            except Exception as exc:  # noqa: BLE001 - surface one failed buy cycle to waiters
                error = str(exc).strip() or exc.__class__.__name__
                with self._condition:
                    self._prefetch_reserved -= quantity
                    self._prefetch_active -= 1
                    self._prefetch_errors.append(error)
                    self._log(f"Hotmail007 预取失败：{error}")
                    self._condition.notify_all()
                return

    def peek_email(self) -> str:
        return "Hotmail007 将在注册开始时循环购买邮箱"

    def get_email(self) -> MailboxAccount:
        with self._condition:
            if self._prefetch_enabled:
                while not self._cache:
                    self.raise_if_cancelled()
                    if self._prefetch_delivered >= self._prefetch_total:
                        raise RuntimeError("Hotmail007 预取邮箱已分配完毕")
                    if self._prefetch_active == 0 and self._prefetch_errors:
                        raise RuntimeError(f"Hotmail007 预取失败: {self._prefetch_errors[-1]}")
                    self._condition.wait(timeout=1)
                entry = self._cache.pop(0)
                self._prefetch_delivered += 1
                self._condition.notify_all()
            else:
                entry = self._cache.pop(0) if self._cache else self._buy_until_success()
        issued_at = int(time.time())
        self._log(f"Hotmail007 邮箱已分配：{entry.email}")
        return MailboxAccount(
            email=entry.email,
            account_id=entry.key,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "hotmail007",
                    "login_identifier": entry.email,
                    "display_name": entry.email,
                    "credentials": entry.credentials(),
                    "metadata": {
                        "source": "hotmail007",
                        "product_id": self.product_id,
                        "issued_at": issued_at,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "hotmail007",
                    "resource_type": "mailbox",
                    "resource_identifier": entry.key,
                    "handle": entry.email,
                    "display_name": entry.email,
                    "metadata": {
                        "email": entry.email,
                        "source": "hotmail007",
                        "product_id": self.product_id,
                        "issued_at": issued_at,
                    },
                },
            },
        )

    def _entry_for_account(self, account: MailboxAccount) -> Hotmail007AccountEntry:
        extra = dict(getattr(account, "extra", {}) or {})
        provider_account = dict(extra.get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        if credentials:
            api_account = str(credentials.get("api_account") or "").strip()
            if api_account:
                return parse_hotmail007_account(api_account)
            raw = ":".join(
                str(credentials.get(key) or "")
                for key in ("email", "password", "refresh_token", "client_id")
            )
            return parse_hotmail007_account(raw)
        raise RuntimeError(f"Hotmail007 未找到账号凭据: {getattr(account, 'email', '')}")

    @staticmethod
    def _mail_text(mail: dict[str, Any]) -> str:
        return " ".join(
            str(value or "")
            for value in (
                mail.get("subject"),
                mail.get("text"),
                html.unescape(str(mail.get("html") or "")),
            )
        )

    @classmethod
    def _mail_signature(cls, mail: dict[str, Any]) -> str:
        material = "|".join(
            str(mail.get(key) or "")
            for key in ("receivedAt", "from", "subject", "text", "html")
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _latest_mail(
        self,
        entry: Hotmail007AccountEntry,
        *,
        folder: str,
        start_timestamp: int | None = None,
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "clientKey": self.client_key,
            "account": entry.api_account,
            "folder": folder,
        }
        if start_timestamp is not None and start_timestamp > 0:
            params["start_timestamp"] = int(start_timestamp)
        payload = self._get_json("/open/mail/latest", params)
        data = self._ensure_success(payload, action="取件")
        return data if isinstance(data, dict) and data else None

    def _latest_mails(
        self,
        entry: Hotmail007AccountEntry,
        *,
        start_timestamp: int | None = None,
        log_attempt: bool = False,
    ) -> list[dict[str, Any]]:
        mails: list[dict[str, Any]] = []
        last_error = ""
        for folder in self.folders:
            try:
                if log_attempt:
                    self._log(f"Hotmail007 正在取件：{entry.email} / {folder}")
                mail = self._latest_mail(entry, folder=folder, start_timestamp=start_timestamp)
                if mail:
                    mail = dict(mail)
                    mail["_folder"] = folder
                    mails.append(mail)
                    subject = str(mail.get("subject") or "").strip()
                    if log_attempt:
                        self._log(
                            f"Hotmail007 取件成功：{entry.email} / {folder}"
                            + (f"，主题：{subject[:80]}" if subject else "")
                        )
            except Exception as exc:  # noqa: BLE001 - try the next folder
                last_error = str(exc).strip() or exc.__class__.__name__
                if log_attempt:
                    self._log(f"Hotmail007 取件失败：{entry.email} / {folder}，{last_error}")
                continue
        if not mails and last_error:
            raise RuntimeError(last_error)
        return mails

    @staticmethod
    def _issued_at(account: MailboxAccount) -> int:
        extra = dict(getattr(account, "extra", {}) or {})
        provider_account = dict(extra.get("provider_account") or {})
        metadata = dict(provider_account.get("metadata") or {})
        return _int_value(metadata.get("issued_at"), 0, minimum=0)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            entry = self._entry_for_account(account)
            return {
                self._mail_signature(mail)
                for mail in self._latest_mails(entry, start_timestamp=self._issued_at(account))
            }
        except Exception:
            return set()

    def list_messages(self, account: MailboxAccount, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 10), 50))
        entry = self._entry_for_account(account)
        messages: list[dict[str, Any]] = []
        for mail in self._latest_mails(entry, start_timestamp=self._issued_at(account), log_attempt=True):
            text = self._mail_text(mail)
            messages.append(
                {
                    "id": self._mail_signature(mail),
                    "subject": mail.get("subject") or "",
                    "from": mail.get("from") or "",
                    "to": [account.email] if account.email else [],
                    "received_at": mail.get("receivedAt") or mail.get("received_at") or "",
                    "preview": text[:1000],
                    "code": ApiMailboxPool._extract_code(
                        {"subject": mail.get("subject"), "text": mail.get("text"), "html": mail.get("html")},
                        text,
                    ),
                    "link": _extract_verification_link(text, ""),
                    "folder": mail.get("_folder") or "",
                    "provider": "hotmail007",
                }
            )
            if len(messages) >= limit:
                break
        return messages

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        entry = self._entry_for_account(account)
        seen = set(before_ids or set())
        deadline = time.monotonic() + max(int(timeout or 0), 1)
        last_error = ""
        start_timestamp = self._issued_at(account)
        attempt = 0
        self._log(f"Hotmail007 开始等待验证码：{account.email}，文件夹={','.join(self.folders)}")
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            attempt += 1
            log_attempt = attempt <= 3 or attempt % 25 == 0
            try:
                for mail in self._latest_mails(entry, start_timestamp=start_timestamp, log_attempt=log_attempt):
                    signature = self._mail_signature(mail)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    text = self._mail_text(mail)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = ApiMailboxPool._extract_code(
                        {"subject": mail.get("subject"), "text": mail.get("text"), "html": mail.get("html")},
                        text,
                        code_pattern=code_pattern or DEFAULT_CODE_PATTERN,
                    )
                    if code:
                        self._log(f"Hotmail007 获取验证码成功：{account.email}")
                        return code
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc).strip() or exc.__class__.__name__
                if log_attempt:
                    self._log(f"Hotmail007 第 {attempt} 次取验证码未成功：{last_error}")
                if _is_permanent_mailbox_error(last_error):
                    self._log(f"Hotmail007 邮箱认证已失效，停止等待验证码：{account.email}")
                    raise RuntimeError(f"Hotmail007 邮箱认证失败: {last_error}") from exc
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(1.0, remaining))
                continue
            if log_attempt:
                self._log(f"Hotmail007 第 {attempt} 次取验证码未发现新验证码")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1.0, remaining))
        self._log(f"Hotmail007 等待验证码超时：{account.email}，最后错误：{last_error or 'none'}")
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 Hotmail007 验证码超时 ({timeout}s){suffix}")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        entry = self._entry_for_account(account)
        seen = set(before_ids or set())
        deadline = time.monotonic() + max(int(timeout or 0), 1)
        last_error = ""
        start_timestamp = self._issued_at(account)
        attempt = 0
        self._log(f"Hotmail007 开始等待验证链接：{account.email}，文件夹={','.join(self.folders)}")
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            attempt += 1
            log_attempt = attempt <= 3 or attempt % 25 == 0
            try:
                for mail in self._latest_mails(entry, start_timestamp=start_timestamp, log_attempt=log_attempt):
                    signature = self._mail_signature(mail)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    link = _extract_verification_link(self._mail_text(mail), keyword)
                    if link:
                        self._log(f"Hotmail007 获取验证链接成功：{account.email}")
                        return link
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc).strip() or exc.__class__.__name__
                if log_attempt:
                    self._log(f"Hotmail007 第 {attempt} 次取验证链接未成功：{last_error}")
                if _is_permanent_mailbox_error(last_error):
                    self._log(f"Hotmail007 邮箱认证已失效，停止等待验证链接：{account.email}")
                    raise RuntimeError(f"Hotmail007 邮箱认证失败: {last_error}") from exc
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(1.0, remaining))
                continue
            if log_attempt:
                self._log(f"Hotmail007 第 {attempt} 次取验证链接未发现新链接")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1.0, remaining))
        self._log(f"Hotmail007 等待验证链接超时：{account.email}，最后错误：{last_error or 'none'}")
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 Hotmail007 验证链接超时 ({timeout}s){suffix}")
