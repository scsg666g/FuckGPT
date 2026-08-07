from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import re
from typing import Any

from sqlmodel import Session, delete, select

from core.account_check_settings import INVALID_CHECK_LIMIT
from core.datetime_utils import ensure_utc_datetime, serialize_datetime
from core.db import (
    AccountAuthCredentialModel,
    AccountCodexAuthModel,
    AccountModel,
    AccountSecurityProfileModel,
    AccountStatusModel,
    AccountSubscriptionModel,
    AccountUsageSnapshotModel,
    MailboxAccountLinkModel,
    MailboxResourceModel,
    ProviderAccountModel,
    ProviderResourceModel,
)


PLATFORM_CREDENTIAL_TYPES: dict[str, str] = {
    "access_token": "token",
    "refresh_token": "token",
    "firebase_refresh_token": "token",
    "session_token": "token",
    "session_cookie": "cookie",
    "id_token": "token",
    "client_id": "identifier",
    "client_secret": "secret",
    "workspace_id": "identifier",
    "workspace_slug": "identifier",
    "customer_id": "identifier",
    "referral_code": "identifier",
    "account_id": "identifier",
    "chatgpt_account_id": "identifier",
    "org_id": "identifier",
    "auth_token": "token",
    "accessToken": "token",
    "refreshToken": "token",
    "sessionToken": "token",
    "idToken": "token",
    "clientId": "identifier",
    "clientSecret": "secret",
    "workspaceId": "identifier",
    "accountId": "identifier",
    "orgId": "identifier",
    "authToken": "token",
    "cookies": "cookie",
    "cookie": "cookie",
    "api_key": "secret",
    "wos_session": "token",
    "sso": "cookie",
    "sso_rw": "cookie",
}

CODEX_CREDENTIAL_TYPES: dict[str, str] = {
    "codex_access_token": "token",
    "codex_refresh_token": "token",
    "codex_id_token": "token",
}

CODEX_METADATA_KEYS = {
    "codex_account_id",
    "codex_email",
    "codex_plan_type",
    "codex_expires_at",
    "codex_last_refresh",
    "codex_auth_path",
}

PRIMARY_TOKEN_WRITE_KEYS: dict[str, str] = {
    "cursor": "session_token",
    "chatgpt": "access_token",
    "kiro": "accessToken",
    "trae": "access_token",
    "blink": "firebase_refresh_token",
    "openblocklabs": "wos_session",
}

