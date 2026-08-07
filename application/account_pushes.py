from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from application.account_exports import make_cpa_payload, make_codex_inventory_payload, make_sub2api_payload
from domain.accounts import AccountExportSelection, AccountRecord
from infrastructure.account_push_deliveries_repository import AccountPushDeliveriesRepository
from infrastructure.accounts_repository import AccountsRepository
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
from infrastructure.provider_settings_repository import ProviderSettingsRepository
from providers.registry import create_provider, load_all


class AccountPushService:
    def __init__(
        self,
        accounts: AccountsRepository | None = None,
        settings: ProviderSettingsRepository | None = None,
        deliveries: AccountPushDeliveriesRepository | None = None,
    ):
        self.accounts = accounts or AccountsRepository()
        self.settings = settings or ProviderSettingsRepository()
        self.deliveries = deliveries or AccountPushDeliveriesRepository()
        self.definitions = ProviderDefinitionsRepository()

    def list_targets(self) -> list[dict]:
        targets = []
        for setting in self.settings.list_enabled("push"):
            runtime = self.settings.resolve_runtime_settings("push", setting.provider_key)
            targets.append({
                "key": setting.provider_key,
                "label": setting.display_name or setting.provider_key,
                "is_default": bool(setting.is_default),
                "payload_format": (
                    "codex"
                    if setting.provider_key == "cpa"
                    else str(runtime.get(f"{setting.provider_key}_payload_format") or "codex")
                ),
            })
        return targets

    def push_accounts(
        self,
        selection: AccountExportSelection,
        *,
        target_key: str = "",
        payload_format: str = "",
    ) -> dict:
        resolved_key = target_key or self.settings.get_default_provider_key("push")
        if not resolved_key:
            raise ValueError("未配置已启用的推送目标，请先到设置 → 推送目标中配置")
        setting = self.settings.get_by_key("push", resolved_key)
        if not setting or not setting.enabled:
            raise ValueError("推送目标不存在或未启用")
        definition = self.definitions.get_by_key("push", resolved_key)
        if not definition:
            raise ValueError(f"未找到推送目标定义: {resolved_key}")

        runtime = self.settings.resolve_runtime_settings("push", resolved_key)
        if resolved_key == "cpa":
            selected_format = "codex"
            allowed_formats = {"codex"}
        else:
            selected_format = payload_format or str(runtime.get(f"{resolved_key}_payload_format") or "codex")
            allowed_formats = {"codex", "sub2api"}
        if selected_format not in allowed_formats:
            raise ValueError(f"不支持的推送格式: {selected_format}")
        records = self.accounts.select_for_export(selection)
        if not records:
            raise ValueError("没有可推送的账号")

        load_all()
        provider = create_provider("push", definition.driver_type or resolved_key, runtime)
        configuration_error = getattr(provider, "configuration_error", lambda: "")()
        if configuration_error:
            raise ValueError(configuration_error)

        label = setting.display_name or definition.label or resolved_key
        self.deliveries.mark_pending(
            [record.id for record in records],
            target_key=resolved_key,
            target_label=label,
            payload_format=selected_format,
        )

        def deliver(record: AccountRecord) -> dict:
            try:
                if resolved_key == "cpa":
                    payload = make_cpa_payload(record)
                elif selected_format == "codex":
                    payload = make_codex_inventory_payload(record)
                else:
                    payload = make_sub2api_payload(record)
                response = provider.push(payload)
                return {
                    "account_id": record.id,
                    "email": record.email,
                    "ok": bool(response.ok),
                    "http_status": int(response.http_status or 0),
                    "error": str(response.error or ""),
                }
            except ValueError as exc:
                return {
                    "account_id": record.id,
                    "email": record.email,
                    "ok": False,
                    "http_status": 0,
                    "error": str(exc),
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "account_id": record.id,
                    "email": record.email,
                    "ok": False,
                    "http_status": 0,
                    "error": f"推送失败：{type(exc).__name__}",
                }

        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(4, len(records))) as executor:
            futures = {executor.submit(deliver, record): record.id for record in records}
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item["account_id"])
        self.deliveries.record_results(results, target_key=resolved_key)
        succeeded = sum(1 for result in results if result["ok"])
        return {
            "ok": succeeded == len(results),
            "target_key": resolved_key,
            "target_label": label,
            "payload_format": selected_format,
            "total": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "results": results,
        }
