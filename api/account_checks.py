from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from application.account_checks import AccountChecksService
from domain.accounts import AccountFilters

router = APIRouter(prefix="/accounts", tags=["account-checks"])
service = AccountChecksService()


class AccountCheckAllRequest(BaseModel):
    platform: str = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    select_all: bool | None = None
    status_filter: str = ""
    search_filter: str = ""
    filters: dict = Field(default_factory=dict)
    platform_proxy_mode: str = ""
    platform_proxy_value: str = ""
    concurrency: int = 0
    request_timeout_seconds: int = 0
    relogin_invalid: bool = False
    relogin_params: dict = Field(default_factory=dict)


@router.post("/check-all")
def check_all_accounts(body: AccountCheckAllRequest | None = None, platform: str = "chatgpt"):
    if body is None:
        return service.check_all_async(platform)
    return service.check_all_async(
        body.platform or platform,
        ids=body.ids,
        select_all=body.select_all,
        status_filter=body.status_filter,
        search_filter=body.search_filter,
        filters=(
            AccountFilters(**{
                key: value
                for key, value in body.filters.items()
                if key in AccountFilters.__dataclass_fields__
            })
            if body.filters
            else None
        ),
        platform_proxy_mode=body.platform_proxy_mode,
        platform_proxy_value=body.platform_proxy_value,
        concurrency=body.concurrency,
        request_timeout_seconds=body.request_timeout_seconds,
        relogin_invalid=body.relogin_invalid,
        relogin_params=body.relogin_params,
    )
