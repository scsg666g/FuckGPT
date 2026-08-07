from __future__ import annotations

import io
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.account_exports import AccountExportsService, ExportArtifact
from application.accounts import AccountsService
from application.account_pushes import AccountPushService
from application.tasks import create_codex_oauth_batch_task, create_relogin_batch_task
from services.task_runtime import task_runtime
from domain.accounts import AccountExportSelection, AccountFilters, AccountQuery, AccountUpdateCommand
from infrastructure.accounts_repository import AccountsRepository

router = APIRouter(prefix="/accounts", tags=["accounts"])
service = AccountsService()
exports_service = AccountExportsService()
push_service = AccountPushService()


class AccountUpdateRequest(BaseModel):
    password: Optional[str] = None
    user_id: Optional[str] = None
    lifecycle_status: Optional[str] = None
    overview: Optional[dict] = None
    credentials: Optional[dict] = None
    provider_accounts: Optional[list[dict]] = None
    provider_resources: Optional[list[dict]] = None
    replace_provider_accounts: bool = False
    replace_provider_resources: bool = False
    primary_token: Optional[str] = None
    cashier_url: Optional[str] = None
    region: Optional[str] = None
    trial_end_time: Optional[int] = None


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]


class AccountFiltersRequest(BaseModel):
    search: str = ""
    status: str = ""
    mailbox_bound: str = ""
    mailbox_provider: str = ""
    mailbox_email_match: str = ""
    phone_state: str = ""
    checked_state: str = ""
    mfa_state: str = ""
    codex_auth_state: str = ""
    push_status: str = ""
    push_target: str = ""
    pushed_from: str = ""
    pushed_to: str = ""
    codex_refreshed_from: str = ""
    codex_refreshed_to: str = ""
    time_field: str = ""
    time_from: str = ""
    time_to: str = ""
    source: str = ""
    import_method: str = ""
    region: str = ""
    sort_by: str = "created_at"
    sort_order: str = "desc"

    def to_domain(self) -> AccountFilters:
        return AccountFilters(**self.model_dump())


class BatchExportRequest(BaseModel):
    platform: str = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    select_all: bool = False
    status_filter: Optional[str] = None
    email_service_filter: Optional[str] = None
    search_filter: Optional[str] = None
    filters: AccountFiltersRequest = Field(default_factory=AccountFiltersRequest)


class CodexOAuthBatchRequest(BatchExportRequest):
    params: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = 1


class ReloginBatchRequest(BatchExportRequest):
    params: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = 1


class AccountPushRequest(BatchExportRequest):
    target_key: str = ""
    payload_format: Literal["codex", "sub2api"] | None = None


def _selection(body: BatchExportRequest) -> AccountExportSelection:
    return AccountExportSelection(
        platform=body.platform or "chatgpt",
        ids=body.ids,
        select_all=body.select_all,
        status_filter=body.status_filter or "",
        search_filter=body.search_filter or "",
        filters=body.filters.to_domain(),
    )


def _stream_artifact(artifact: ExportArtifact) -> StreamingResponse:
    if isinstance(artifact.content, io.BytesIO):
        body = artifact.content
    elif isinstance(artifact.content, bytes):
        body = iter([artifact.content])
    else:
        body = iter([artifact.content])
    return StreamingResponse(
        body,
        media_type=artifact.media_type,
        headers={"Content-Disposition": f"attachment; filename={artifact.filename}"},
    )