GENERIC_USAGE_KEYS = {
    "remaining_credits",
    "usage_total",
    "plan_credits",
    "next_reset_at",
    "days_until_reset",
    "prompt_credits_limit",
    "flow_action_credits_limit",
    "prompt_remaining_percent",
    "flow_action_remaining_percent",
    "usage_models",
    "usage_breakdowns",
    "quota_note",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_dict(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _preview_secret(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if len(text) <= 10:
        return "***"
    return f"{text[:6]}...{text[-4:]}"


def _dedupe_chips(*groups: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for item in group or []:
            chip = _text(item)
            if not chip or chip == "本地未切换" or chip in seen:
                continue
            seen.add(chip)
            result.append(chip)
    return result


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc_datetime(value)
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, timezone.utc) if timestamp > 0 else None
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.isdigit():
            return _parse_datetime(int(normalized))
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            return ensure_utc_datetime(datetime.fromisoformat(normalized))
        except ValueError:
            return None
    return None


def _timestamp(value: Any) -> int:
    if isinstance(value, datetime):
        return int(ensure_utc_datetime(value).timestamp())
    try:
        timestamp = int(float(value or 0))
    except (TypeError, ValueError):
        parsed = _parse_datetime(value)
        return int(parsed.timestamp()) if parsed else 0
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return max(timestamp, 0)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mask_phone_number(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    # Only trust the canonical masked form.  A full number with an arbitrary
    # asterisk appended must still be remasked before it reaches raw JSON.
    if re.fullmatch(r"\+?\d{1,4}\*{3,}\d{2,4}", text):
        return text
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) <= 8:
        return "***"
    if text.startswith("+"):
        return f"+{digits[:3]}****{digits[-4:]}"
    return f"{digits[:4]}****{digits[-4:]}"


def _is_sensitive_raw_key(key: str) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
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
    }:
        return True
    if compact in {"accesstoken", "refreshtoken", "idtoken", "sessiontoken", "authtoken", "apikey", "clientsecret"}:
        return True
    # Token accounting is usage data, not an authentication credential.
    # Keep common singular-token metric spellings while continuing to scrub
    # actual token values such as access_token and refresh_token.
    accounting_suffixes = (
        "_token_count",
        "_token_counts",
        "_token_used",
        "_token_usage",
        "_token_limit",
        "_token_remaining",
        "_token_total",
    )
    if normalized.endswith(accounting_suffixes):
        return False
    return normalized.endswith("_token") or "_token_" in normalized or "cookie" in normalized or "password" in normalized


def _is_phone_raw_key(key: str) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    compact = "".join(character for character in normalized if character.isalnum())
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
    parts = {part for part in normalized.split("_") if part}
    return bool(parts & {"mobile", "msisdn", "tel", "telephone"})


def _sanitize_phone_data(value: Any, *, key: str = "") -> Any:
    """Remove auth secrets and mask phone values before raw JSON or views."""

    phone_context = _is_phone_raw_key(key)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            child_key_text = str(child_key)
            if _is_sensitive_raw_key(child_key_text):
                continue
            child_context = child_key_text
            if phone_context and not _is_phone_raw_key(child_key_text):
                child_context = key
            sanitized[child_key_text] = _sanitize_phone_data(child_value, key=child_context)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_phone_data(item, key=key) for item in value]
    if phone_context and not isinstance(value, bool):
        return _mask_phone_number(value)
    return value


def _normalize_plan_state(value: Any) -> str:
    raw = _text(value).lower()
    if not raw:
        return ""
    if raw in {"trial", "trialing", "free_trial", "trial-active", "trial_active"}:
        return "trial"
    if raw in {"expired", "cancelled", "canceled", "inactive", "ended", "invalid", "banned"}:
        return "expired"
    if raw in {"free", "basic", "starter", "hobby"}:
        return "free"
    if raw in {"eligible", "trial_eligible"}:
        return "eligible"
    if any(
        token in raw
        for token in ("pro", "plus", "premium", "paid", "student", "team", "business", "enterprise", "member")
    ):
        return "subscribed"
    return raw


def _canonical_plan_type(platform: str, value: Any) -> str:
    raw = _text(value)
    lowered = raw.lower()
    if not lowered:
        return ""
    if platform == "chatgpt":
        if any(token in lowered for token in ("team", "enterprise", "business")):
            return "team"
        if any(token in lowered for token in ("plus", "pro", "premium", "paid")):
            return "plus"
        if any(token in lowered for token in ("free", "basic", "starter")):
            return "free"
    return lowered


def _derive_display_status(lifecycle_status: str, validity_status: str, plan_state: str) -> str:
    if validity_status == "deactivated" or lifecycle_status == "deactivated":
        return "deactivated"
    if validity_status == "invalid":
        return "invalid"
    if lifecycle_status == "expired" or plan_state == "expired":
        return "expired"
    if plan_state == "subscribed":
        return "subscribed"
    if plan_state == "trial":
        return "trial"
    if plan_state == "free" and lifecycle_status in {"trial", "subscribed", "expired"}:
        return "registered"
    return lifecycle_status or "registered"


def recover_lifecycle_status_for_valid_account(graph: dict[str, Any]) -> str:
    """Recover lifecycle after a formerly invalid account validates again."""

    overview = _safe_dict(graph.get("overview"))
    lifecycle_status = _text(overview.get("lifecycle_status") or graph.get("lifecycle_status"))
    plan_state = _normalize_plan_state(overview.get("plan_state") or graph.get("plan_state"))
    if lifecycle_status in {"invalid", "deactivated"}:
        if plan_state in {"trial", "subscribed", "expired"}:
            return plan_state
        return "registered"
    if lifecycle_status in {"trial", "subscribed", "expired"}:
        if plan_state == "free":
            return "registered"
        if plan_state in {"trial", "subscribed", "expired"}:
            return plan_state
    if lifecycle_status:
        return lifecycle_status
    return "registered"


def _infer_credential_type(key: str) -> str:
    if key in PLATFORM_CREDENTIAL_TYPES:
        return PLATFORM_CREDENTIAL_TYPES[key]
    if key in CODEX_CREDENTIAL_TYPES:
        return CODEX_CREDENTIAL_TYPES[key]
    lower = key.lower()
    if "cookie" in lower:
        return "cookie"
    if "token" in lower:
        return "token"
    if "secret" in lower:
        return "secret"
    if "client" in lower or "workspace" in lower or lower.endswith("_id"):
        return "identifier"
    return "credential"


def _default_primary_token_key(platform: str) -> str:
    return PRIMARY_TOKEN_WRITE_KEYS.get(platform, "access_token")


def _normalize_credential_rows(platform: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in items:
        key = _text(raw.get("key"))
        value = raw.get("value")
        if not key or value in (None, ""):
            continue
        scope = _text(raw.get("scope")) or ("codex" if key in CODEX_CREDENTIAL_TYPES else "platform")
        provider_name = _text(raw.get("provider_name")) or ("openai" if scope == "codex" else platform)
        normalized[(scope, provider_name, key)] = {
            "scope": scope,
            "provider_name": provider_name,
            "credential_type": _text(raw.get("credential_type")) or _infer_credential_type(key),
            "key": key,
            "value": _text(value),
            "is_primary": bool(raw.get("is_primary")),
            "source": _text(raw.get("source")),
            "metadata": _safe_dict(raw.get("metadata")),
        }

    platform_rows = [item for item in normalized.values() if item["scope"] == "platform"]
    primary_key = next((item["key"] for item in platform_rows if item["is_primary"]), "")
    if not primary_key and platform_rows:
        preferred = _default_primary_token_key(platform)
        primary_key = next((item["key"] for item in platform_rows if item["key"] == preferred), "")
    if primary_key:
        for item in platform_rows:
            item["is_primary"] = item["key"] == primary_key
    return list(normalized.values())


def _credential_rows_from_extra(
    platform: str,
    extra: dict[str, Any],
    *,
    primary_token: str = "",
    source: str = "account.extra",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    preferred_token_key = _default_primary_token_key(platform)
    if primary_token and extra.get(preferred_token_key) in (None, ""):
        rows.append(
            {
                "scope": "platform",
                "provider_name": platform,
                "credential_type": "token",
                "key": preferred_token_key,
                "value": primary_token,
                "is_primary": False,
                "source": source,
                "metadata": {},
            }
        )
    for key, credential_type in PLATFORM_CREDENTIAL_TYPES.items():
        if extra.get(key) not in (None, ""):
            rows.append(
                {
                    "scope": "platform",
                    "provider_name": platform,
                    "credential_type": credential_type,
                    "key": key,
                    "value": extra[key],
                    "is_primary": False,
                    "source": source,
                    "metadata": {},
                }
            )
    for key, credential_type in CODEX_CREDENTIAL_TYPES.items():
        if extra.get(key) not in (None, ""):
            rows.append(
                {
                    "scope": "codex",
                    "provider_name": "openai",
                    "credential_type": credential_type,
                    "key": key,
                    "value": extra[key],
                    "is_primary": key == "codex_access_token",
                    "source": source,
                    "metadata": {},
                }
            )
    return _normalize_credential_rows(platform, rows)


def _upsert_credentials(
    session: Session,
    *,
    account_id: int,
    platform: str,
    rows: list[dict[str, Any]],
) -> bool:
    normalized = _normalize_credential_rows(platform, rows)
    if not normalized:
        return False
    now = _utcnow()
    primary_scopes = {
        (item["scope"], item["provider_name"])
        for item in normalized
        if item.get("is_primary")
    }
    for scope, provider_name in primary_scopes:
        existing_primary = session.exec(
            select(AccountAuthCredentialModel)
            .where(AccountAuthCredentialModel.account_id == account_id)
            .where(AccountAuthCredentialModel.scope == scope)
            .where(AccountAuthCredentialModel.provider_name == provider_name)
            .where(AccountAuthCredentialModel.is_primary == True)  # noqa: E712
        ).all()
        for item in existing_primary:
            item.is_primary = False
            item.updated_at = now
            session.add(item)

    for item in normalized:
        model = session.exec(
            select(AccountAuthCredentialModel)
            .where(AccountAuthCredentialModel.account_id == account_id)
            .where(AccountAuthCredentialModel.scope == item["scope"])
            .where(AccountAuthCredentialModel.provider_name == item["provider_name"])
            .where(AccountAuthCredentialModel.key == item["key"])
        ).first()
        if model is None:
            model = AccountAuthCredentialModel(
                account_id=account_id,
                scope=item["scope"],
                provider_name=item["provider_name"],
                key=item["key"],
            )
        model.credential_type = item["credential_type"]
        model.value = item["value"]
        model.is_primary = bool(item.get("is_primary"))
        model.source = item.get("source", "")
        model.set_metadata(item.get("metadata") or {})
        model.updated_at = now
        session.add(model)
    session.flush()
    return any(item["scope"] == "codex" for item in normalized)


def _provider_accounts_from_extra(extra: dict[str, Any]) -> list[dict[str, Any]]:
    items = _safe_list(extra.get("provider_accounts"))
    identity = _safe_dict(extra.get("identity"))
    if isinstance(identity.get("provider_account"), dict):
        items.append(identity["provider_account"])
    identity_mailbox = _safe_dict(identity.get("mailbox"))
    mailbox = identity_mailbox or _safe_dict(extra.get("verification_mailbox"))
    if mailbox:
        items.append(
            {
                "provider_type": "mailbox",
                "provider_name": mailbox.get("provider"),
                "login_identifier": mailbox.get("email"),
                "display_name": mailbox.get("email"),
                "metadata": {"account_id": mailbox.get("account_id")},
            }
        )

    normalized: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in items:
        item = _safe_dict(raw)
        provider_type = _text(item.get("provider_type") or "mailbox") or "mailbox"
        provider_name = _text(item.get("provider_name") or item.get("provider"))
        login_identifier = _text(item.get("login_identifier") or item.get("email") or item.get("username"))
        key = (provider_type, provider_name, login_identifier)
        previous = normalized.get(key, {})
        credentials = {**_safe_dict(previous.get("credentials")), **_safe_dict(item.get("credentials"))}
        metadata = {**_safe_dict(previous.get("metadata")), **_safe_dict(item.get("metadata"))}
        for field in ("email", "username", "account_id", "api_url", "login_url", "auth_type"):
            if item.get(field) not in (None, ""):
                metadata.setdefault(field, item[field])
        normalized[key] = {
            "provider_type": provider_type,
            "provider_name": provider_name,
            "login_identifier": login_identifier,
            "display_name": _text(item.get("display_name") or login_identifier or provider_name),
            "credentials": credentials,
            "metadata": metadata,
        }
    return list(normalized.values())


def _provider_resources_from_extra(extra: dict[str, Any]) -> list[dict[str, Any]]:
    items = _safe_list(extra.get("provider_resources"))
    identity = _safe_dict(extra.get("identity"))
    if isinstance(identity.get("provider_resource"), dict):
        items.append(identity["provider_resource"])
    identity_mailbox = _safe_dict(identity.get("mailbox"))
    mailbox = identity_mailbox or _safe_dict(extra.get("verification_mailbox"))
    if mailbox:
        items.append(
            {
                "provider_type": "mailbox",
                "provider_name": mailbox.get("provider"),
                "resource_type": "mailbox",
                "resource_identifier": mailbox.get("account_id"),
                "handle": mailbox.get("email"),
                "display_name": mailbox.get("email"),
                "metadata": {"account_id": mailbox.get("account_id"), "email": mailbox.get("email")},
            }
        )

    normalized: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in items:
        item = _safe_dict(raw)
        provider_type = _text(item.get("provider_type") or "mailbox") or "mailbox"
        provider_name = _text(item.get("provider_name") or item.get("provider"))
        resource_type = _text(item.get("resource_type") or "resource") or "resource"
        resource_identifier = _text(
            item.get("resource_identifier") or item.get("account_id") or item.get("external_id") or item.get("id")
        )
        handle = _text(item.get("handle") or item.get("email") or item.get("address"))
        metadata = _safe_dict(item.get("metadata"))
        for field in ("email", "account_id", "address", "api_url"):
            if item.get(field) not in (None, ""):
                metadata.setdefault(field, item[field])
        key = (provider_type, provider_name, resource_type, resource_identifier or handle)
        normalized[key] = {
            "provider_type": provider_type,
            "provider_name": provider_name,
            "resource_type": resource_type,
            "resource_identifier": resource_identifier,
            "handle": handle,
            "display_name": _text(item.get("display_name") or handle or resource_identifier),
            "metadata": metadata,
        }
    return list(normalized.values())


def _merge_provider_accounts(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    prefer_existing: bool,
) -> list[dict[str, Any]]:
    ordered = list(incoming) + list(existing) if prefer_existing else list(existing) + list(incoming)
    return _provider_accounts_from_extra({"provider_accounts": ordered})


def _merge_provider_resources(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    prefer_existing: bool,
) -> list[dict[str, Any]]:
    ordered = list(incoming) + list(existing) if prefer_existing else list(existing) + list(incoming)
    return _provider_resources_from_extra({"provider_resources": ordered})


def _replace_provider_relations(
    session: Session,
    *,
    account_id: int,
    provider_accounts: list[dict[str, Any]],
    provider_resources: list[dict[str, Any]],
) -> None:
    session.exec(delete(ProviderResourceModel).where(ProviderResourceModel.account_id == account_id))
    session.exec(delete(ProviderAccountModel).where(ProviderAccountModel.account_id == account_id))
    now = _utcnow()
    for item in provider_accounts:
        model = ProviderAccountModel(
            account_id=account_id,
            provider_type=item["provider_type"],
            provider_name=item["provider_name"],
            login_identifier=item["login_identifier"],
            display_name=item["display_name"],
            updated_at=now,
        )
        model.set_credentials(item.get("credentials") or {})
        model.set_metadata(item.get("metadata") or {})
        session.add(model)
    for item in provider_resources:
        model = ProviderResourceModel(
            account_id=account_id,
            provider_type=item["provider_type"],
            provider_name=item["provider_name"],
            resource_type=item["resource_type"],
            resource_identifier=item["resource_identifier"],
            handle=item["handle"],
            display_name=item["display_name"],
            updated_at=now,
        )
        model.set_metadata(item.get("metadata") or {})
        session.add(model)


def _extract_usage_payload(summary: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("chatgpt_usage", "wham_usage", "usage"):
        value = summary.get(key)
        if isinstance(value, dict) and value:
            return _sanitize_phone_data(value)
    usage_summary = summary.get("usage_summary")
    if isinstance(usage_summary, dict) and usage_summary:
        return _sanitize_phone_data(usage_summary)
    generic = {key: summary[key] for key in GENERIC_USAGE_KEYS if summary.get(key) not in (None, "", [], {})}
    return _sanitize_phone_data(generic) if generic else None


def _usage_window(raw: dict[str, Any]) -> dict[str, Any]:
    rate_limit = _safe_dict(raw.get("rate_limit"))
    return _safe_dict(rate_limit.get("primary_window")) or _safe_dict(raw.get("primary_window"))


def _upsert_subscription(
    session: Session,
    *,
    account_id: int,
    platform: str,
    lifecycle_status: str,
    summary: dict[str, Any],
    usage_raw: dict[str, Any] | None,
) -> AccountSubscriptionModel | None:
    usage_plan = _text((usage_raw or {}).get("plan_type"))
    direct_plan = _text(
        summary.get("plan_type")
        or summary.get("plan")
        or summary.get("plan_name")
        or summary.get("membership_type")
        or summary.get("individual_membership_type")
        or summary.get("subscription_status")
    )
    plan_candidate = usage_plan or direct_plan
    has_update = any(
        key in summary
        for key in (
            "plan_type",
            "plan",
            "plan_name",
            "membership_type",
            "individual_membership_type",
            "subscription_status",
            "plan_state",
            "trial_end_time",
            "cashier_url",
            "check_source",
            "subscription_source",
            "subscription_raw",
            "subscription",
        )
    ) or bool(usage_plan)
    model = session.get(AccountSubscriptionModel, account_id)
    if model is None and not has_update:
        return None
    if model is None:
        model = AccountSubscriptionModel(account_id=account_id)

    incoming_source = _text(
        summary.get("check_source") or summary.get("subscription_source") or summary.get("source")
    )
    if platform == "chatgpt" and usage_plan and not incoming_source:
        incoming_source = "backend-api/wham/usage"
    keep_cached_wham_plan = bool(
        platform == "chatgpt"
        and not usage_plan
        and direct_plan
        and "wham/usage" not in incoming_source.lower()
        and model.plan_type
        and "wham/usage" in _text(model.source).lower()
    )
    if keep_cached_wham_plan:
        plan_candidate = model.plan_type

    if plan_candidate:
        model.plan_type = _canonical_plan_type(platform, plan_candidate)
    explicit_state = _normalize_plan_state(summary.get("plan_state"))
    # A fresh wham/usage plan is authoritative, including an explicit
    # downgrade to free.  A stale plan_state from another source must not win.
    if usage_plan or keep_cached_wham_plan:
        model.plan_state = _normalize_plan_state(model.plan_type) or "unknown"
    elif explicit_state:
        model.plan_state = explicit_state
    elif plan_candidate:
        model.plan_state = _normalize_plan_state(model.plan_type) or "unknown"
    elif lifecycle_status in {"trial", "subscribed", "expired"} and model.plan_state == "unknown":
        model.plan_state = lifecycle_status
    if "trial_end_time" in summary:
        model.trial_end_time = _timestamp(summary.get("trial_end_time"))
    if "cashier_url" in summary:
        model.cashier_url = _text(summary.get("cashier_url"))
    if incoming_source and not keep_cached_wham_plan:
        model.source = incoming_source
    raw = summary.get("subscription_raw")
    if not isinstance(raw, dict):
        raw = summary.get("subscription")
    if isinstance(raw, dict):
        model.set_raw(_sanitize_phone_data(raw))
    if "checked_at" in summary and not keep_cached_wham_plan:
        model.checked_at = _parse_datetime(summary.get("checked_at"))
    model.updated_at = _utcnow()
    session.add(model)
    return model


def _upsert_security(
    session: Session,
    *,
    account_id: int,
    summary: dict[str, Any],
) -> AccountSecurityProfileModel | None:
    profile = _safe_dict(summary.get("profile")) or _safe_dict(summary.get("me"))
    security_raw = summary.get("security_raw")
    if not isinstance(security_raw, dict):
        security_raw = summary.get("security")
    amr = summary.get("amr") if isinstance(summary.get("amr"), list) else None
    if amr is None and isinstance(profile.get("amr"), list):
        amr = profile.get("amr")
    phone_value = (
        summary.get("phone_number_masked")
        or summary.get("phone_number")
        or profile.get("phone_number_masked")
        or profile.get("phone_number")
    )
    has_update = (
        any(
            key in summary
            for key in (
                "phone_bound",
                "phone_number_masked",
                "phone_number",
                "mfa_enabled",
                "amr",
                "registration_auth_mode",
                "account_source",
                "import_method",
                "registration_executor",
            )
        )
        or bool(profile)
        or isinstance(security_raw, dict)
    )
    model = session.get(AccountSecurityProfileModel, account_id)
    if model is None and not has_update:
        return None
    if model is None:
        model = AccountSecurityProfileModel(account_id=account_id)

    if phone_value not in (None, ""):
        model.phone_number_masked = _mask_phone_number(phone_value)
        model.phone_bound = True
    if "phone_bound" in summary:
        model.phone_bound = bool(summary.get("phone_bound"))
        if not model.phone_bound:
            model.phone_number_masked = ""
    if "mfa_enabled" in summary:
        model.mfa_enabled = bool(summary.get("mfa_enabled"))
    elif "mfa_enabled" in profile:
        model.mfa_enabled = bool(profile.get("mfa_enabled"))
    elif isinstance(amr, list):
        model.mfa_enabled = any(
            _text(method).lower() not in {"pwd", "password", "email", "otp_email"}
            for method in amr
        )
    if isinstance(amr, list):
        model.set_amr([_text(item) for item in amr if _text(item)])

    # Security observations are partial.  Merge them into the last sanitized
    # raw payload so durable metadata such as registration_auth_mode is not
    # erased by a later profile-only check.
    raw_payload = _safe_dict(model.get_raw())
    if isinstance(security_raw, dict):
        incoming_security_raw = dict(security_raw)
        incoming_raw_profile = incoming_security_raw.pop("profile", None)
        raw_payload.update(incoming_security_raw)
        if isinstance(incoming_raw_profile, dict):
            merged_profile = _safe_dict(raw_payload.get("profile"))
            merged_profile.update(incoming_raw_profile)
            raw_payload["profile"] = merged_profile
    if profile:
        merged_profile = _safe_dict(raw_payload.get("profile"))
        merged_profile.update(profile)
        raw_payload["profile"] = merged_profile
    if summary.get("registration_auth_mode") not in (None, ""):
        raw_payload["registration_auth_mode"] = summary.get("registration_auth_mode")
    if "mfa_enabled" in summary or isinstance(amr, list):
        raw_payload["_mfa_observed"] = True
    if any(key in summary for key in ("phone_bound", "phone_number_masked", "phone_number")):
        raw_payload["_phone_observed"] = True
    for key in (
        "account_source",
        "import_method",
        "registration_executor",
        "deactivation_reason",
        "deactivation_error",
        "deactivation_detected_at",
    ):
        if key in summary:
            if summary.get(key) in (None, ""):
                raw_payload.pop(key, None)
            else:
                raw_payload[key] = summary.get(key)
    if raw_payload:
        model.set_raw(_sanitize_phone_data(raw_payload))
    if "checked_at" in summary:
        model.checked_at = _parse_datetime(summary.get("checked_at"))
    model.updated_at = _utcnow()
    session.add(model)
    return model


def _append_usage_snapshot(
    session: Session,
    *,
    account_id: int,
    platform: str,
    summary: dict[str, Any],
    raw: dict[str, Any] | None,
    subscription: AccountSubscriptionModel | None,
) -> AccountUsageSnapshotModel | None:
    if not raw:
        return None
    window = _usage_window(raw)
    rate_limit = _safe_dict(raw.get("rate_limit"))
    # Some successful wham responses contain quota windows but omit
    # ``plan_type``.  In that case inherit the already reconciled subscription
    # value instead of falling back to a weaker /me plan and creating an
    # internally inconsistent view.
    plan_type = _canonical_plan_type(
        platform,
        raw.get("plan_type")
        or (subscription.plan_type if subscription else "")
        or summary.get("plan_type")
        or summary.get("plan"),
    )
    used_percent = _float_or_none(raw.get("used_percent"))
    if used_percent is None:
        used_percent = _float_or_none(window.get("used_percent"))
    limit_reached = bool(
        raw.get("limit_reached")
        or rate_limit.get("limit_reached")
        or window.get("limit_reached")
    )
    reset_at = _timestamp(raw.get("reset_at") or window.get("reset_at"))
    credits = _safe_dict(_sanitize_phone_data(_safe_dict(raw.get("credits"))))
    checked_at = _parse_datetime(summary.get("checked_at")) or _utcnow()
    model = AccountUsageSnapshotModel(
        account_id=account_id,
        provider=_text(summary.get("usage_provider")) or platform,
        plan_type=plan_type,
        used_percent=used_percent,
        limit_reached=limit_reached,
        reset_at=reset_at,
        checked_at=checked_at,
    )
    model.set_credits(credits)
    model.set_raw(_sanitize_phone_data(raw))
    session.add(model)
    return model


def _upsert_codex_auth(
    session: Session,
    *,
    account_id: int,
    updates: dict[str, Any],
    credentials_touched: bool,
) -> AccountCodexAuthModel | None:
    model = session.get(AccountCodexAuthModel, account_id)
    if model is None and not updates and not credentials_touched:
        return None
    if model is None:
        model = AccountCodexAuthModel(account_id=account_id)

    if "codex_email" in updates:
        model.codex_email = _text(updates.get("codex_email"))
    if "codex_account_id" in updates:
        model.codex_account_id = _text(updates.get("codex_account_id"))
    if "codex_plan_type" in updates:
        model.codex_plan_type = _text(updates.get("codex_plan_type")).lower()
    if "codex_auth_path" in updates:
        model.auth_path = _text(updates.get("codex_auth_path"))
    if "codex_expires_at" in updates:
        model.expires_at = _parse_datetime(updates.get("codex_expires_at"))
    if "codex_last_refresh" in updates:
        model.last_refresh = _parse_datetime(updates.get("codex_last_refresh"))

    credentials = session.exec(
        select(AccountAuthCredentialModel)
        .where(AccountAuthCredentialModel.account_id == account_id)
        .where(AccountAuthCredentialModel.scope == "codex")
    ).all()
    populated_keys = {item.key for item in credentials if item.value}
    model.has_access_token = bool(populated_keys & {"codex_access_token", "access_token"})
    model.has_refresh_token = bool(populated_keys & {"codex_refresh_token", "refresh_token"})
    model.updated_at = _utcnow()
    session.add(model)
    return model


def _upsert_status(
    session: Session,
    *,
    account_id: int,
    lifecycle_status: str | None,
    summary: dict[str, Any],
    subscription: AccountSubscriptionModel | None,
) -> AccountStatusModel:
    model = session.get(AccountStatusModel, account_id)
    if model is None:
        model = AccountStatusModel(account_id=account_id)

    if lifecycle_status not in (None, ""):
        model.lifecycle_status = _text(lifecycle_status) or "registered"
    elif summary.get("lifecycle_status") not in (None, ""):
        model.lifecycle_status = _text(summary.get("lifecycle_status")) or "registered"
    explicit_validity_status = _text(summary.get("validity_status"))
    if explicit_validity_status == "deactivated":
        model.validity_status = "deactivated"
    elif "valid" in summary:
        model.validity_status = "valid" if bool(summary.get("valid")) else "invalid"
        if bool(summary.get("valid")):
            model.invalid_check_count = 0
            if "last_error" not in summary and "check_error" not in summary:
                model.last_error = ""
        elif bool(summary.get("_track_invalid_attempt")):
            model.invalid_check_count = min(max(int(model.invalid_check_count or 0), 0) + 1, 2)
    elif explicit_validity_status:
        model.validity_status = explicit_validity_status or "unknown"
    elif model.lifecycle_status in {"invalid", "deactivated"}:
        model.validity_status = model.lifecycle_status
    if model.validity_status == "deactivated":
        model.invalid_check_count = max(int(model.invalid_check_count or 0), INVALID_CHECK_LIMIT)
    if "remote_email" in summary:
        model.remote_email = _text(summary.get("remote_email"))
    if "region" in summary:
        model.region = _text(summary.get("region"))
    if "checked_at" in summary:
        model.checked_at = _parse_datetime(summary.get("checked_at"))
    if "last_error" in summary or "check_error" in summary:
        model.last_error = _text(summary.get("last_error") or summary.get("check_error"))

    plan_state = subscription.plan_state if subscription else "unknown"
    observed_usage = _extract_usage_payload(summary)
    observed_plan = bool(_text((observed_usage or {}).get("plan_type"))) or any(
        summary.get(key) not in (None, "")
        for key in (
            "plan_type",
            "plan",
            "plan_name",
            "membership_type",
            "individual_membership_type",
            "subscription_status",
            "plan_state",
        )
    )
    if model.validity_status == "valid" and observed_plan:
        if plan_state == "free" and model.lifecycle_status in {"trial", "subscribed", "expired"}:
            model.lifecycle_status = "registered"
        elif plan_state == "subscribed" and model.lifecycle_status in {"trial", "expired"}:
            model.lifecycle_status = "subscribed"
        elif plan_state == "trial" and model.lifecycle_status in {"subscribed", "expired"}:
            model.lifecycle_status = "trial"
        elif plan_state == "expired":
            model.lifecycle_status = "expired"
    model.display_status = _derive_display_status(
        model.lifecycle_status,
        model.validity_status,
        plan_state,
    )
    model.updated_at = _utcnow()
    session.add(model)
    return model


def _apply_structured_updates(
    session: Session,
    model: AccountModel,
    *,
    lifecycle_status: str | None,
    summary: dict[str, Any] | None,
    credential_rows: list[dict[str, Any]] | None = None,
    codex_updates: dict[str, Any] | None = None,
) -> None:
    account_id = int(model.id or 0)
    if account_id <= 0:
        return
    payload = _safe_dict(summary)
    credentials_touched = _upsert_credentials(
        session,
        account_id=account_id,
        platform=model.platform,
        rows=credential_rows or [],
    )
    usage_raw = _extract_usage_payload(payload)
    existing_status = session.get(AccountStatusModel, account_id)
    effective_lifecycle = _text(
        lifecycle_status
        or payload.get("lifecycle_status")
        or (existing_status.lifecycle_status if existing_status else "")
    ) or "registered"
    subscription = _upsert_subscription(
        session,
        account_id=account_id,
        platform=model.platform,
        lifecycle_status=effective_lifecycle,
        summary=payload,
        usage_raw=usage_raw,
    )
    _upsert_security(session, account_id=account_id, summary=payload)
    _append_usage_snapshot(
        session,
        account_id=account_id,
        platform=model.platform,
        summary=payload,
        raw=usage_raw,
        subscription=subscription or session.get(AccountSubscriptionModel, account_id),
    )
    _upsert_codex_auth(
        session,
        account_id=account_id,
        updates=_safe_dict(codex_updates),
        credentials_touched=credentials_touched,
    )
    _upsert_status(
        session,
        account_id=account_id,
        lifecycle_status=lifecycle_status,
        summary=payload,
        subscription=subscription or session.get(AccountSubscriptionModel, account_id),
    )


def _serialize_status(model: AccountStatusModel) -> dict[str, Any]:
    return {
        "lifecycle_status": model.lifecycle_status,
        "validity_status": model.validity_status,
        "display_status": model.display_status,
        "remote_email": model.remote_email,
        "region": model.region,
        "checked_at": model.checked_at,
        "last_error": model.last_error,
        "invalid_check_count": int(model.invalid_check_count or 0),
        "updated_at": model.updated_at,
    }


def _serialize_subscription(model: AccountSubscriptionModel) -> dict[str, Any]:
    return {
        "plan_type": model.plan_type,
        "plan_state": model.plan_state,
        "source": model.source,
        "trial_end_time": int(model.trial_end_time or 0),
        "cashier_url": model.cashier_url,
        "raw": _json_dict(model.raw_json),
        "checked_at": model.checked_at,
        "updated_at": model.updated_at,
    }


def _serialize_security(model: AccountSecurityProfileModel) -> dict[str, Any]:
    return {
        "phone_bound": bool(model.phone_bound),
        "phone_number_masked": _mask_phone_number(model.phone_number_masked),
        "mfa_enabled": bool(model.mfa_enabled),
        "amr": model.get_amr(),
        "raw": _sanitize_phone_data(model.get_raw()),
        "checked_at": model.checked_at,
        "updated_at": model.updated_at,
    }


def _serialize_usage(model: AccountUsageSnapshotModel) -> dict[str, Any]:
    return {
        "id": int(model.id or 0),
        "provider": model.provider,
        "plan_type": model.plan_type,
        "used_percent": model.used_percent,
        "limit_reached": bool(model.limit_reached),
        "reset_at": int(model.reset_at or 0),
        "credits": _safe_dict(_sanitize_phone_data(model.get_credits())),
        "raw": _sanitize_phone_data(model.get_raw()),
        "checked_at": model.checked_at,
    }


def _serialize_codex(model: AccountCodexAuthModel) -> dict[str, Any]:
    return {
        "codex_email": model.codex_email,
        "codex_account_id": model.codex_account_id,
        "codex_plan_type": model.codex_plan_type,
        "auth_path": model.auth_path,
        "expires_at": model.expires_at,
        "last_refresh": model.last_refresh,
        "has_access_token": bool(model.has_access_token),
        "has_refresh_token": bool(model.has_refresh_token),
        "updated_at": model.updated_at,
    }


def _serialize_credential(model: AccountAuthCredentialModel) -> dict[str, Any]:
    return {
        "id": int(model.id or 0),
        "scope": model.scope,
        "provider_name": model.provider_name,
        "credential_type": model.credential_type,
        "key": model.key,
        "value": model.value,
        "preview": _preview_secret(model.value),
        "is_primary": bool(model.is_primary),
        "source": model.source,
        "metadata": model.get_metadata(),
    }


def _serialize_provider_account(model: ProviderAccountModel) -> dict[str, Any]:
    credentials = model.get_credentials()
    return {
        "id": int(model.id or 0),
        "provider_type": model.provider_type,
        "provider_name": model.provider_name,
        "login_identifier": model.login_identifier,
        "display_name": model.display_name,
        "credentials": credentials,
        "credential_previews": {key: _preview_secret(value) for key, value in credentials.items()},
        "metadata": model.get_metadata(),
    }


def _serialize_provider_resource(model: ProviderResourceModel) -> dict[str, Any]:
    return {
        "id": int(model.id or 0),
        "provider_type": model.provider_type,
        "provider_name": model.provider_name,
        "resource_type": model.resource_type,
        "resource_identifier": model.resource_identifier,
        "handle": model.handle,
        "display_name": model.display_name,
        "metadata": model.get_metadata(),
    }


def _canonical_mailboxes(session: Session, account_ids: list[int]) -> dict[int, dict[str, Any]]:
    links = session.exec(
        select(MailboxAccountLinkModel).where(MailboxAccountLinkModel.account_id.in_(account_ids))
    ).all()
    resource_ids = [int(link.resource_id) for link in links]
    if not resource_ids:
        return {}
    resources = {
        int(item.id or 0): item
        for item in session.exec(
            select(MailboxResourceModel).where(MailboxResourceModel.id.in_(resource_ids))
        ).all()
    }
    result: dict[int, dict[str, Any]] = {}
    for link in links:
        resource = resources.get(int(link.resource_id))
        account_id = int(link.account_id or 0)
        if not resource or account_id <= 0:
            continue
        result[account_id] = {
            "provider_type": "mailbox",
            "provider_name": resource.provider_name,
            "resource_type": "mailbox",
            "resource_identifier": resource.resource_identifier,
            "handle": resource.address,
            "display_name": resource.address,
            "metadata": {
                "account_id": resource.resource_identifier,
                "email": resource.address,
            },
        }
    return result


def _synthesize_overview(graph: dict[str, Any]) -> dict[str, Any]:
    status = _safe_dict(graph.get("status"))
    subscription = _safe_dict(graph.get("subscription"))
    security = _safe_dict(graph.get("security"))
    usage = _safe_dict(graph.get("usage"))
    codex = _safe_dict(graph.get("codex"))
    overview: dict[str, Any] = {
        "lifecycle_status": _text(status.get("lifecycle_status")) or "registered",
        "validity_status": _text(status.get("validity_status")) or "unknown",
        "display_status": _text(status.get("display_status")) or "registered",
        "remote_email": _text(status.get("remote_email")),
        "region": _text(status.get("region")),
        "checked_at": status.get("checked_at"),
        "check_error": _text(status.get("last_error")),
        "invalid_check_count": int(status.get("invalid_check_count") or 0),
        "plan_type": _text(subscription.get("plan_type")),
        "plan": _text(subscription.get("plan_type")),
        "plan_name": _text(subscription.get("plan_type")),
        "plan_state": _text(subscription.get("plan_state")) or "unknown",
        "subscription_source": _text(subscription.get("source")),
        "trial_end_time": int(subscription.get("trial_end_time") or 0),
        "cashier_url": _text(subscription.get("cashier_url")),
    }
    if overview["validity_status"] in {"valid", "invalid", "deactivated"}:
        overview["valid"] = overview["validity_status"] == "valid"

    if security:
        overview.update(
            {
                "phone_bound": bool(security.get("phone_bound")),
                "phone_number_masked": _mask_phone_number(security.get("phone_number_masked")),
                "mfa_enabled": bool(security.get("mfa_enabled")),
                "amr": _safe_list(security.get("amr")),
            }
        )
        security_raw = _safe_dict(security.get("raw"))
        if isinstance(security_raw.get("profile"), dict):
            overview["profile"] = _sanitize_phone_data(security_raw["profile"])
        for key in (
            "registration_auth_mode",
            "account_source",
            "import_method",
            "registration_executor",
            "last_login_at",
            "last_login_status",
            "last_login_auth_mode",
            "last_login_error",
            "last_login_failure_code",
            "deactivation_reason",
            "deactivation_error",
            "deactivation_detected_at",
        ):
            if security_raw.get(key):
                overview[key] = security_raw[key]

    if usage:
        raw = _safe_dict(usage.get("raw"))
        if usage.get("provider") == "chatgpt":
            overview["chatgpt_usage"] = raw
        else:
            overview["usage_summary"] = raw
        for key in GENERIC_USAGE_KEYS:
            if key in raw:
                overview[key] = raw[key]

    if codex:
        overview.update(
            {
                "codex_email": _text(codex.get("codex_email")),
                "codex_account_id": _text(codex.get("codex_account_id")),
                "codex_plan_type": _text(codex.get("codex_plan_type")),
                "codex_auth_path": _text(codex.get("auth_path")),
                "codex_expires_at": codex.get("expires_at"),
                "codex_last_refresh": codex.get("last_refresh"),
            }
        )

    chips: list[str] = []
    if overview.get("plan_name"):
        chips.append(str(overview["plan_name"]).title())
    if overview.get("phone_bound"):
        chips.append("已绑手机")
    overview["chips"] = _dedupe_chips(chips)
    return overview


def load_account_graphs(session: Session, account_ids: list[int]) -> dict[int, dict[str, Any]]:
    normalized_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids if int(account_id or 0) > 0))
    if not normalized_ids:
        return {}
    graphs: dict[int, dict[str, Any]] = {
        account_id: {
            "status": {},
            "subscription": {},
            "security": {},
            "usage": {},
            "codex": {},
            "overview": {},
            "credentials": [],
            "provider_accounts": [],
            "provider_resources": [],
        }
        for account_id in normalized_ids
    }

    for item in session.exec(select(AccountStatusModel).where(AccountStatusModel.account_id.in_(normalized_ids))).all():
        graphs[int(item.account_id)]["status"] = _serialize_status(item)
    for item in session.exec(
        select(AccountSubscriptionModel).where(AccountSubscriptionModel.account_id.in_(normalized_ids))
    ).all():
        graphs[int(item.account_id)]["subscription"] = _serialize_subscription(item)
    for item in session.exec(
        select(AccountSecurityProfileModel).where(AccountSecurityProfileModel.account_id.in_(normalized_ids))
    ).all():
        graphs[int(item.account_id)]["security"] = _serialize_security(item)
    usage_rows = session.exec(
        select(AccountUsageSnapshotModel)
        .where(AccountUsageSnapshotModel.account_id.in_(normalized_ids))
        .order_by(AccountUsageSnapshotModel.checked_at.desc(), AccountUsageSnapshotModel.id.desc())
    ).all()
    for item in usage_rows:
        account_id = int(item.account_id)
        if not graphs[account_id]["usage"]:
            graphs[account_id]["usage"] = _serialize_usage(item)
    for item in session.exec(
        select(AccountCodexAuthModel).where(AccountCodexAuthModel.account_id.in_(normalized_ids))
    ).all():
        graphs[int(item.account_id)]["codex"] = _serialize_codex(item)
    for item in session.exec(
        select(AccountAuthCredentialModel).where(AccountAuthCredentialModel.account_id.in_(normalized_ids))
    ).all():
        graphs[int(item.account_id)]["credentials"].append(_serialize_credential(item))
    for item in session.exec(select(ProviderAccountModel).where(ProviderAccountModel.account_id.in_(normalized_ids))).all():
        graphs[int(item.account_id)]["provider_accounts"].append(_serialize_provider_account(item))
    for item in session.exec(select(ProviderResourceModel).where(ProviderResourceModel.account_id.in_(normalized_ids))).all():
        graphs[int(item.account_id)]["provider_resources"].append(_serialize_provider_resource(item))

    canonical_mailboxes = _canonical_mailboxes(session, normalized_ids)
    for account_id, graph in graphs.items():
        status = _safe_dict(graph.get("status"))
        subscription = _safe_dict(graph.get("subscription"))
        graph["lifecycle_status"] = _text(status.get("lifecycle_status")) or "registered"
        graph["validity_status"] = _text(status.get("validity_status")) or "unknown"
        graph["plan_state"] = _text(subscription.get("plan_state")) or "unknown"
        graph["plan_name"] = _text(subscription.get("plan_type"))
        graph["display_status"] = _text(status.get("display_status")) or graph["lifecycle_status"]
        graph["verification_mailbox"] = canonical_mailboxes.get(account_id) or next(
            (
                resource
                for resource in graph["provider_resources"]
                if resource.get("resource_type") == "mailbox"
            ),
            None,
        )
        graph["overview"] = _synthesize_overview(graph)
    return graphs


def _graph_for_account(session: Session, account_id: int) -> dict[str, Any]:
    return load_account_graphs(session, [account_id]).get(account_id, {})


def sync_account_graph(session: Session, model: AccountModel) -> None:
    _apply_structured_updates(
        session,
        model,
        lifecycle_status=None,
        summary=None,
    )


def sync_platform_account_graph(session: Session, model: AccountModel, account: Any) -> None:
    account_id = int(model.id or 0)
    if account_id <= 0:
        return
    current = _graph_for_account(session, account_id)
    extra = _safe_dict(getattr(account, "extra", {}) or {})
    summary = _safe_dict(extra.get("account_overview"))
    for key in (
        "valid",
        "validity_status",
        "checked_at",
        "check_source",
        "subscription_source",
        "remote_email",
        "region",
        "plan",
        "plan_name",
        "plan_type",
        "plan_state",
        "phone_bound",
        "phone_number_masked",
        "phone_number",
        "mfa_enabled",
        "amr",
        "chatgpt_usage",
        "wham_usage",
        "usage",
        "usage_summary",
        "usage_provider",
        "subscription",
        "subscription_raw",
        "security",
        "security_raw",
        "last_error",
        "check_error",
    ):
        if key in extra:
            summary[key] = extra[key]
    if getattr(account, "trial_end_time", 0) or "trial_end_time" in extra:
        summary["trial_end_time"] = int(getattr(account, "trial_end_time", 0) or extra.get("trial_end_time") or 0)
    if "cashier_url" in extra:
        summary["cashier_url"] = extra.get("cashier_url")
    if getattr(account, "region", ""):
        summary["region"] = getattr(account, "region", "")
    for key in ("profile", "registration_auth_mode", "account_source", "import_method", "registration_executor"):
        if key in extra and key not in summary:
            summary[key] = extra[key]
    profile = _safe_dict(summary.get("profile"))
    if profile.get("email") and not summary.get("remote_email"):
        summary["remote_email"] = profile.get("email")
    if profile.get("plan_type") and not any(
        summary.get(key) not in (None, "")
        for key in ("plan_type", "plan", "plan_name", "membership_type")
    ):
        summary["plan_type"] = profile.get("plan_type")

    status_value = getattr(getattr(account, "status", None), "value", getattr(account, "status", ""))
    lifecycle_status = _text(status_value) or None
    rows = _credential_rows_from_extra(
        model.platform,
        extra,
        primary_token=_text(getattr(account, "token", "")),
        source="registration",
    )
    _apply_structured_updates(
        session,
        model,
        lifecycle_status=lifecycle_status,
        summary=summary,
        credential_rows=rows,
        codex_updates={key: extra[key] for key in CODEX_METADATA_KEYS if key in extra},
    )

    incoming_accounts = _provider_accounts_from_extra(extra)
    incoming_resources = _provider_resources_from_extra(extra)
    if incoming_accounts or incoming_resources:
        provider_accounts = _merge_provider_accounts(
            current.get("provider_accounts") or [],
            incoming_accounts,
            prefer_existing=False,
        )
        provider_resources = _merge_provider_resources(
            current.get("provider_resources") or [],
            incoming_resources,
            prefer_existing=False,
        )
        _replace_provider_relations(
            session,
            account_id=account_id,
            provider_accounts=provider_accounts,
            provider_resources=provider_resources,
        )


def patch_account_graph(
    session: Session,
    model: AccountModel,
    *,
    lifecycle_status: str | None = None,
    primary_token: str | None = None,
    cashier_url: str | None = None,
    region: str | None = None,
    trial_end_time: int | None = None,
    summary_updates: dict[str, Any] | None = None,
    credential_updates: dict[str, Any] | None = None,
    replace_credential_keys: set[str] | None = None,
    provider_accounts: list[dict[str, Any]] | None = None,
    provider_resources: list[dict[str, Any]] | None = None,
    replace_provider_accounts: bool = False,
    replace_provider_resources: bool = False,
) -> None:
    account_id = int(model.id or 0)
    if account_id <= 0:
        return
    summary = _safe_dict(summary_updates)
    if cashier_url is not None:
        summary["cashier_url"] = cashier_url
    if region is not None:
        summary["region"] = region
    if trial_end_time is not None:
        summary["trial_end_time"] = int(trial_end_time or 0)

    updates = _safe_dict(credential_updates)
    if replace_credential_keys:
        keys = {_text(key) for key in replace_credential_keys if _text(key)}
        if keys:
            existing_credentials = session.exec(
                select(AccountAuthCredentialModel)
                .where(AccountAuthCredentialModel.account_id == account_id)
                .where(AccountAuthCredentialModel.scope == "platform")
                .where(AccountAuthCredentialModel.key.in_(keys))
            ).all()
            for credential in existing_credentials:
                session.delete(credential)
            if existing_credentials:
                session.flush()
    codex_updates = {key: updates[key] for key in CODEX_METADATA_KEYS if key in updates}
    rows = _credential_rows_from_extra(model.platform, updates, source="runtime.patch")
    if primary_token is not None and _text(primary_token):
        current_primary = session.exec(
            select(AccountAuthCredentialModel)
            .where(AccountAuthCredentialModel.account_id == account_id)
            .where(AccountAuthCredentialModel.scope == "platform")
            .where(AccountAuthCredentialModel.is_primary == True)  # noqa: E712
        ).first()
        key = current_primary.key if current_primary else _default_primary_token_key(model.platform)
        rows.append(
            {
                "scope": "platform",
                "provider_name": model.platform,
                "credential_type": "token",
                "key": key,
                "value": primary_token,
                "is_primary": True,
                "source": "accounts.api",
                "metadata": {},
            }
        )
    _apply_structured_updates(
        session,
        model,
        lifecycle_status=lifecycle_status,
        summary=summary,
        credential_rows=rows,
        codex_updates=codex_updates,
    )

    current = _graph_for_account(session, account_id)
    if provider_accounts is not None or provider_resources is not None:
        current_accounts = list(current.get("provider_accounts") or [])
        current_resources = list(current.get("provider_resources") or [])
        next_accounts = current_accounts
        next_resources = current_resources
        if provider_accounts is not None:
            normalized = _provider_accounts_from_extra({"provider_accounts": provider_accounts})
            next_accounts = normalized if replace_provider_accounts else _merge_provider_accounts(
                current_accounts,
                normalized,
                prefer_existing=False,
            )
        if provider_resources is not None:
            normalized = _provider_resources_from_extra({"provider_resources": provider_resources})
            next_resources = normalized if replace_provider_resources else _merge_provider_resources(
                current_resources,
                normalized,
                prefer_existing=False,
            )
        _replace_provider_relations(
            session,
            account_id=account_id,
            provider_accounts=next_accounts,
            provider_resources=next_resources,
        )


def sync_all_account_graphs(session: Session) -> None:
    for model in session.exec(select(AccountModel)).all():
        if model.id is not None:
            sync_account_graph(session, model)


def purge_account_graph(session: Session, account_id: int) -> None:
    session.exec(delete(AccountUsageSnapshotModel).where(AccountUsageSnapshotModel.account_id == account_id))
    session.exec(delete(AccountCodexAuthModel).where(AccountCodexAuthModel.account_id == account_id))
    session.exec(delete(AccountSecurityProfileModel).where(AccountSecurityProfileModel.account_id == account_id))
    session.exec(delete(AccountSubscriptionModel).where(AccountSubscriptionModel.account_id == account_id))
    session.exec(delete(AccountStatusModel).where(AccountStatusModel.account_id == account_id))
    session.exec(delete(AccountAuthCredentialModel).where(AccountAuthCredentialModel.account_id == account_id))
    session.exec(delete(ProviderResourceModel).where(ProviderResourceModel.account_id == account_id))
    session.exec(delete(ProviderAccountModel).where(ProviderAccountModel.account_id == account_id))


def _remote_account_id(model: AccountModel, graph: dict[str, Any]) -> str:
    for key in ("account_id", "chatgpt_account_id", "accountId"):
        for item in graph.get("credentials") or []:
            if (
                isinstance(item, dict)
                and item.get("scope") == "platform"
                and item.get("key") == key
                and item.get("credential_type") == "identifier"
            ):
                return _text(item.get("value"))
    return _text(model.user_id)


def build_account_view(model: AccountModel, graph: dict[str, Any]) -> dict[str, Any]:
    """Build the only frontend-facing account projection.

    Authentication secret values and provider credential JSON are deliberately
    never exposed.  Non-secret identifier credentials may populate
    ``identity.account_id``; Codex tokens are represented only by presence
    booleans from ``account_codex_auth``.
    """

    status = _safe_dict(graph.get("status"))
    subscription = _safe_dict(graph.get("subscription"))
    security = _safe_dict(graph.get("security"))
    security_raw = _safe_dict(security.get("raw"))
    usage = _safe_dict(graph.get("usage"))
    codex = _safe_dict(graph.get("codex"))
    platform_credentials: list[dict[str, Any]] = []
    for raw_credential in graph.get("credentials") or []:
        credential = _safe_dict(raw_credential)
        if credential.get("scope") == "platform" and credential.get("value") not in (None, ""):
            platform_credentials.append(credential)
    platform_credential_keys = {_text(item.get("key")) for item in platform_credentials}
    platform_credential_types = {_text(item.get("credential_type")) for item in platform_credentials}
    platform_auth = {
        "has_primary_credential": any(bool(item.get("is_primary")) for item in platform_credentials),
        "has_access_token": bool(
            platform_credential_keys & {"access_token", "accessToken", "auth_token", "authToken"}
        ),
        "has_refresh_token": bool(
            platform_credential_keys & {"refresh_token", "refreshToken", "firebase_refresh_token"}
        ),
        "has_session_token": bool(
            platform_credential_keys & {"session_token", "sessionToken", "wos_session"}
        ),
        "has_cookie": "cookie" in platform_credential_types or bool(
            platform_credential_keys & {"cookie", "cookies", "sso", "sso_rw"}
        ),
    }
    resource = _safe_dict(graph.get("verification_mailbox"))
    mailbox = None
    if resource:
        metadata = _safe_dict(resource.get("metadata"))
        mailbox = {
            "provider": _text(resource.get("provider_name")),
            "email": _text(resource.get("handle") or resource.get("display_name") or metadata.get("email")),
            "account_id": _text(resource.get("resource_identifier") or metadata.get("account_id")),
        }

    view: dict[str, Any] = {
        "identity": {
            "id": int(model.id or 0),
            "platform": model.platform,
            "email": model.email,
            "remote_email": _text(status.get("remote_email")),
            "account_id": _remote_account_id(model, graph),
            "user_id": model.user_id,
        },
        "status": {
            "lifecycle": _text(status.get("lifecycle_status")) or "registered",
            "validity": _text(status.get("validity_status")) or "unknown",
            "display": _text(status.get("display_status")) or "registered",
            "checked_at": serialize_datetime(status.get("checked_at")),
        },
        "subscription": {
            "plan": _text(subscription.get("plan_type")),
            "state": _text(subscription.get("plan_state")) or "unknown",
            "source": _text(subscription.get("source")),
            "trial_end_time": int(subscription.get("trial_end_time") or 0),
            "cashier_url": _text(subscription.get("cashier_url")),
        },
        "security": {
            "phone_bound": bool(security.get("phone_bound")),
            "phone_number_masked": _mask_phone_number(security.get("phone_number_masked")),
            "mfa_enabled": bool(security.get("mfa_enabled")),
            "amr": _safe_list(security.get("amr")),
            "checked_at": serialize_datetime(security.get("checked_at")),
            "observed": bool(
                security.get("checked_at")
                or security_raw.get("_mfa_observed")
                or security_raw.get("_phone_observed")
                or security_raw.get("profile")
                or security.get("amr")
                or security.get("phone_number_masked")
            ),
            "account_source": _text(security_raw.get("account_source")),
            "import_method": _text(security_raw.get("import_method")),
            "registration_executor": _text(security_raw.get("registration_executor")),
            "deactivation_reason": _text(security_raw.get("deactivation_reason")),
            "deactivation_error": _text(security_raw.get("deactivation_error")),
            "deactivation_detected_at": _text(security_raw.get("deactivation_detected_at")),
            "platform_auth": platform_auth,
        },
        "usage": {
            "plan_type": _text(usage.get("plan_type")),
            "used_percent": usage.get("used_percent"),
            "limit_reached": bool(usage.get("limit_reached")),
            "reset_at": int(usage.get("reset_at") or 0),
            "credits": _safe_dict(_sanitize_phone_data(_safe_dict(usage.get("credits")))),
        },
        "codex": {
            "authorized": bool(codex.get("has_access_token") or codex.get("has_refresh_token")),
            "email": _text(codex.get("codex_email")),
            "account_id": _text(codex.get("codex_account_id")),
            "plan_type": _text(codex.get("codex_plan_type")),
            "expires_at": serialize_datetime(codex.get("expires_at")),
            "last_refresh": serialize_datetime(codex.get("last_refresh")),
            "auth_path": _text(codex.get("auth_path")),
            "has_access_token": bool(codex.get("has_access_token")),
            "has_refresh_token": bool(codex.get("has_refresh_token")),
        },
        "verification": {"mailbox": mailbox},
    }
    from core.account_display import build_account_view_display

    view["display"] = build_account_view_display(
        status=view["status"],
        subscription=view["subscription"],
        security=view["security"],
        usage={**view["usage"], "raw": _safe_dict(usage.get("raw"))},
        codex=view["codex"],
        verification=view["verification"],
        last_error=_text(status.get("last_error")),
    )
    return view


def load_account_views(
    session: Session,
    models: list[AccountModel] | list[int],
) -> dict[int, dict[str, Any]]:
    if not models:
        return {}
    if isinstance(models[0], AccountModel):
        account_models = [item for item in models if isinstance(item, AccountModel)]
    else:
        ids = [int(item) for item in models if int(item or 0) > 0]
        account_models = list(session.exec(select(AccountModel).where(AccountModel.id.in_(ids))).all())
    graphs = load_account_graphs(session, [int(model.id or 0) for model in account_models])
    return {
        int(model.id or 0): build_account_view(model, graphs.get(int(model.id or 0), {}))
        for model in account_models
        if model.id is not None
    }


def matches_status_filter(graph: dict[str, Any], status: str) -> bool:
    expected = _text(status)
    if not expected:
        return True
    return expected in {
        _text(graph.get("display_status")),
        _text(graph.get("lifecycle_status")),
        _text(graph.get("plan_state")),
        _text(graph.get("validity_status")),
    }


def compute_account_stats(graphs: list[dict[str, Any]], platforms: list[str]) -> dict[str, dict[str, int]]:
    by_platform: dict[str, int] = defaultdict(int)
    by_lifecycle_status: dict[str, int] = defaultdict(int)
    by_plan_state: dict[str, int] = defaultdict(int)
    by_validity_status: dict[str, int] = defaultdict(int)
    by_display_status: dict[str, int] = defaultdict(int)
    for platform in platforms:
        by_platform[platform] += 1
    for graph in graphs:
        by_lifecycle_status[_text(graph.get("lifecycle_status") or "registered")] += 1
        by_plan_state[_text(graph.get("plan_state") or "unknown")] += 1
        by_validity_status[_text(graph.get("validity_status") or "unknown")] += 1
        by_display_status[_text(graph.get("display_status") or "registered")] += 1
    return {
        "by_platform": dict(by_platform),
        "by_lifecycle_status": dict(by_lifecycle_status),
        "by_plan_state": dict(by_plan_state),
        "by_validity_status": dict(by_validity_status),
        "by_display_status": dict(by_display_status),
    }
