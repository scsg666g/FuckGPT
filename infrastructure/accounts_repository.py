from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from core.account_display import build_account_display_summary
from core.datetime_utils import ensure_utc_datetime, serialize_datetime
from core.db import AccountModel, AccountPushDeliveryModel, engine
from core.account_graph import (
    build_account_view,
    compute_account_stats,
    load_account_graphs,
    matches_status_filter,
    patch_account_graph,
    purge_account_graph,
    sync_account_graph,
)
from core.platform_accounts import resolve_primary_token
from domain.accounts import (
    AccountExportSelection,
    AccountFilters,
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountStats,
    AccountUpdateCommand,
)


def _build_summary_updates(
    overview: dict | None,
    *,
    cashier_url: str | None = None,
    region: str | None = None,
    trial_end_time: int | None = None,
) -> dict | None:
    summary = dict(overview or {})
    if cashier_url is not None:
        summary["cashier_url"] = cashier_url
    if region is not None:
        summary["region"] = region
    if trial_end_time is not None:
        summary["trial_end_time"] = int(trial_end_time or 0)
    return summary or None


def _build_credential_updates(
    credentials: dict | None,
) -> dict | None:
    return dict(credentials or {}) or None


def _to_record(
    model: AccountModel,
    graph: dict | None = None,
    push_deliveries: list[dict] | None = None,
) -> AccountRecord:
    graph = graph or {}
    overview = graph.get("overview") or {}
    lifecycle_status = graph.get("lifecycle_status") or "registered"
    validity_status = graph.get("validity_status") or "unknown"
    plan_state = graph.get("plan_state") or "unknown"
    plan_name = graph.get("plan_name") or ""
    display_status = graph.get("display_status") or "registered"
    provider_resources = list(graph.get("provider_resources") or [])
    return AccountRecord(
        id=int(model.id or 0),
        platform=model.platform,
        email=model.email,
        password=model.password,
        user_id=model.user_id,
        primary_token=resolve_primary_token(model, graph),
        trial_end_time=int(overview.get("trial_end_time") or 0),
        cashier_url=str(overview.get("cashier_url") or ""),
        lifecycle_status=lifecycle_status,
        validity_status=validity_status,
        plan_state=plan_state,
        plan_name=plan_name,
        display_status=display_status,
        overview=overview,
        display_summary=build_account_display_summary(
            platform=model.platform,
            email=model.email,
            lifecycle_status=lifecycle_status,
            validity_status=validity_status,
            plan_state=plan_state,
            plan_name=plan_name,
            display_status=display_status,
            overview=overview,
            provider_resources=provider_resources,
        ),
        account_view=build_account_view(model, graph),
        credentials=list(graph.get("credentials") or []),
        provider_accounts=list(graph.get("provider_accounts") or []),
        provider_resources=provider_resources,
        push_deliveries=list(push_deliveries or []),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class AccountsRepository:
    @staticmethod
    def _effective_filters(
        filters: AccountFilters | None,
        *,
        status: str = "",
        search: str = "",
    ) -> AccountFilters:
        value = filters or AccountFilters()
        return AccountFilters(
            **{
                **{field: getattr(value, field) for field in AccountFilters.__dataclass_fields__},
                "status": value.status or status,
                "search": value.search or search,
            }
        )

    @staticmethod
    def _record_matches(record: AccountRecord, filters: AccountFilters) -> bool:
        view = record.account_view or {}
        identity = view.get("identity") or {}
        status = view.get("status") or {}
        security = view.get("security") or {}
        codex = view.get("codex") or {}
        mailbox = ((view.get("verification") or {}).get("mailbox") or {})
        deliveries = list(record.push_deliveries or [])

        if filters.status and not matches_status_filter({
            "display_status": record.display_status,
            "lifecycle_status": record.lifecycle_status,
            "plan_state": record.plan_state,
            "validity_status": record.validity_status,
        }, filters.status):
            return False

        search = filters.search.strip().casefold()
        if search:
            searchable = (
                record.email,
                record.user_id,
                identity.get("remote_email"),
                identity.get("account_id"),
                identity.get("user_id"),
                codex.get("email"),
                codex.get("account_id"),
                mailbox.get("email"),
                mailbox.get("account_id"),
            )
            if not any(search in str(value or "").casefold() for value in searchable):
                return False

        mailbox_bound = bool(mailbox)
        if filters.mailbox_bound == "bound" and not mailbox_bound:
            return False
        if filters.mailbox_bound == "unbound" and mailbox_bound:
            return False
        if filters.mailbox_provider and str(mailbox.get("provider") or "").casefold() != filters.mailbox_provider.casefold():
            return False
        if filters.mailbox_email_match:
            same = bool(mailbox.get("email")) and str(mailbox.get("email")).casefold() == record.email.casefold()
            if filters.mailbox_email_match == "same" and not same:
                return False
            if filters.mailbox_email_match == "different" and (not mailbox_bound or same):
                return False

        checked = bool(status.get("checked_at") or security.get("checked_at"))
        phone_bound = bool(security.get("phone_bound"))
        if filters.phone_state == "bound" and not phone_bound:
            return False
        if filters.phone_state == "unbound" and (phone_bound or not checked):
            return False
        if filters.phone_state == "unchecked" and checked:
            return False
        if filters.checked_state == "checked" and not checked:
            return False
        if filters.checked_state == "unchecked" and checked:
            return False
        mfa_observed = bool(security.get("observed") or checked)
        mfa_enabled = bool(security.get("mfa_enabled"))
        if filters.mfa_state == "enabled" and not mfa_enabled:
            return False
        if filters.mfa_state == "disabled" and (mfa_enabled or not mfa_observed):
            return False
        if filters.mfa_state == "unchecked" and mfa_observed:
            return False

        codex_authorized = bool(codex.get("authorized"))
        if filters.codex_auth_state == "authorized" and not codex_authorized:
            return False
        if filters.codex_auth_state == "unauthorized" and codex_authorized:
            return False

        selected_delivery = next(
            (
                delivery
                for delivery in deliveries
                if str(delivery.get("target_key") or "") == filters.push_target
            ),
            None,
        ) if filters.push_target else (deliveries[0] if deliveries else None)
        if filters.push_target and filters.push_status == "" and selected_delivery is None:
            return False
        if filters.push_status == "not_pushed" and selected_delivery is not None:
            return False
        if filters.push_status and filters.push_status != "not_pushed":
            if selected_delivery is None or str(selected_delivery.get("status") or "") != filters.push_status:
                return False
        if filters.pushed_from or filters.pushed_to:
            pushed_at = ensure_utc_datetime(
                (selected_delivery or {}).get("last_attempt_at")
                or (selected_delivery or {}).get("pushed_at")
            )
            pushed_from = ensure_utc_datetime(filters.pushed_from)
            pushed_to = ensure_utc_datetime(filters.pushed_to)
            if pushed_at is None or (pushed_from and pushed_at < pushed_from) or (pushed_to and pushed_at > pushed_to):
                return False

        if filters.codex_refreshed_from or filters.codex_refreshed_to:
            refreshed_at = ensure_utc_datetime(codex.get("last_refresh"))
            refreshed_from = ensure_utc_datetime(filters.codex_refreshed_from)
            refreshed_to = ensure_utc_datetime(filters.codex_refreshed_to)
            if refreshed_at is None or (refreshed_from and refreshed_at < refreshed_from) or (refreshed_to and refreshed_at > refreshed_to):
                return False

        source = str(security.get("account_source") or "")
        executor = str(security.get("registration_executor") or "")
        if filters.source:
            actual_source = source
            if source == "registration":
                actual_source = "protocol" if executor == "protocol" else "browser"
            if actual_source != filters.source:
                return False
        if filters.import_method and str(security.get("import_method") or "") != filters.import_method:
            return False
        if filters.region and str(record.overview.get("region") or "").casefold() != filters.region.casefold():
            return False

        if filters.time_from or filters.time_to:
            time_values = {
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "checked_at": status.get("checked_at") or security.get("checked_at"),
                "expires_at": codex.get("expires_at") or (
                    datetime.fromtimestamp(record.trial_end_time, tz=timezone.utc)
                    if record.trial_end_time
                    else None
                ),
            }
            value = ensure_utc_datetime(time_values.get(filters.time_field or "created_at"))
            start = ensure_utc_datetime(filters.time_from)
            end = ensure_utc_datetime(filters.time_to)
            if value is None or (start and value < start) or (end and value > end):
                return False
        return True

    @staticmethod
    def _sort_records(records: list[AccountRecord], filters: AccountFilters) -> list[AccountRecord]:
        field = filters.sort_by if filters.sort_by in {"created_at", "updated_at", "checked_at", "expires_at"} else "created_at"
        def key(record: AccountRecord):
            view = record.account_view or {}
            if field == "checked_at":
                raw = (view.get("status") or {}).get("checked_at") or (view.get("security") or {}).get("checked_at")
            elif field == "expires_at":
                raw = (view.get("codex") or {}).get("expires_at")
                if not raw and record.trial_end_time:
                    raw = datetime.fromtimestamp(record.trial_end_time, tz=timezone.utc)
            else:
                raw = getattr(record, field, None)
            return (ensure_utc_datetime(raw) or datetime.min.replace(tzinfo=timezone.utc), record.id)
        return sorted(records, key=key, reverse=filters.sort_order != "asc")

    def select_filtered(self, platform: str, filters: AccountFilters | None = None) -> list[AccountRecord]:
        effective = self._effective_filters(filters)
        with Session(engine) as session:
            statement = select(AccountModel)
            if platform:
                statement = statement.where(AccountModel.platform == platform)
            models = session.exec(statement.order_by(AccountModel.id.desc())).all()
            records = self._load_records(session, models)
        matched = [record for record in records if self._record_matches(record, effective)]
        return self._sort_records(matched, effective)

    def filter_stats(self, platform: str) -> dict:
        records = self.select_filtered(platform, AccountFilters())
        mailbox_providers: set[str] = set()
        regions: set[str] = set()
        push_targets: dict[str, str] = {}
        counts = {
            "total": len(records),
            "trial": 0,
            "subscribed": 0,
            "invalid": 0,
            "deactivated": 0,
            "mailbox_bound": 0,
            "phone_bound": 0,
            "unchecked": 0,
            "mfa_enabled": 0,
            "codex_authorized": 0,
            "codex_unauthorized": 0,
            "push_failed": 0,
            "push_not_pushed": 0,
        }
        for record in records:
            view = record.account_view or {}
            status = view.get("status") or {}
            security = view.get("security") or {}
            mailbox = ((view.get("verification") or {}).get("mailbox") or {})
            codex = view.get("codex") or {}
            if record.plan_state == "trial":
                counts["trial"] += 1
            if record.plan_state == "subscribed":
                counts["subscribed"] += 1
            if (
                record.validity_status == "deactivated"
                or record.lifecycle_status == "deactivated"
                or record.display_status == "deactivated"
            ):
                counts["deactivated"] += 1
            if record.validity_status == "invalid" or record.lifecycle_status == "invalid":
                counts["invalid"] += 1
            if mailbox:
                counts["mailbox_bound"] += 1
                if mailbox.get("provider"):
                    mailbox_providers.add(str(mailbox["provider"]))
            if security.get("phone_bound"):
                counts["phone_bound"] += 1
            if not (status.get("checked_at") or security.get("checked_at")):
                counts["unchecked"] += 1
            if security.get("mfa_enabled"):
                counts["mfa_enabled"] += 1
            if codex.get("authorized"):
                counts["codex_authorized"] += 1
            else:
                counts["codex_unauthorized"] += 1
            latest_delivery = record.push_deliveries[0] if record.push_deliveries else None
            for delivery in record.push_deliveries:
                target_key = str(delivery.get("target_key") or "").strip()
                if target_key:
                    push_targets[target_key] = str(delivery.get("target_label") or target_key)
            if latest_delivery is None:
                counts["push_not_pushed"] += 1
            elif latest_delivery.get("status") == "failed":
                counts["push_failed"] += 1
            region = str(record.overview.get("region") or "").strip()
            if region:
                regions.add(region)
        return {
            **counts,
            "mailbox_providers": sorted(mailbox_providers, key=str.casefold),
            "regions": sorted(regions, key=str.casefold),
            "push_targets": [
                {"key": key, "label": push_targets[key]}
                for key in sorted(push_targets, key=lambda value: push_targets[value].casefold())
            ],
        }

    @staticmethod
    def _load_records(session: Session, models: list[AccountModel]) -> list[AccountRecord]:
        account_ids = [int(model.id or 0) for model in models if model.id]
        graphs = load_account_graphs(session, account_ids)
        missing = [
            model
            for model in models
            if not (graphs.get(int(model.id or 0)) or {}).get("status")
        ]
        if missing:
            for model in missing:
                sync_account_graph(session, model)
            session.commit()
            graphs = load_account_graphs(session, account_ids)
        deliveries_by_account: dict[int, list[dict]] = {account_id: [] for account_id in account_ids}
        if account_ids:
            deliveries = session.exec(
                select(AccountPushDeliveryModel)
                .where(AccountPushDeliveryModel.account_id.in_(account_ids))
                .order_by(AccountPushDeliveryModel.updated_at.desc())
            ).all()
            for delivery in deliveries:
                deliveries_by_account.setdefault(delivery.account_id, []).append({
                    "target_key": delivery.target_key,
                    "target_label": delivery.target_label,
                    "payload_format": delivery.payload_format,
                    "status": delivery.status,
                    "attempt_count": delivery.attempt_count,
                    "http_status": delivery.http_status,
                    "last_error": delivery.last_error,
                    "last_attempt_at": serialize_datetime(delivery.last_attempt_at),
                    "pushed_at": serialize_datetime(delivery.pushed_at),
                })
        return [
            _to_record(
                model,
                graphs.get(int(model.id or 0), {}),
                deliveries_by_account.get(int(model.id or 0), []),
            )
            for model in models
        ]

    def list(self, query: AccountQuery) -> tuple[int, list[AccountRecord]]:
        page = max(query.page, 1)
        page_size = min(max(query.page_size, 1), 200)
        start = (page - 1) * page_size
        effective = self._effective_filters(query.filters, status=query.status, search=query.email)
        records = self.select_filtered(query.platform, effective)
        total = len(records)
        end = start + page_size
        return total, records[start:end]

    def get(self, account_id: int) -> AccountRecord | None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return None
            records = self._load_records(session, [model])
            return records[0] if records else None

    def select_for_export(self, selection: AccountExportSelection) -> list[AccountRecord]:
        if not selection.select_all and not selection.ids:
            return []
        effective = self._effective_filters(
            selection.filters,
            status=selection.status_filter,
            search=selection.search_filter,
        )
        records = self.select_filtered(selection.platform, effective)
        if not selection.select_all:
            selected = set(selection.ids)
            records = [record for record in records if record.id in selected]
        return records

    def update(self, account_id: int, command: AccountUpdateCommand) -> AccountRecord | None:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return None
            if command.password is not None:
                model.password = command.password
            if command.user_id is not None:
                model.user_id = command.user_id
            model.updated_at = datetime.now(timezone.utc)
            patch_account_graph(
                session,
                model,
                lifecycle_status=command.lifecycle_status,
                primary_token=command.primary_token,
                cashier_url=command.cashier_url,
                region=command.region,
                trial_end_time=command.trial_end_time,
                summary_updates=_build_summary_updates(
                    command.overview,
                    cashier_url=command.cashier_url,
                    region=command.region,
                    trial_end_time=command.trial_end_time,
                ),
                credential_updates=_build_credential_updates(command.credentials),
                provider_accounts=command.provider_accounts,
                provider_resources=command.provider_resources,
                replace_provider_accounts=command.replace_provider_accounts,
                replace_provider_resources=command.replace_provider_resources,
            )
            session.add(model)
            session.commit()
            session.refresh(model)
            return self._load_records(session, [model])[0]

    def delete(self, account_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(AccountModel, account_id)
            if not model:
                return False
            from core.mailbox_lifecycle import MailboxAllocationLifecycle

            MailboxAllocationLifecycle().archive_account_mailbox(account_id)
            purge_account_graph(session, account_id)
            session.delete(model)
            session.commit()
            return True

    def import_lines(self, platform: str, lines: list[AccountImportLine]) -> int:
        created = 0
        with Session(engine) as session:
            models_by_email: dict[str, AccountModel] = {}
            for line in lines:
                model = models_by_email.get(line.email)
                if model is None:
                    model = session.exec(
                        select(AccountModel)
                        .where(AccountModel.platform == platform)
                        .where(AccountModel.email == line.email)
                    ).first()
                is_new = model is None
                if model is None:
                    model = AccountModel(
                        platform=platform,
                        email=line.email,
                        password=line.password,
                    )
                    session.add(model)
                    session.flush()
                    created += 1
                else:
                    model.password = line.password
                    model.updated_at = datetime.now(timezone.utc)
                    session.add(model)
                models_by_email[line.email] = model

                extra = dict(line.extra or {})
                summary_updates = dict(extra.get("overview") or extra.get("summary") or {})
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
                    "trial_end_time",
                    "cashier_url",
                    "phone_bound",
                    "phone_number_masked",
                    "mfa_enabled",
                    "amr",
                    "chatgpt_usage",
                    "wham_usage",
                    "usage",
                    "usage_summary",
                    "subscription",
                    "security",
                    "profile",
                    "registration_auth_mode",
                    "account_source",
                    "import_method",
                    "registration_executor",
                ):
                    if key in extra and key not in summary_updates:
                        summary_updates[key] = extra[key]
                credential_updates = dict(extra.get("credentials") or {})
                for key in (
                    "access_token",
                    "refresh_token",
                    "session_token",
                    "id_token",
                    "accessToken",
                    "refreshToken",
                    "sessionToken",
                    "idToken",
                    "cookies",
                    "cookie",
                    "api_key",
                    "wos_session",
                    "sso",
                    "sso_rw",
                    "codex_access_token",
                    "codex_refresh_token",
                    "codex_id_token",
                    "codex_account_id",
                    "codex_email",
                    "codex_plan_type",
                    "codex_expires_at",
                    "codex_last_refresh",
                    "codex_auth_path",
                ):
                    if key in extra and key not in credential_updates:
                        credential_updates[key] = extra[key]
                primary_token = extra.get("primary_token")
                if primary_token in (None, ""):
                    primary_token = extra.get("token")
                lifecycle_status = extra.get("lifecycle_status")
                if lifecycle_status in (None, ""):
                    lifecycle_status = extra.get("status")
                if lifecycle_status in (None, "") and is_new:
                    lifecycle_status = "registered"
                patch_account_graph(
                    session,
                    model,
                    lifecycle_status=str(lifecycle_status or "") or None,
                    primary_token=str(primary_token or "") or None,
                    cashier_url=str(extra.get("cashier_url") or "") or None,
                    summary_updates=summary_updates or None,
                    credential_updates=credential_updates or None,
                    provider_accounts=list(extra.get("provider_accounts") or []) or None,
                    provider_resources=list(extra.get("provider_resources") or []) or None,
                    replace_provider_accounts=bool(extra.get("provider_accounts")),
                    replace_provider_resources=bool(extra.get("provider_resources")),
                )
            session.commit()
        return created

    def stats(self) -> AccountStats:
        with Session(engine) as session:
            accounts = session.exec(select(AccountModel).order_by(AccountModel.created_at.desc(), AccountModel.id.desc())).all()
            records = self._load_records(session, accounts)
        stats = compute_account_stats(
            [
                {
                    "lifecycle_status": item.lifecycle_status,
                    "plan_state": item.plan_state,
                    "validity_status": item.validity_status,
                    "display_status": item.display_status,
                }
                for item in records
            ],
            [item.platform for item in records],
        )
        return AccountStats(
            total=len(records),
            by_platform=stats["by_platform"],
            by_status=stats["by_display_status"],
            by_lifecycle_status=stats["by_lifecycle_status"],
            by_plan_state=stats["by_plan_state"],
            by_validity_status=stats["by_validity_status"],
            by_display_status=stats["by_display_status"],
        )
