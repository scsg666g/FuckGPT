"""SMSToMe public phone-pool provider.

This adapts the phone-pool behavior from any-auto-register to the project's
common :class:`BaseSmsProvider` interface without requiring an HTML parser
dependency.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import random
import re
from typing import Iterable
from urllib.parse import urljoin

import requests

from core.base_sms import BaseSmsProvider, SmsActivation, SmsStatus


DEFAULT_BASE_URL = "https://smstome.com"
DEFAULT_COUNTRIES = (
    "poland",
    "united-kingdom",
    "slovenia",
    "sweden",
    "finland",
    "belgium",
)
COUNTRY_LABELS = {
    "poland": "Poland",
    "united-kingdom": "United Kingdom",
    "slovenia": "Slovenia",
    "sweden": "Sweden",
    "finland": "Finland",
    "belgium": "Belgium",
}
COUNTRY_REGIONS = {
    "poland": "PL",
    "united-kingdom": "GB",
    "slovenia": "SI",
    "sweden": "SE",
    "finland": "FI",
    "belgium": "BE",
}


class SMSToMeError(RuntimeError):
    pass


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _country_slugs(value: str | Iterable[str] | None) -> list[str]:
    raw = value if isinstance(value, str) else ",".join(str(item) for item in (value or ()))
    result = []
    for item in re.split(r"[\s,;|]+", raw):
        slug = item.strip().lower().replace("_", "-")
        if slug and slug not in result:
            result.append(slug)
    return result


class _PhonePageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.article_depth = 0
        self.href = ""
        self.text_parts: list[str] = []
        self.entries: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs):
        if tag == "article":
            self.article_depth += 1
        if self.article_depth and tag == "a":
            href = dict(attrs).get("href", "")
            if "/phone/" in href:
                self.href = href
                self.text_parts = []

    def handle_data(self, data: str):
        if self.href:
            self.text_parts.append(data)

    def handle_endtag(self, tag: str):
        if tag == "a" and self.href:
            phone = " ".join(self.text_parts).strip()
            if phone:
                self.entries.append((phone, self.href))
            self.href = ""
            self.text_parts = []
        if tag == "article" and self.article_depth:
            self.article_depth -= 1


class _SmsTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_first_table = False
        self.finished = False
        self.in_cell = False
        self.header_row = False
        self.row: list[str] = []
        self.cell_parts: list[str] = []
        self.rows: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, _attrs):
        if tag == "table" and not self.finished:
            self.in_first_table = True
        elif self.in_first_table and tag == "tr":
            self.row = []
            self.header_row = False
        elif self.in_first_table and tag == "th":
            self.header_row = True
        elif self.in_first_table and tag == "td":
            self.in_cell = True
            self.cell_parts = []
        elif self.in_cell and tag in {"br", "p", "div"}:
            self.cell_parts.append(" ")

    def handle_data(self, data: str):
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str):
        if self.in_first_table and tag == "td" and self.in_cell:
            self.row.append(" ".join("".join(self.cell_parts).split()))
            self.in_cell = False
        elif self.in_first_table and tag == "tr":
            if not self.header_row and len(self.row) >= 3 and self.row[2]:
                self.rows.append((self.row[0], self.row[1], self.row[2]))
        elif self.in_first_table and tag == "table":
            self.in_first_table = False
            self.finished = True


class SMSToMeClient(BaseSmsProvider):
    provider_key = "smstome"
    provider_label = "SMSToMe"

    def __init__(
        self,
        *,
        cookie: str = "",
        base_url: str = DEFAULT_BASE_URL,
        default_country: str = ",".join(DEFAULT_COUNTRIES),
        state_file: str = "data/.smstome_phone_state.json",
        max_pages_per_country: int = 5,
        request_timeout: float = 20,
        poll_interval: float = 5,
        proxy: str | None = None,
        session: requests.Session | None = None,
        log_fn=None,
    ):
        self.cookie = _text(cookie)
        self.base_url = _text(base_url).rstrip("/") or DEFAULT_BASE_URL
        self.default_service = "openai"
        self.default_country = _text(default_country) or ",".join(DEFAULT_COUNTRIES)
        self.state_file = Path(_text(state_file) or "data/.smstome_phone_state.json")
        self.max_pages_per_country = max(int(max_pages_per_country or 1), 1)
        self.request_timeout = max(float(request_timeout or 20), 0.5)
        self.poll_interval = max(float(poll_interval or 5), 0)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()
        self._log_fn = log_fn if callable(log_fn) else None
        self._seen: dict[str, set[str]] = {}

    @classmethod
    def from_config(cls, config: dict) -> "SMSToMeClient":
        client = cls(
            cookie=config.get("smstome_cookie", ""),
            base_url=config.get("smstome_base_url", DEFAULT_BASE_URL),
            default_country=config.get("smstome_country_slugs", ",".join(DEFAULT_COUNTRIES)),
            state_file=config.get("smstome_state_file", "data/.smstome_phone_state.json"),
            max_pages_per_country=int(float(config.get("smstome_sync_max_pages_per_country") or 5)),
            request_timeout=config.get("smstome_request_timeout", 20),
            poll_interval=config.get("smstome_poll_interval_seconds", 5),
            proxy=config.get("proxy") or config.get("sms_proxy") or None,
            log_fn=config.get("_log_fn"),
        )
        client.buy_max_attempts = max(int(float(config.get("smstome_phone_attempts") or 3)), 1)
        client.buy_retry_interval = max(float(config.get("smstome_buy_retry_interval") or 2), 0)
        client.otp_timeout_seconds = max(int(float(config.get("smstome_otp_timeout_seconds") or 120)), 1)
        return client

    def configuration_error(self) -> str:
        return ""

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123 Safari/537.36",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _get_html(self, url: str) -> str:
        try:
            response = self.session.get(
                url,
                headers=self._headers(),
                proxies=self.proxy,
                timeout=self.request_timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            html = str(response.text or "")
        except Exception as exc:  # noqa: BLE001
            message = str(exc).replace(self.cookie, "***") if self.cookie else str(exc)
            raise SMSToMeError(f"SMSToMe 请求失败: {message}") from exc
        if not html.strip():
            raise SMSToMeError("SMSToMe 返回空页面")
        return html

    def _list_phone_entries(self, slug: str) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        base = f"{self.base_url}/country/{slug}"
        for page in range(1, self.max_pages_per_country + 1):
            parser = _PhonePageParser()
            parser.feed(self._get_html(base if page == 1 else f"{base}?page={page}"))
            new_count = 0
            for phone, href in parser.entries:
                if phone in seen:
                    continue
                seen.add(phone)
                entries.append((phone, urljoin(self.base_url + "/", href)))
                new_count += 1
            if not parser.entries or (page > 1 and new_count == 0):
                break
        return entries

    def _load_used(self) -> set[str]:
        if not self.state_file.exists():
            return set()
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            values = payload.get("used_numbers", []) if isinstance(payload, dict) else payload
            return {_text(item) for item in values if _text(item)}
        except Exception:
            return set()

    def _mark_used(self, phone: str) -> None:
        values = self._load_used()
        values.add(phone)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps({"used_numbers": sorted(values)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_number(self, service: str = "", country: str = "", **_options) -> SmsActivation:
        slugs = _country_slugs(country) or _country_slugs(self.default_country) or list(DEFAULT_COUNTRIES)
        used = self._load_used()
        candidates: list[tuple[str, str, str]] = []
        for slug in slugs:
            candidates.extend((phone, slug, url) for phone, url in self._list_phone_entries(slug) if phone not in used)
        if not candidates:
            raise SMSToMeError("SMSToMe 当前国家暂无未使用号码 (NO_NUMBERS)")
        phone, slug, detail_url = random.choice(candidates)
        self._mark_used(phone)
        if self._log_fn:
            self._log_fn(f"SMSToMe 分配号码成功：country={slug}")
        return SmsActivation(
            activation_id=detail_url,
            phone_number=phone,
            provider="smstome",
            service=_text(service) or self.default_service,
            country=slug,
            raw=detail_url,
            metadata={"detail_url": detail_url},
        )

    def _messages(self, detail_url: str) -> list[tuple[str, str, str]]:
        parser = _SmsTableParser()
        parser.feed(self._get_html(detail_url))
        return parser.rows

    @staticmethod
    def _fingerprint(message: tuple[str, str, str]) -> str:
        return "\t".join(message)

    def mark_sms_sent(self, activation_id: str) -> str:
        messages = self._messages(_text(activation_id))
        self._seen[_text(activation_id)] = {self._fingerprint(item) for item in messages}
        return "BASELINE_RECORDED"

    def get_status(self, activation_id: str) -> SmsStatus:
        activation_id = _text(activation_id)
        messages = self._messages(activation_id)
        seen = self._seen.setdefault(activation_id, set())
        for message in messages:
            fingerprint = self._fingerprint(message)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            match = re.search(r"(?<!\d)(\d(?:[\s-]*\d){3,7})(?!\d)", message[2])
            if match:
                code = re.sub(r"[\s-]", "", match.group(1))
                return SmsStatus(status="ok", code=code, raw=message[2], metadata={"sender": message[0], "received": message[1]})
        return SmsStatus(status="waiting", raw=f"messages={len(messages)}")

    def set_status(self, activation_id: str, status: int | str) -> str:
        if str(status).strip() not in {"1", "3", "6", "8"}:
            raise SMSToMeError(f"SMSToMe 不支持状态码: {status}")
        return "ACKNOWLEDGED"

    def test_connection(self) -> dict:
        slug = (_country_slugs(self.default_country) or list(DEFAULT_COUNTRIES))[0]
        count = len(self._list_phone_entries(slug))
        return {"message": f"SMSToMe 连接成功，{COUNTRY_LABELS.get(slug, slug)} 当前发现 {count} 个号码", "available": count}

    def list_country_options(self) -> list[dict[str, str]]:
        return [
            {
                "value": slug,
                "label": f"{COUNTRY_LABELS.get(slug, slug)} ({COUNTRY_REGIONS.get(slug, slug)})",
                "english_name": COUNTRY_LABELS.get(slug, slug),
                "localized_name": "",
                "region_code": COUNTRY_REGIONS.get(slug, ""),
                "dial_code": "",
            }
            for slug in DEFAULT_COUNTRIES
        ]

    def list_service_options(self) -> list[dict[str, str]]:
        return [{"value": "openai", "label": "OpenAI / ChatGPT (openai)"}]
