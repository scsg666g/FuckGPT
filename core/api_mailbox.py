"""Mailbox provider backed by per-address verification-code API URLs.

Each configured row has the form ``email----api_url``.  Entries may be supplied
as pasted text or loaded from a local JSON file.  The URL is treated as an
opaque secret because it commonly contains the mailbox password or token in its
query string.  FlySMS and ICSMS pickup links are also supported as
``email---token---pickup_url`` and are translated to their read-only latest
message endpoint.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link


DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".api_mailbox_pool_state.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_POOL_FILE_SIZE = 5 * 1024 * 1024
DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"


@dataclass(frozen=True)
class ApiMailboxEntry:
    email: str
    api_url: str
    token: str = ""
    referer: str = ""
    provider: str = "generic"

    @property
    def key(self) -> str:
        return self.email.strip().lower()


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


_PICKUP_HOST_PROVIDERS = {
    "flysms.xyz": "flysms",
    "www.flysms.xyz": "flysms",
    "flysms.top": "flysms",
    "www.flysms.top": "flysms",
    "icsms.top": "icsms",
    "www.icsms.top": "icsms",
}


def _strip_fragment(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def _pickup_provider(pickup_url: str) -> str:
    parsed = urlparse(pickup_url)
    host = (parsed.hostname or "").lower()
    provider = _PICKUP_HOST_PROVIDERS.get(host)
    if not provider:
        raise ValueError("token 取件格式目前仅支持 flysms.xyz/flysms.top/icsms.top")
    return provider


def _pickup_api_url(pickup_url: str) -> str:
    parsed = urlparse(pickup_url)
    _pickup_provider(pickup_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/pickup"):
        raise ValueError("取件 URL 应为 .../pickup 页面地址")
    base_path = path[: -len("/pickup")]
    api_path = f"{base_path}/api/pickup/messages/latest" if base_path else "/api/pickup/messages/latest"
    return urlunparse((parsed.scheme, parsed.netloc, api_path, "", "", ""))


def parse_api_mailbox_rows(text: str) -> list[ApiMailboxEntry]:
    """Parse one API mailbox entry per line."""

    entries: list[ApiMailboxEntry] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(str(text or "").splitlines(), start=1):
        line = raw_line.strip().strip("\ufeff")
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "----" in line:
            email, _, api_url = line.partition("----")
            email = email.strip()
            api_url = api_url.strip()
            if "@" not in email or not api_url:
                raise ValueError(f"API 邮箱第 {line_number} 行格式错误，应为：邮箱----完整 API URL 或 邮箱---token---flysms/icsms取件URL")
            parsed = urlparse(api_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"API 邮箱第 {line_number} 行 URL 无效，仅支持 http/https")
            entry = ApiMailboxEntry(email=email, api_url=api_url)
        elif "---" in line:
            parts = [part.strip() for part in line.split("---", 2)]
            if len(parts) != 3:
                raise ValueError(f"API 邮箱第 {line_number} 行格式错误，应为：邮箱---token---flysms/icsms取件URL")
            email, token, pickup_url = parts
            if "@" not in email or not token or not pickup_url:
                raise ValueError(f"API 邮箱第 {line_number} 行格式错误，应为：邮箱---token---flysms/icsms取件URL")
            parsed = urlparse(pickup_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"API 邮箱第 {line_number} 行 URL 无效，仅支持 http/https")
            try:
                api_url = _pickup_api_url(pickup_url)
                provider = _pickup_provider(pickup_url)
            except ValueError as exc:
                raise ValueError(f"API 邮箱第 {line_number} 行格式错误：{exc}") from exc
            entry = ApiMailboxEntry(
                email=email,
                api_url=api_url,
                token=token,
                referer=_strip_fragment(pickup_url),
                provider=provider,
            )
        else:
            raise ValueError(f"API 邮箱第 {line_number} 行格式错误，应为：邮箱----完整 API URL 或 邮箱---token---flysms/icsms取件URL")
        if entry.key in seen:
            continue
        seen.add(entry.key)
        entries.append(entry)
    return entries


def _required_json_text(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def parse_api_mailbox_json(payload: object) -> list[ApiMailboxEntry]:
    """Parse API mailbox entries from a JSON-compatible object."""

    if isinstance(payload, dict) and "mailboxes" in payload:
        items = payload.get("mailboxes")
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and "email" in payload:
        items = [payload]
    else:
        raise ValueError('JSON 根节点应为数组或包含 "mailboxes" 数组的对象')

    if not isinstance(items, list):
        raise ValueError('JSON 字段 "mailboxes" 必须是数组')

    entries: list[ApiMailboxEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        try:
            if isinstance(item, str):
                parsed = parse_api_mailbox_rows(item)
                if len(parsed) != 1:
                    raise ValueError("字符串条目必须包含一组邮箱配置")
                entry = parsed[0]
            elif isinstance(item, dict):
                email = _required_json_text(item, ("email", "mailbox"))
                token = _required_json_text(item, ("token", "access_token"))
                pickup_url = _required_json_text(item, ("pickup_url", "pickupUrl"))
                api_url = _required_json_text(item, ("api_url", "apiUrl"))
                fallback_url = _required_json_text(item, ("url",))

                if pickup_url or (token and fallback_url):
                    pickup_url = pickup_url or fallback_url
                    parsed = parse_api_mailbox_rows(f"{email}---{token}---{pickup_url}")
                else:
                    api_url = api_url or fallback_url
                    parsed = parse_api_mailbox_rows(f"{email}----{api_url}")
                entry = parsed[0]
            else:
                raise ValueError("条目必须是字符串或对象")
        except (IndexError, ValueError) as exc:
            raise ValueError(f"JSON 第 {index} 项格式错误：{exc}") from exc

        if entry.key in seen:
            continue
        seen.add(entry.key)
        entries.append(entry)
    return entries


def _deduplicate_entries(entries: list[ApiMailboxEntry]) -> list[ApiMailboxEntry]:
    result: list[ApiMailboxEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.key in seen:
            continue
        seen.add(entry.key)
        result.append(entry)
    return result


class ApiMailboxPool(BaseMailbox):
    """Use fixed email addresses and poll their individual API URLs for OTPs."""

    _lock = threading.Lock()

    def __init__(
        self,
        *,
        pool_text: str = "",
        pool_file: str = "",
        state_file: str = "",
        allow_reuse: bool = False,
        poll_interval: float | str = 3,
        request_timeout: float | str = 15,
        proxy: str | None = None,
        session: requests.Session | None = None,
    ):
        self.pool_text = str(pool_text or "")
        self.pool_file = str(pool_file or "").strip()
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.allow_reuse = bool(allow_reuse)
        self.poll_interval = max(0.0, float(3 if poll_interval in (None, "") else poll_interval))
        self.request_timeout = max(1.0, float(15 if request_timeout in (None, "") else request_timeout))
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()

    @classmethod
    def from_config(cls, config: dict) -> "ApiMailboxPool":
        return cls(
            pool_text=config.get("api_mailbox_pool_text", ""),
            pool_file=config.get("api_mailbox_pool_file", ""),
            state_file=config.get("api_mailbox_state_file", ""),
            allow_reuse=_truthy(config.get("api_mailbox_allow_reuse")),
            poll_interval=config.get("api_mailbox_poll_interval", 3),
            request_timeout=config.get("api_mailbox_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
        )

    def _pool_file_path(self) -> Path:
        path = Path(self.pool_file).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    def _load_pool_file_entries(self) -> list[ApiMailboxEntry]:
        path = self._pool_file_path()
        if path.suffix.lower() != ".json":
            raise RuntimeError(f"API 邮箱池文件必须是 JSON 文件: {path}")
        if not path.exists():
            raise RuntimeError(f"API 邮箱池 JSON 文件不存在: {path}")
        if not path.is_file():
            raise RuntimeError(f"API 邮箱池 JSON 路径不是文件: {path}")
        if path.stat().st_size > MAX_POOL_FILE_SIZE:
            raise RuntimeError(f"API 邮箱池 JSON 文件超过 {MAX_POOL_FILE_SIZE // 1024 // 1024} MB 限制: {path}")

        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise RuntimeError(f"读取 API 邮箱池 JSON 文件失败: {path}: {exc}") from exc
        if not text.strip():
            raise RuntimeError(f"API 邮箱池 JSON 文件为空: {path}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as json_exc:
            if text.lstrip().startswith(("{", "[")):
                raise RuntimeError(
                    f"API 邮箱池 JSON 格式错误: {path}，第 {json_exc.lineno} 行第 {json_exc.colno} 列"
                ) from json_exc
            # 兼容把邮箱池文件当作纯文本维护的场景：每行一组
            # “邮箱----API URL”或“邮箱---token---取件 URL”。文件名仍可
            # 使用 .json，方便用户沿用现有配置路径。
            try:
                entries = parse_api_mailbox_rows(text)
            except ValueError as text_exc:
                raise RuntimeError(
                    f"API 邮箱池文件既不是有效 JSON，也不是逐行邮箱配置: {path}；"
                    f"JSON 第 {json_exc.lineno} 行第 {json_exc.colno} 列；文本格式错误: {text_exc}"
                ) from text_exc
            if not entries:
                raise RuntimeError(f"API 邮箱池 JSON 文件为空: {path}")
            return entries
        try:
            return parse_api_mailbox_json(payload)
        except ValueError as exc:
            raise RuntimeError(f"API 邮箱池 JSON 内容无效: {path}: {exc}") from exc

    def _entries(self) -> list[ApiMailboxEntry]:
        entries: list[ApiMailboxEntry] = []
        if self.pool_text.strip():
            entries.extend(parse_api_mailbox_rows(self.pool_text))
        if self.pool_file:
            entries.extend(self._load_pool_file_entries())
        entries = _deduplicate_entries(entries)
        if not entries:
            raise RuntimeError(
                "API 邮箱池为空，请填写邮箱 API 池或配置邮箱池 JSON 文件路径"
            )
        return entries

    def _available_entry(self) -> ApiMailboxEntry:
        from core.mailbox_lifecycle import MailboxAllocationLifecycle

        entries = self._entries()
        lifecycle = MailboxAllocationLifecycle()
        for entry in entries:
            if lifecycle.is_available(
                provider_name="api_mailbox",
                resource_identifier=entry.key,
            ):
                return entry
        raise RuntimeError(f"API 邮箱池已用尽: total={len(entries)}")

    def _reserve(self, entry: ApiMailboxEntry) -> None:
        # Allocation state is owned by MailboxAllocationLifecycle.  Keep this
        # method as a no-op for compatibility with callers that subclassed the
        # old pool implementation; the legacy JSON ledger is never written.
        return None

    def peek_email(self) -> str:
        return self._available_entry().email

    def get_email(self) -> MailboxAccount:
        with self._lock:
            entry = self._available_entry()
            self._reserve(entry)
        credentials = {"email": entry.email, "api_url": entry.api_url}
        if entry.token:
            credentials["token"] = entry.token
        if entry.referer:
            credentials["referer"] = entry.referer
        if entry.provider:
            credentials["provider"] = entry.provider
        return MailboxAccount(
            email=entry.email,
            account_id=entry.key,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "api_mailbox",
                    "login_identifier": entry.email,
                    "display_name": entry.email,
                    "credentials": credentials,
                    "metadata": {"source": "email_api_url", "api_mailbox_provider": entry.provider},
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "api_mailbox",
                    "resource_type": "mailbox",
                    "resource_identifier": entry.key,
                    "handle": entry.email,
                    "display_name": entry.email,
                    "metadata": {
                        "email": entry.email,
                        "source": "email_api_url",
                        "api_mailbox_provider": entry.provider,
                        "reserved": not self.allow_reuse,
                    },
                },
            },
        )

    def _entry_for_account(self, account: MailboxAccount) -> ApiMailboxEntry:
        extra = dict(getattr(account, "extra", {}) or {})
        provider_account = dict(extra.get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        email = str(credentials.get("email") or account.email or "").strip()
        api_url = str(credentials.get("api_url") or "").strip()
        if email and api_url:
            return ApiMailboxEntry(
                email=email,
                api_url=api_url,
                token=str(credentials.get("token") or "").strip(),
                referer=str(credentials.get("referer") or "").strip(),
                provider=str(credentials.get("provider") or "generic").strip() or "generic",
            )
        account_key = str(account.email or "").strip().lower()
        for entry in self._entries():
            if entry.key == account_key:
                return entry
        raise RuntimeError(f"API 邮箱池未找到账号: {account.email}")

    def _request(self, entry: ApiMailboxEntry) -> tuple[object | None, str]:
        headers = {"Accept": "application/json, text/plain, */*", "User-Agent": "FuckGPT/api-mailbox"}
        if entry.provider in {"flysms", "icsms"} and entry.token:
            headers["Authorization"] = f"Bearer {entry.token}"
            headers["X-Mailbox-Email"] = entry.email
            if entry.referer:
                headers["Referer"] = entry.referer
        response = self.session.get(
            entry.api_url,
            headers=headers,
            proxies=self.proxy,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        raw = str(response.text or "").strip()
        try:
            payload = response.json()
        except Exception:
            payload = None
        return payload, raw

    def _sleep_interruptibly(self, seconds: float) -> None:
        deadline = time.monotonic() + max(float(seconds or 0), 0)
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            time.sleep(min(0.5, max(deadline - time.monotonic(), 0)))

    @staticmethod
    def _match_code(value: object, pattern: re.Pattern[str]) -> str:
        match = pattern.search(str(value or ""))
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    @classmethod
    def _extract_code(cls, payload: object | None, raw: str, code_pattern: str | None = None) -> str:
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)
        priority_keys = {
            "verification_code", "verificationcode", "verify_code", "verifycode",
            "mail_code", "mailcode", "otp", "one_time_code", "code",
        }
        ignored_keys = {
            "email", "mail", "url", "api_url", "password", "pass", "token",
            "status", "status_code", "timestamp", "created_at", "updated_at",
        }

        def walk(value: object, parent_key: str = "") -> str:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = str(key or "").strip().lower().replace("-", "_")
                    if normalized in priority_keys:
                        code = cls._match_code(child, pattern)
                        if code:
                            return code
                for key, child in value.items():
                    normalized = str(key or "").strip().lower().replace("-", "_")
                    if normalized in ignored_keys:
                        continue
                    code = walk(child, normalized)
                    if code:
                        return code
                return ""
            if isinstance(value, (list, tuple)):
                for child in value:
                    code = walk(child, parent_key)
                    if code:
                        return code
                return ""
            if isinstance(value, str) and parent_key not in ignored_keys:
                return cls._match_code(value, pattern)
            return ""

        code = walk(payload)
        if code:
            return code

        text = str(raw or "").strip()
        if not text:
            return ""
        if code_pattern:
            return cls._match_code(text, pattern)

        exact = re.fullmatch(r"[\s\"']*(\d{6})[\s\"']*", text)
        if exact:
            return exact.group(1)
        labelled = re.search(
            r"(?:验证码|校验码|动态码|verification\s*code|one[- ]?time\s*code|otp|code)"
            r"[^0-9]{0,20}(\d{6})(?!\d)",
            text,
            flags=re.IGNORECASE,
        )
        if labelled:
            return labelled.group(1)

        safe_text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", " ", text, flags=re.IGNORECASE)
        safe_text = re.sub(r"https?://\S+", " ", safe_text, flags=re.IGNORECASE)
        candidates = list(dict.fromkeys(re.findall(DEFAULT_CODE_PATTERN, safe_text)))
        return candidates[0] if len(candidates) == 1 else ""

    @classmethod
    def _signatures(cls, payload: object | None, raw: str) -> set[str]:
        signatures: set[str] = set()
        code = cls._extract_code(payload, raw)
        if code:
            signatures.add(f"code:{code}")
        link = _extract_verification_link(raw, "")
        if link:
            signatures.add("link:" + hashlib.sha256(link.encode("utf-8")).hexdigest())
        normalized = raw
        if payload is not None:
            try:
                normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except Exception:
                pass
        if normalized:
            signatures.add("body:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest())
        return signatures

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            payload, raw = self._request(self._entry_for_account(account))
            return self._signatures(payload, raw)
        except Exception:
            return set()

    def list_messages(self, account: MailboxAccount, limit: int = 10) -> list[dict]:
        del limit
        payload, raw = self._request(self._entry_for_account(account))
        code = self._extract_code(payload, raw)
        link = _extract_verification_link(raw, "")
        subject = ""
        received_at = ""
        if isinstance(payload, dict):
            subject = str(payload.get("subject") or payload.get("title") or "")
            received_at = str(payload.get("received_at") or payload.get("receivedAt") or payload.get("created_at") or payload.get("timestamp") or "")
        return [
            {
                "id": next(iter(self._signatures(payload, raw)), ""),
                "subject": subject or ("验证码" if code else "API 邮箱返回"),
                "from": "",
                "to": [account.email] if account.email else [],
                "received_at": received_at,
                "preview": raw[:1000],
                "code": code,
                "link": link,
                "provider": "api_mailbox",
            }
        ]

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        del keyword  # This API exposes the requested mailbox's code directly.
        entry = self._entry_for_account(account)
        seen = set(before_ids or set())
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            try:
                payload, raw = self._request(entry)
                code = self._extract_code(payload, raw, code_pattern=code_pattern)
                signatures = self._signatures(payload, raw)
                code_signature = f"code:{code}" if code else ""
                if code and code_signature not in seen:
                    return code
                seen.update(signatures)
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
            if self.poll_interval > 0:
                self._sleep_interruptibly(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 API 邮箱验证码超时 ({timeout}s){suffix}")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        entry = self._entry_for_account(account)
        seen = set(before_ids or set())
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            try:
                payload, raw = self._request(entry)
                link = _extract_verification_link(raw, keyword)
                link_signature = "link:" + hashlib.sha256(link.encode("utf-8")).hexdigest() if link else ""
                if link and link_signature not in seen:
                    return link
                seen.update(self._signatures(payload, raw))
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
            if self.poll_interval > 0:
                self._sleep_interruptibly(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 API 邮箱验证链接超时 ({timeout}s){suffix}")
