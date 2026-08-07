"""Remote temporary-mail providers used by registration workflows.

The adapters in this module keep provider-specific API contracts behind the
small :class:`BaseMailbox` interface.  Account-scoped credentials are stored in
``MailboxAccount.extra`` so the mailbox resource can be reopened for later
verification and account-recovery tasks.
"""

from __future__ import annotations

import hashlib
import html
import quopri
import random
import re
import string
import time
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link


EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
DEFAULT_CODE_PATTERNS = (
    re.compile(
        r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|"
        r"security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)"
        r"[^0-9]{0,30}(\d{4,8})"
    ),
    re.compile(r"(?is)\bcode\b[^0-9]{0,12}(\d{4,8})"),
    re.compile(r"(?<![a-zA-Z0-9])(\d{4,8})(?![a-zA-Z0-9])"),
)


def _positive_float(value: object, default: float, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _normalize_domain(value: object) -> str:
    return str(value or "").strip().lower().lstrip("@")


def _random_local_part(letters: int = 8, digits: int = 4) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=letters)) + "".join(
        random.choices(string.digits, k=digits)
    )


class RemoteMailbox(BaseMailbox):
    """Shared polling, extraction, persistence, and HTTP safety helpers."""

    provider_key = "remote"

    def __init__(
        self,
        *,
        proxy: str | None = None,
        poll_interval: float | str = 3,
        request_timeout: float | str = 15,
        session: requests.Session | None = None,
        log_fn=None,
    ) -> None:
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.poll_interval = _positive_float(poll_interval, 3)
        self.request_timeout = _positive_float(request_timeout, 15, minimum=1)
        self.session = session or requests.Session()
        self._log_fn = log_fn
        self._secrets: set[str] = set()

    def _log(self, message: str) -> None:
        if callable(self._log_fn):
            self._log_fn(self._redact(message))

    def _redact(self, value: object) -> str:
        text = str(value or "")
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _json(self, response: Any, action: str) -> Any:
        status = int(getattr(response, "status_code", 200) or 200)
        try:
            payload = response.json()
        except Exception as exc:
            preview = self._redact(str(getattr(response, "text", "") or "")[:200])
            raise RuntimeError(f"{action}返回非 JSON: HTTP {status} {preview}".strip()) from exc
        if status >= 400:
            error = payload
            if isinstance(payload, dict):
                error = payload.get("error") or payload.get("message") or payload.get("detail") or payload
            raise RuntimeError(f"{action}失败: HTTP {status} {self._redact(error)}")
        if isinstance(payload, dict) and payload.get("success") is False:
            error = payload.get("error") or payload.get("message") or "unknown error"
            raise RuntimeError(f"{action}失败: {self._redact(error)}")
        return payload

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def _request(self, method: str, url: str, **kwargs) -> Any:
        self.raise_if_cancelled()
        kwargs.setdefault("proxies", self.proxy)
        kwargs.setdefault("timeout", self.request_timeout)
        return self.session.request(method, url, **kwargs)

    @staticmethod
    def _credentials(account: MailboxAccount) -> dict[str, Any]:
        extra = dict(getattr(account, "extra", {}) or {})
        provider_account = dict(extra.get("provider_account") or {})
        return dict(provider_account.get("credentials") or {})

    def _account(
        self,
        *,
        email: str,
        account_id: str,
        credentials: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MailboxAccount:
        credentials = {"email": email, **dict(credentials or {})}
        metadata = {"source": self.provider_key, **dict(metadata or {})}
        resource_id = str(account_id or email).strip() or email
        return MailboxAccount(
            email=email,
            account_id=resource_id,
            extra={
                "provider": self.provider_key,
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": self.provider_key,
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": credentials,
                    "metadata": metadata,
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": self.provider_key,
                    "resource_type": "mailbox",
                    "resource_identifier": resource_id,
                    "handle": email,
                    "display_name": email,
                    "metadata": {"email": email, **metadata},
                },
            },
        )

    @staticmethod
    def _message_id(message: dict[str, Any]) -> str:
        for key in ("id", "message_id", "messageId", "msgid", "_id"):
            value = message.get(key)
            if value not in (None, ""):
                return str(value)
        material = "|".join(
            str(message.get(key) or "")
            for key in ("date", "createdAt", "receivedAt", "from", "subject", "text", "body", "html")
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @classmethod
    def _normalize_message(
        cls, message: dict[str, Any], detail: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        detail = dict(detail or {})
        source = {**message, **detail}
        message_id = cls._message_id(source)
        subject = str(source.get("subject") or source.get("title") or "")
        text = " ".join(
            str(source.get(key) or "")
            for key in ("text", "content", "body", "snippet", "intro", "raw", "raw_headers")
        ).strip()
        html_body = " ".join(
            str(source.get(key) or "") for key in ("html", "html_content", "htmlBody")
        ).strip()
        return {
            "id": message_id,
            "subject": subject,
            "text": text,
            "html": html_body,
            "from": source.get("from") or source.get("from_address") or source.get("sender") or "",
            "received_at": source.get("date")
            or source.get("createdAt")
            or source.get("created_at")
            or source.get("receivedAt")
            or source.get("received_at")
            or "",
        }

    @staticmethod
    def _mail_text(message: dict[str, Any]) -> str:
        raw = " ".join(
            str(message.get(key) or "") for key in ("subject", "from", "text", "html")
        )
        try:
            raw = quopri.decodestring(raw).decode("utf-8", errors="ignore")
        except Exception:
            pass
        raw = html.unescape(raw)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = EMAIL_PATTERN.sub(" ", raw)
        raw = URL_PATTERN.sub(" ", raw)
        return re.sub(r"\s+", " ", raw).strip()

    @staticmethod
    def _extract_code(text: str, pattern: str | None = None) -> str | None:
        patterns: list[re.Pattern[str]] = []
        if pattern:
            try:
                patterns.append(re.compile(pattern, re.IGNORECASE | re.DOTALL))
            except re.error as exc:
                raise ValueError(f"验证码正则无效: {exc}") from exc
        patterns.extend(DEFAULT_CODE_PATTERNS)
        for regex in patterns:
            match = regex.search(str(text or ""))
            if match:
                return match.group(1) if match.groups() else match.group(0)
        return None

    def _fetch_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {self._message_id(item) for item in self._fetch_messages(account)}
        except Exception:
            self.raise_if_cancelled()
            return set()

    def list_messages(self, account: MailboxAccount, limit: int = 10) -> list[dict[str, Any]]:
        normalized = [self._normalize_message(item) for item in self._fetch_messages(account)]
        result: list[dict[str, Any]] = []
        for message in normalized[: max(1, min(int(limit or 10), 50))]:
            content = self._mail_text(message)
            result.append(
                {
                    **message,
                    "preview": content[:1000],
                    "code": self._extract_code(content),
                    "link": _extract_verification_link(
                        " ".join(str(message.get(key) or "") for key in ("subject", "text", "html"))
                    ),
                    "provider": self.provider_key,
                }
            )
        return result

    def _sleep(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self.raise_if_cancelled()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        seen = {str(value) for value in (before_ids or set())}
        deadline = time.monotonic() + max(float(timeout or 0), 0.01)
        last_error = ""
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            try:
                for raw in self._fetch_messages(account):
                    message = self._normalize_message(raw)
                    message_id = str(message["id"])
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    content = self._mail_text(message)
                    if keyword and keyword.lower() not in content.lower():
                        continue
                    code = self._extract_code(content, code_pattern)
                    if code:
                        self._log(f"{self.provider_key} 收到验证码")
                        return code
            except Exception as exc:  # transient mailbox errors are retried until timeout
                self.raise_if_cancelled()
                last_error = self._redact(exc)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._sleep(min(self.poll_interval, remaining))
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待邮箱验证码超时 ({int(timeout)}s){suffix}")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        seen = {str(value) for value in (before_ids or set())}
        deadline = time.monotonic() + max(float(timeout or 0), 0.01)
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            for raw in self._fetch_messages(account):
                message = self._normalize_message(raw)
                if message["id"] in seen:
                    continue
                seen.add(message["id"])
                original = " ".join(
                    str(message.get(key) or "") for key in ("subject", "text", "html")
                )
                link = _extract_verification_link(original, keyword)
                if link:
                    return link
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._sleep(min(self.poll_interval, remaining))
        raise TimeoutError(f"等待邮箱验证链接超时 ({int(timeout)}s)")


class TempMailLolMailbox(RemoteMailbox):
    provider_key = "tempmail_lol"

    def __init__(self, *, base_url: str = "https://api.tempmail.lol/v2", **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_url = str(base_url or "https://api.tempmail.lol/v2").rstrip("/")

    @classmethod
    def from_config(cls, config: dict) -> "TempMailLolMailbox":
        return cls(
            base_url=config.get("tempmail_lol_base_url", "https://api.tempmail.lol/v2"),
            poll_interval=config.get("tempmail_lol_poll_interval", 3),
            request_timeout=config.get("tempmail_lol_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
            log_fn=config.get("_log_fn"),
        )

    def get_email(self) -> MailboxAccount:
        response = self._request("POST", f"{self.base_url}/inbox/create", json={})
        payload = self._unwrap(self._json(response, "TempMail.lol 创建邮箱"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"TempMail.lol 创建邮箱返回异常: {payload}")
        email = str(payload.get("address") or payload.get("email") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not email or not token:
            raise RuntimeError("TempMail.lol 创建邮箱未返回 address/token")
        self._secrets.add(token)
        return self._account(email=email, account_id=email, credentials={"inbox_token": token})

    def _fetch_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        credentials = self._credentials(account)
        token = str(credentials.get("inbox_token") or account.account_id or "").strip()
        if not token:
            raise RuntimeError("TempMail.lol 邮箱缺少 inbox token")
        self._secrets.add(token)
        response = self._request("GET", f"{self.base_url}/inbox", params={"token": token})
        payload = self._unwrap(self._json(response, "TempMail.lol 获取邮件"))
        messages = payload.get("emails", []) if isinstance(payload, dict) else payload
        return [dict(item) for item in (messages or []) if isinstance(item, dict)]


class DuckMailMailbox(RemoteMailbox):
    provider_key = "duckmail"

    def __init__(
        self,
        *,
        api_url: str = "https://www.duckmail.sbs",
        provider_url: str = "https://api.duckmail.sbs",
        bearer: str = "kevin273945",
        domain: str = "",
        api_key: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.api_url = str(api_url or "https://www.duckmail.sbs").rstrip("/")
        self.provider_url = str(provider_url or "https://api.duckmail.sbs").rstrip("/")
        self.bearer = str(bearer or "kevin273945").strip()
        self.domain = _normalize_domain(domain)
        self.api_key = str(api_key or "").strip()
        self.direct = bool(self.api_key)
        self._secrets.update({self.bearer, self.api_key})

    @classmethod
    def from_config(cls, config: dict) -> "DuckMailMailbox":
        return cls(
            api_url=config.get("duckmail_api_url", "https://www.duckmail.sbs"),
            provider_url=config.get("duckmail_provider_url", "https://api.duckmail.sbs"),
            bearer=config.get("duckmail_bearer", "kevin273945"),
            domain=config.get("duckmail_domain", ""),
            api_key=config.get("duckmail_api_key", ""),
            poll_interval=config.get("duckmail_poll_interval", 3),
            request_timeout=config.get("duckmail_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
            log_fn=config.get("_log_fn"),
        )

    def _api_request(self, method: str, endpoint: str, *, token: str = "", **kwargs) -> Any:
        if self.direct:
            url = f"{self.provider_url}{endpoint}"
            authorization = token or self.api_key
            headers = {"Authorization": f"Bearer {authorization}", "Content-Type": "application/json"}
        else:
            url = f"{self.api_url}/api/mail?endpoint={quote(endpoint, safe='')}"
            authorization = token or self.bearer
            headers = {
                "Authorization": f"Bearer {authorization}",
                "X-API-Provider-Base-URL": self.provider_url,
                "Content-Type": "application/json",
            }
        return self._request(method, url, headers=headers, **kwargs)

    def get_email(self) -> MailboxAccount:
        domain = self.domain
        if not domain:
            hostname = (urlsplit(self.provider_url).hostname or "duckmail.sbs").lower()
            domain = hostname[4:] if hostname.startswith("api.") else hostname
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = f"Test{''.join(random.choices(string.digits, k=8))}!"
        address = f"{username}@{domain}"
        created = self._json(
            self._api_request("POST", "/accounts", json={"address": address, "password": password}),
            "DuckMail 创建邮箱",
        )
        created = self._unwrap(created)
        if isinstance(created, dict):
            address = str(created.get("address") or created.get("email") or address).strip()
        login = self._unwrap(
            self._json(
                self._api_request("POST", "/token", json={"address": address, "password": password}),
                "DuckMail 登录邮箱",
            )
        )
        token = str(login.get("token") or "").strip() if isinstance(login, dict) else ""
        if not token:
            raise RuntimeError("DuckMail 登录邮箱未返回 token")
        self._secrets.update({token, password})
        return self._account(
            email=address,
            account_id=address,
            credentials={"access_token": token, "password": password},
            metadata={"direct_api": self.direct, "domain": domain},
        )

    def _fetch_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        token = str(self._credentials(account).get("access_token") or account.account_id or "").strip()
        if not token:
            raise RuntimeError("DuckMail 邮箱缺少 access token")
        self._secrets.add(token)
        payload = self._unwrap(
            self._json(
                self._api_request("GET", "/messages?page=1", token=token),
                "DuckMail 获取邮件",
            )
        )
        messages = payload.get("hydra:member", payload.get("messages", [])) if isinstance(payload, dict) else payload
        result: list[dict[str, Any]] = []
        for item in (messages or []):
            if not isinstance(item, dict):
                continue
            message_id = self._message_id(item)
            try:
                detail = self._unwrap(
                    self._json(
                        self._api_request("GET", f"/messages/{quote(message_id, safe='')}", token=token),
                        "DuckMail 获取邮件详情",
                    )
                )
            except Exception:
                self.raise_if_cancelled()
                detail = {}
            result.append(self._normalize_message(item, detail if isinstance(detail, dict) else {}))
        return result


class GPTMailMailbox(RemoteMailbox):
    provider_key = "gptmail"

    def __init__(
        self,
        *,
        base_url: str = "https://mail.chatgpt.org.uk",
        api_key: str = "",
        domain: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.base_url = str(base_url or "https://mail.chatgpt.org.uk").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = _normalize_domain(domain)
        self._secrets.add(self.api_key)

    @classmethod
    def from_config(cls, config: dict) -> "GPTMailMailbox":
        return cls(
            base_url=config.get("gptmail_base_url", "https://mail.chatgpt.org.uk"),
            api_key=config.get("gptmail_api_key", ""),
            domain=config.get("gptmail_domain", ""),
            poll_interval=config.get("gptmail_poll_interval", 3),
            request_timeout=config.get("gptmail_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
            log_fn=config.get("_log_fn"),
        )

    def _api_request(self, method: str, path: str, **kwargs) -> Any:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return self._request(method, f"{self.base_url}{path}", headers=headers, **kwargs)

    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{_random_local_part(6, 4)}@{self.domain}"
            return self._account(email=email, account_id=email, metadata={"domain": self.domain, "local_address": True})
        payload = self._unwrap(
            self._json(self._api_request("GET", "/api/generate-email"), "GPTMail 创建邮箱")
        )
        email = str(payload.get("email") or payload.get("address") or "").strip() if isinstance(payload, dict) else ""
        if not email:
            raise RuntimeError("GPTMail 创建邮箱未返回 email")
        return self._account(email=email, account_id=email)

    def _fetch_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        payload = self._unwrap(
            self._json(
                self._api_request("GET", "/api/emails", params={"email": account.email}),
                "GPTMail 获取邮件",
            )
        )
        messages = payload.get("emails", payload.get("messages", [])) if isinstance(payload, dict) else payload
        result: list[dict[str, Any]] = []
        for item in (messages or []):
            if not isinstance(item, dict):
                continue
            message_id = self._message_id(item)
            try:
                detail = self._unwrap(
                    self._json(
                        self._api_request("GET", f"/api/email/{quote(message_id, safe='')}"),
                        "GPTMail 获取邮件详情",
                    )
                )
            except Exception:
                self.raise_if_cancelled()
                detail = {}
            result.append(self._normalize_message(item, detail if isinstance(detail, dict) else {}))
        return result


class MaliAPIMailbox(RemoteMailbox):
    provider_key = "maliapi"

    def __init__(
        self,
        *,
        base_url: str = "https://maliapi.215.im/v1",
        api_key: str = "",
        domain: str = "",
        auto_domain_strategy: str = "balanced",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.base_url = str(base_url or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = _normalize_domain(domain)
        self.auto_domain_strategy = str(auto_domain_strategy or "").strip()
        self._secrets.add(self.api_key)

    @classmethod
    def from_config(cls, config: dict) -> "MaliAPIMailbox":
        return cls(
            base_url=config.get("maliapi_base_url", "https://maliapi.215.im/v1"),
            api_key=config.get("maliapi_api_key", ""),
            domain=config.get("maliapi_domain", ""),
            auto_domain_strategy=config.get("maliapi_auto_domain_strategy", "balanced"),
            poll_interval=config.get("maliapi_poll_interval", 3),
            request_timeout=config.get("maliapi_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
            log_fn=config.get("_log_fn"),
        )

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise RuntimeError("MaliAPI API Key 未配置")

    def _api_request(self, method: str, path: str, *, bearer: str = "", **kwargs) -> Any:
        self._ensure_api_key()
        headers = {"Accept": "application/json", "Content-Type": "application/json", "X-API-Key": self.api_key}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return self._request(method, f"{self.base_url}{path}", headers=headers, **kwargs)

    def get_email(self) -> MailboxAccount:
        body: dict[str, str] = {}
        if self.domain:
            body["domain"] = self.domain
        if self.auto_domain_strategy:
            body["autoDomainStrategy"] = self.auto_domain_strategy
        payload = self._unwrap(
            self._json(self._api_request("POST", "/accounts", json=body), "MaliAPI 创建邮箱")
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"MaliAPI 创建邮箱返回异常: {payload}")
        email = str(payload.get("address") or payload.get("email") or "").strip()
        temp_token = str(payload.get("tempToken") or payload.get("temp_token") or payload.get("token") or "").strip()
        inbox_id = str(payload.get("id") or "").strip()
        if not email:
            raise RuntimeError("MaliAPI 创建邮箱未返回 address/email")
        self._secrets.add(temp_token)
        return self._account(
            email=email,
            account_id=inbox_id or email,
            credentials={"temp_token": temp_token, "inbox_id": inbox_id},
            metadata={"domain": self.domain, "auto_domain_strategy": self.auto_domain_strategy},
        )

    def _fetch_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        credentials = self._credentials(account)
        bearer = str(credentials.get("temp_token") or "").strip()
        self._secrets.add(bearer)
        payload = self._unwrap(
            self._json(
                self._api_request("GET", "/messages", bearer=bearer, params={"address": account.email}),
                "MaliAPI 获取邮件",
            )
        )
        messages = payload.get("messages", []) if isinstance(payload, dict) else payload
        result: list[dict[str, Any]] = []
        for item in (messages or []):
            if not isinstance(item, dict):
                continue
            message_id = self._message_id(item)
            try:
                detail = self._unwrap(
                    self._json(
                        self._api_request("GET", f"/messages/{quote(message_id, safe='')}", bearer=bearer),
                        "MaliAPI 获取邮件详情",
                    )
                )
                if isinstance(detail, dict) and isinstance(detail.get("message"), dict):
                    detail = detail["message"]
            except Exception:
                self.raise_if_cancelled()
                detail = {}
            result.append(self._normalize_message(item, detail if isinstance(detail, dict) else {}))
        return result


class MoeMailMailbox(RemoteMailbox):
    provider_key = "moemail"

    def __init__(
        self,
        *,
        api_url: str = "https://sall.cc",
        api_key: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.api_url = str(api_url or "https://sall.cc").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self._secrets.add(self.api_key)
        self._session_ready = False
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145 Safari/537.36",
                "Origin": self.api_url,
                "Referer": f"{self.api_url}/zh-CN/login",
            }
        )

    @classmethod
    def from_config(cls, config: dict) -> "MoeMailMailbox":
        return cls(
            api_url=config.get("moemail_api_url", "https://sall.cc"),
            api_key=config.get("moemail_api_key", ""),
            poll_interval=config.get("moemail_poll_interval", 3),
            request_timeout=config.get("moemail_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
            log_fn=config.get("_log_fn"),
        )

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _login(self, username: str, password: str) -> None:
        csrf_payload = self._json(
            self._request("GET", f"{self.api_url}/api/auth/csrf", headers=self._headers()),
            "MoeMail 获取 CSRF",
        )
        csrf = str(csrf_payload.get("csrfToken") or "") if isinstance(csrf_payload, dict) else ""
        if not csrf:
            raise RuntimeError("MoeMail 登录未获取到 CSRF token")
        response = self._request(
            "POST",
            f"{self.api_url}/api/auth/callback/credentials",
            headers={**self._headers(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "username": username,
                "password": password,
                "csrfToken": csrf,
                "redirect": "false",
                "callbackUrl": self.api_url,
            },
            allow_redirects=True,
        )
        status = int(getattr(response, "status_code", 200) or 200)
        if status >= 400:
            raise RuntimeError(f"MoeMail 登录失败: HTTP {status}")
        if not any("session-token" in cookie.name for cookie in self.session.cookies):
            raise RuntimeError("MoeMail 登录失败，未获得 session-token Cookie")
        self._session_ready = True

    def _register_and_login(self) -> tuple[str, str]:
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        password = f"Test{''.join(random.choices(string.digits, k=8))}!"
        response = self._request(
            "POST",
            f"{self.api_url}/api/auth/register",
            headers=self._headers(),
            json={"username": username, "password": password, "turnstileToken": ""},
        )
        status = int(getattr(response, "status_code", 200) or 200)
        if status >= 400:
            preview = self._redact(str(getattr(response, "text", "") or "")[:200])
            raise RuntimeError(f"MoeMail 注册账号失败: HTTP {status} {preview}".strip())
        self._secrets.update({username, password})
        self._login(username, password)
        return username, password

    def _ensure_session(self, account: MailboxAccount) -> None:
        if self._session_ready:
            return
        credentials = self._credentials(account)
        cookies = credentials.get("cookies")
        if isinstance(cookies, dict) and cookies:
            self._secrets.update(str(value) for value in cookies.values())
            self.session.cookies.update({str(key): str(value) for key, value in cookies.items()})
            self._session_ready = True
            return
        username = str(credentials.get("username") or "").strip()
        password = str(credentials.get("password") or "")
        if username and password:
            self._login(username, password)
            return
        raise RuntimeError("MoeMail 邮箱缺少登录会话")

    def get_email(self) -> MailboxAccount:
        username, password = self._register_and_login()
        config_payload = self._unwrap(
            self._json(
                self._request("GET", f"{self.api_url}/api/config", headers=self._headers()),
                "MoeMail 获取域名",
            )
        )
        raw_domains = config_payload.get("emailDomains", "sall.cc") if isinstance(config_payload, dict) else "sall.cc"
        if isinstance(raw_domains, list):
            domains = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            domains = [item.strip() for item in str(raw_domains or "sall.cc").split(",") if item.strip()]
        domain = random.choice(domains or ["sall.cc"])
        payload = self._unwrap(
            self._json(
                self._request(
                    "POST",
                    f"{self.api_url}/api/emails/generate",
                    headers=self._headers(),
                    json={"name": _random_local_part(8, 0), "domain": domain, "expiryTime": 86400000},
                ),
                "MoeMail 创建邮箱",
            )
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"MoeMail 创建邮箱返回异常: {payload}")
        email = str(payload.get("email") or payload.get("address") or "").strip()
        email_id = str(payload.get("id") or "").strip()
        if not email or not email_id:
            raise RuntimeError("MoeMail 创建邮箱未返回 email/id")
        cookies = self.session.cookies.get_dict()
        session_token = next(
            (value for key, value in cookies.items() if "session-token" in key), ""
        )
        self._secrets.update({session_token, username, password})
        return self._account(
            email=email,
            account_id=email_id,
            credentials={
                "email_id": email_id,
                "username": username,
                "password": password,
                "cookies": cookies,
                "session_token": session_token,
            },
            metadata={"domain": domain, "expires_in_ms": 86400000},
        )

    def _fetch_messages(self, account: MailboxAccount) -> list[dict[str, Any]]:
        self._ensure_session(account)
        credentials = self._credentials(account)
        email_id = str(credentials.get("email_id") or account.account_id or "").strip()
        if not email_id:
            raise RuntimeError("MoeMail 邮箱缺少 email id")
        payload = self._unwrap(
            self._json(
                self._request(
                    "GET",
                    f"{self.api_url}/api/emails/{quote(email_id, safe='')}",
                    headers=self._headers(),
                ),
                "MoeMail 获取邮件",
            )
        )
        messages = payload.get("messages", []) if isinstance(payload, dict) else payload
        return [dict(item) for item in (messages or []) if isinstance(item, dict)]


__all__ = [
    "DuckMailMailbox",
    "GPTMailMailbox",
    "MaliAPIMailbox",
    "MoeMailMailbox",
    "RemoteMailbox",
    "TempMailLolMailbox",
]
