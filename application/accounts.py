from __future__ import annotations

import ast
import csv
import json
import re

from core.datetime_utils import serialize_datetime
from domain.accounts import (
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountStats,
    AccountUpdateCommand,
)
from infrastructure.accounts_repository import AccountsRepository


IMPORT_LINE_RE = re.compile(
    r'^\s*(?P<email>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'\s+(?P<password>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'(?:\s+(?P<extra>.*))?\s*$'
)


def _decode_import_token(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(text)
            return decoded if isinstance(decoded, str) else str(decoded)
        except Exception:
            return text[1:-1]
    return text


def _parse_csv_row(raw: str) -> list[str]:
    return next(csv.reader([raw]))


def _normalized_debug_key(key: object) -> str:
    text = str(key or "").strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _is_sensitive_debug_key(key: object) -> bool:
    normalized = _normalized_debug_key(key)
    compact = normalized.replace("_", "")
    if normalized in {
        "token",
        "authorization",
        "api_key",
        "client_secret",
        "password",
        "secret",
        "cookie",
        "cookies",
        "sso",
        "sso_rw",
        "wos_session",
        "private_key",
        "bearer",
        "csrf",
    }:
        return True
    if compact in {
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "authtoken",
        "bearertoken",
        "apikey",
        "clientsecret",
        "privatekey",
    }:
        return True
    # Usage counters describe token consumption and are safe debug data.
    accounting_names = (
        "token_count",
        "token_counts",
        "token_used",
        "token_usage",
        "token_limit",
        "token_remaining",
        "token_total",
    )
    if any(normalized == name or normalized.endswith(f"_{name}") for name in accounting_names):
        return False
    parts = set(normalized.split("_"))
    return (
        compact.endswith(("token", "apikey", "clientsecret", "privatekey"))
        or "authorization" in compact
        or bool(parts & {"token", "cookie", "cookies", "password", "secret", "bearer"})
        or normalized.endswith("_token")
        or "_token_" in normalized
        or "cookie" in normalized
        or "password" in normalized
        or "client_secret" in normalized
        or "api_key" in normalized
        or "private_key" in normalized
        or normalized.endswith("_secret")
        or normalized.startswith("authorization_")
    )


def _is_phone_debug_key(key: object) -> bool:
    normalized = _normalized_debug_key(key)
    compact = normalized.replace("_", "")
    if compact in {
        "phonebound",
        "hasphone",
        "phoneverified",
        "mobileverified",
        "msisdnverified",
        "telverified",
    }:
        return False
    if "phone" in compact:
        return True
    if compact in {
        "mobile",
        "mobilenumber",
        "mobileno",
        "msisdn",
        "tel",
        "telephone",
        "telephonenumber",
    }:
        return True
    return bool(set(normalized.split("_")) & {"mobile", "msisdn", "tel", "telephone"})


def _mask_debug_phone(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\+?\d{1,4}\*{3,}\d{2,4}", text):
        return text
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) <= 8:
        return "***"
    if text.startswith("+"):
        return f"+{digits[:3]}****{digits[-4:]}"
    return f"{digits[:4]}****{digits[-4:]}"


def _dict_describes_sensitive_credential(value: dict) -> bool:
    for descriptor_key in ("key", "name", "credential_key", "credential_name"):
        descriptor = value.get(descriptor_key)
        if isinstance(descriptor, str) and _is_sensitive_debug_key(descriptor):
            return True
    credential_type = str(value.get("credential_type") or "").strip().lower()
    return credential_type in {"token", "cookie", "password", "secret", "api_key"}


def _sanitize_debug_value(
    value: object,
    *,
    key: object = "",
    credential_context: bool = False,
) -> object:
    """Recursively make legacy/debug account payloads safe for public APIs."""

    phone_context = _is_phone_debug_key(key)
    if isinstance(value, dict):
        credential_context = credential_context or _dict_describes_sensitive_credential(value)
        sanitized: dict[str, object] = {}
        for child_key, child_value in value.items():
            child_key_text = str(child_key)
            normalized = _normalized_debug_key(child_key_text)
            if _is_sensitive_debug_key(child_key_text):
                continue
            compact = normalized.replace("_", "")
            if compact.endswith(("credentials", "credentialpreviews")):
                continue
            if credential_context and normalized in {
                "value",
                "preview",
                "value_preview",
                "secret_value",
            }:
                continue
            child_context = child_key_text
            if phone_context and not _is_phone_debug_key(child_key_text):
                child_context = key
            sanitized[child_key_text] = _sanitize_debug_value(
                child_value,
                key=child_context,
                credential_context=credential_context or "credential" in normalized,
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_debug_value(
                item,
                key=key,
                credential_context=credential_context,
            )
            for item in value
        ]
    if phone_context and not isinstance(value, bool):
        return _mask_debug_phone(value)
    return value


class AccountsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def list_accounts(self, query: AccountQuery) -> dict:
        total, items = self.repository.list(query)
        return {
            "total": total,
            "page": query.page,
            "items": [self._serialize(item) for item in items],
        }

    def get_account(self, account_id: int) -> dict | None:
        item = self.repository.get(account_id)
        return self._serialize(item) if item else None

    def get_credentials(self, account_id: int, scope: str | None = None) -> dict | None:
        """Return credential values only for the explicit credential endpoint."""
        item = self.repository.get(account_id)
        if not item:
            return None

        credentials = []
        for raw in item.credentials:
            if not isinstance(raw, dict):
                continue
            credential_scope = str(raw.get("scope") or "")
            if scope and credential_scope != scope:
                continue
            credentials.append({
                "scope": credential_scope,
                "provider_name": str(raw.get("provider_name") or ""),
                "credential_type": str(raw.get("credential_type") or ""),
                "key": str(raw.get("key") or ""),
                "value": str(raw.get("value") or ""),
                "is_primary": bool(raw.get("is_primary")),
                "source": str(raw.get("source") or ""),
            })
        credentials.sort(key=lambda value: (
            value["scope"],
            not value["is_primary"],
            value["provider_name"],
            value["key"],
        ))
        return {"items": credentials}

    def update_account(self, account_id: int, command: AccountUpdateCommand) -> dict | None:
        item = self.repository.update(account_id, command)
        return self._serialize(item) if item else None

    def delete_account(self, account_id: int) -> dict:
        return {"ok": self.repository.delete(account_id)}

    def import_accounts(self, platform: str, lines: list[str]) -> dict:
        parsed: list[AccountImportLine] = []
        csv_header: list[str] | None = None
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            if csv_header is None and "," in raw:
                try:
                    header_candidate = [item.strip().lower() for item in _parse_csv_row(raw)]
                except Exception:
                    header_candidate = []
                if "email" in header_candidate and "password" in header_candidate:
                    csv_header = header_candidate
                    continue
            if csv_header is not None:
                try:
                    values = _parse_csv_row(raw)
                except Exception:
                    values = []
                if values:
                    row = {
                        csv_header[index]: values[index]
                        for index in range(min(len(csv_header), len(values)))
                    }
                    email = str(row.get("email", "") or "").strip()
                    password = str(row.get("password", "") or "")
                    if email and password and "@" in email and " " not in email:
                        extra = {"account_source": "import", "import_method": "csv"}
                        cashier_url = str(row.get("cashier_url", "") or "").strip()
                        if cashier_url:
                            extra["cashier_url"] = cashier_url
                        parsed.append(AccountImportLine(email=email, password=password, extra=extra))
                        continue
            match = IMPORT_LINE_RE.match(raw)
            if not match:
                continue
            email = _decode_import_token(match.group("email"))
            password = _decode_import_token(match.group("password"))
            extra = {"account_source": "import", "import_method": "text"}
            payload = (match.group("extra") or "").strip()
            if payload:
                try:
                    decoded = json.loads(payload)
                    if isinstance(decoded, dict):
                        extra = {**decoded, "account_source": "import", "import_method": "text"}
                    elif decoded not in (None, ""):
                        extra["cashier_url"] = str(decoded)
                except Exception:
                    extra["cashier_url"] = _decode_import_token(payload)
            parsed.append(AccountImportLine(email=email, password=password, extra=extra))
        return {"created": self.repository.import_lines(platform, parsed)}

    def get_stats(self) -> dict:
        stats: AccountStats = self.repository.stats()
        return {
            "total": stats.total,
            "by_platform": stats.by_platform,
            "by_status": stats.by_status,
            "by_lifecycle_status": stats.by_lifecycle_status,
            "by_plan_state": stats.by_plan_state,
            "by_validity_status": stats.by_validity_status,
            "by_display_status": stats.by_display_status,
        }

    def get_filter_stats(self, platform: str = "chatgpt") -> dict:
        return self.repository.filter_stats(platform)

    @staticmethod
    def _serialize(item: AccountRecord) -> dict:
        # Normal list/detail responses expose only structural credential metadata.
        # Plaintext is available exclusively through the explicit, no-store
        # credential endpoint when the user asks to view or copy one account.
        safe_credentials = [
            _sanitize_debug_value(credential, credential_context=True)
            for credential in item.credentials
            if isinstance(credential, dict)
        ]
        safe_provider_accounts = [
            _sanitize_debug_value(provider)
            for provider in item.provider_accounts
            if isinstance(provider, dict)
        ]
        return {
            "id": item.id,
            "platform": item.platform,
            "email": item.email,
            "password": item.password,
            "user_id": item.user_id,
            "trial_end_time": item.trial_end_time,
            "cashier_url": item.cashier_url,
            "lifecycle_status": item.lifecycle_status,
            "validity_status": item.validity_status,
            "plan_state": item.plan_state,
            "plan_name": item.plan_name,
            "display_status": item.display_status,
            "overview": _sanitize_debug_value(item.overview),
            "display_summary": _sanitize_debug_value(item.display_summary),
            "account_view": item.account_view,
            "credentials": safe_credentials,
            "provider_accounts": safe_provider_accounts,
            "provider_resources": _sanitize_debug_value(item.provider_resources),
            "push_deliveries": item.push_deliveries,
            "created_at": serialize_datetime(item.created_at),
            "updated_at": serialize_datetime(item.updated_at),
        }
