from __future__ import annotations

from domain.accounts import AccountExportSelection, AccountFilters
from infrastructure.accounts_repository import AccountsRepository
from application.tasks import create_account_check_all_task
from services.task_runtime import task_runtime


class AccountChecksService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def check_all_async(
        self,
        platform: str = "chatgpt",
        *,
        ids: list[int] | None = None,
        select_all: bool | None = None,
        status_filter: str = "",
        search_filter: str = "",
        filters: AccountFilters | None = None,
        platform_proxy_mode: str = "",
        platform_proxy_value: str = "",
        concurrency: int = 0,
        request_timeout_seconds: int = 0,
        relogin_invalid: bool = False,
        relogin_params: dict | None = None,
    ) -> dict:
        platform = platform or "chatgpt"
        if select_all is None and ids is None and not status_filter and not search_filter and not filters:
            from core.account_check_settings import get_account_check_settings

            settings = get_account_check_settings()
            task = create_account_check_all_task(
                platform,
                limit=settings.batch_limit,
                platform_proxy_mode=platform_proxy_mode,
                platform_proxy_value=platform_proxy_value,
                concurrency=concurrency or settings.concurrency,
                request_timeout_seconds=request_timeout_seconds or settings.request_timeout_seconds,
                relogin_invalid=relogin_invalid,
                relogin_params=relogin_params,
            )
            task_runtime.wake_up()
            return task

        normalized_ids = [int(item) for item in ids or [] if int(item or 0) > 0]
        if not select_all and not normalized_ids:
            task = create_account_check_all_task(
                platform,
                account_ids=[],
                platform_proxy_mode=platform_proxy_mode,
                platform_proxy_value=platform_proxy_value,
                concurrency=concurrency,
                request_timeout_seconds=request_timeout_seconds,
                relogin_invalid=relogin_invalid,
                relogin_params=relogin_params,
            )
            task_runtime.wake_up()
            return task

        records = self.repository.select_for_export(
            AccountExportSelection(
                platform=platform,
                ids=normalized_ids,
                select_all=bool(select_all),
                status_filter=status_filter or "",
                search_filter=search_filter or "",
                filters=filters or AccountFilters(),
            )
        )
        task = create_account_check_all_task(
            platform,
            account_ids=[item.id for item in records],
            platform_proxy_mode=platform_proxy_mode,
            platform_proxy_value=platform_proxy_value,
            concurrency=concurrency,
            request_timeout_seconds=request_timeout_seconds,
            relogin_invalid=relogin_invalid,
            relogin_params=relogin_params,
        )
        task_runtime.wake_up()
        return task