@router.get("")
def list_accounts(
    platform: str = "",
    status: str = "",
    email: str = "",
    page: int = 1,
    page_size: int = 20,
    search: str = "",
    mailbox_bound: str = "",
    mailbox_provider: str = "",
    mailbox_email_match: str = "",
    phone_state: str = "",
    checked_state: str = "",
    mfa_state: str = "",
    codex_auth_state: str = "",
    push_status: str = "",
    push_target: str = "",
    pushed_from: str = "",
    pushed_to: str = "",
    codex_refreshed_from: str = "",
    codex_refreshed_to: str = "",
    time_field: str = "",
    time_from: str = "",
    time_to: str = "",
    source: str = "",
    import_method: str = "",
    region: str = "",
    sort_by: str = "created_at",
    sort_order: str = "desc",
):
    filters = AccountFilters(
        search=search or email,
        status=status,
        mailbox_bound=mailbox_bound,
        mailbox_provider=mailbox_provider,
        mailbox_email_match=mailbox_email_match,
        phone_state=phone_state,
        checked_state=checked_state,
        mfa_state=mfa_state,
        codex_auth_state=codex_auth_state,
        push_status=push_status,
        push_target=push_target,
        pushed_from=pushed_from,
        pushed_to=pushed_to,
        codex_refreshed_from=codex_refreshed_from,
        codex_refreshed_to=codex_refreshed_to,
        time_field=time_field,
        time_from=time_from,
        time_to=time_to,
        source=source,
        import_method=import_method,
        region=region,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return service.list_accounts(AccountQuery(platform=platform, filters=filters, page=page, page_size=page_size))


@router.get("/stats")
def get_stats(platform: str = ""):
    return {**service.get_stats(), **service.get_filter_stats(platform)}


@router.get("/push-targets")
def list_push_targets():
    return {"items": push_service.list_targets()}


@router.post("/push")
def push_accounts(body: AccountPushRequest):
    try:
        return push_service.push_accounts(
            _selection(body),
            target_key=body.target_key,
            payload_format=body.payload_format or "",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/export/json")
def export_accounts_json(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_json(
            _selection(body)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/csv")
def export_accounts_csv(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_csv(
            _selection(body)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/sub2api")
def export_accounts_sub2api(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_sub2api(
            _selection(body)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/cpa")
def export_accounts_cpa(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_cpa(
            _selection(body)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/codex")
def export_accounts_codex(body: BatchExportRequest):
    try:
        artifact = exports_service.export_chatgpt_codex(
            _selection(body)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/export/any2api")
def export_accounts_any2api(body: BatchExportRequest):
    try:
        artifact = exports_service.export_any2api(
            _selection(body)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _stream_artifact(artifact)


@router.post("/codex-oauth/authorize")
def authorize_codex_oauth_batch(body: CodexOAuthBatchRequest):
    try:
        records = AccountsRepository().select_for_export(
            _selection(body)
        )
        task = create_codex_oauth_batch_task(
            platform=body.platform or "chatgpt",
            account_ids=[int(item.id or 0) for item in records],
            params=body.params,
            concurrency=body.concurrency,
        )
        task_runtime.wake_up()
        return task
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/relogin/batch")
def relogin_batch(body: ReloginBatchRequest):
    try:
        records = AccountsRepository().select_for_export(
            _selection(body)
        )
        task = create_relogin_batch_task(
            platform=body.platform or "chatgpt",
            account_ids=[int(item.id or 0) for item in records],
            params=body.params,
            concurrency=body.concurrency,
        )
        task_runtime.wake_up()
        return task
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/import")
def import_accounts(body: ImportRequest):
    return service.import_accounts(body.platform, body.lines)


@router.get("/{account_id}/credentials")
def get_account_credentials(
    account_id: int,
    response: Response,
    scope: Literal["platform", "codex"] | None = None,
):
    result = service.get_credentials(account_id, scope)
    if result is None:
        raise HTTPException(404, "账号不存在")
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{account_id}")
def get_account(account_id: int):
    item = service.get_account(account_id)
    if not item:
        raise HTTPException(404, "账号不存在")
    return item


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdateRequest):
    item = service.update_account(account_id, AccountUpdateCommand(**body.model_dump()))
    if not item:
        raise HTTPException(404, "账号不存在")
    return item


@router.delete("/{account_id}")
def delete_account(account_id: int):
    result = service.delete_account(account_id)
    if not result["ok"]:
        raise HTTPException(404, "账号不存在")
    return result
