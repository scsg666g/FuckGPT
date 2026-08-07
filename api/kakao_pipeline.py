from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from features.kakao_pipeline.client import CustomerApiProblem
from features.kakao_pipeline.service import KakaoPipelineService


router = APIRouter(prefix="/kakao-pipeline", tags=["kakao-pipeline"])
service = KakaoPipelineService()


class KakaoSettingRequest(BaseModel):
    display_name: str = ""
    base_url: str
    cdk_key: str = ""
    cdk_keys: str | list[str] = ""


class ExtractRequest(BaseModel):
    supplier_setting_id: int | None = None
    payment_method: str = "kakao_pay"


class ScannerRequest(BaseModel):
    scanner_setting_id: int | None = None
    scanner_kind: str = ""


class ResetRequest(BaseModel):
    force: bool = False


class PlusCheckRequest(BaseModel):
    advance_pipeline: bool = False


class DefaultScannerRequest(BaseModel):
    scanner_kind: str


class AutoUploadRequest(BaseModel):
    enabled: bool


class AccountProxyRequest(BaseModel):
    mode: str = "direct"
    value: str = ""


class KakaoArchiveRequest(BaseModel):
    account_ids: list[int] = Field(min_length=1, max_length=500)
    reason: str = ""
    disposition: Literal["auto", "completed", "abandoned"] = "auto"
    force: bool = False


class KakaoArchiveIdsRequest(BaseModel):
    account_ids: list[int] = Field(min_length=1, max_length=500)


def _raise_problem(exc: Exception) -> None:
    if isinstance(exc, CustomerApiProblem):
        status_code = 502 if exc.status_code >= 500 else 400
        raise HTTPException(status_code, {"code": exc.code, "message": exc.message}) from exc
    raise HTTPException(400, str(exc)) from exc


@router.get("/settings")
def list_settings(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return service.list_settings()


@router.put("/settings/{kind}")
def save_setting(kind: str, body: KakaoSettingRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.save_setting(kind, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/settings/{kind}/test")
def test_setting(kind: str, body: KakaoSettingRequest):
    try:
        return service.test_setting(kind, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/settings/{kind}/check-cdks")
def check_cdks(kind: str, body: KakaoSettingRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.check_cdks(kind, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.put("/settings/default-scanner/select")
def select_default_scanner(body: DefaultScannerRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.set_default_scanner(body.scanner_kind)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.put("/settings/options/auto-upload")
def set_auto_upload(body: AutoUploadRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.set_auto_upload(body.enabled)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.put("/settings/options/account-proxy")
def set_account_proxy(body: AccountProxyRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.set_account_proxy(body.mode, body.value)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.get("/accounts")
def list_accounts(
    response: Response,
    search: str = "",
    page: int = Query(default=1, ge=1, le=10_000_000),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    view: Literal["workspace", "completed", "archived", "all"] = "workspace",
):
    response.headers["Cache-Control"] = "no-store"
    return service.list_accounts(
        search=search,
        page=page,
        page_size=page_size,
        view=view,
    )


@router.post("/archive")
def archive_accounts(body: KakaoArchiveRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.archive_accounts(
            body.account_ids,
            reason=body.reason,
            disposition=body.disposition,
            force=body.force,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/archive/restore")
def restore_archived_accounts(body: KakaoArchiveIdsRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.restore_accounts(body.account_ids)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/archive/purge")
def purge_archived_accounts(body: KakaoArchiveIdsRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.purge_archived_accounts(body.account_ids)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.get("/accounts/{account_id}")
def get_pipeline(account_id: int, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.get_account_pipeline(account_id)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/accounts/{account_id}/extract")
def start_extraction(account_id: int, body: ExtractRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.start_extraction(
            account_id,
            supplier_setting_id=body.supplier_setting_id,
            payment_method=body.payment_method,
            enable_post_actions=True,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/accounts/{account_id}/supplier/poll")
def poll_supplier(account_id: int, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.poll_supplier(account_id)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/accounts/{account_id}/scanner")
def submit_scanner(account_id: int, body: ScannerRequest, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.submit_scanner(
            account_id,
            scanner_setting_id=body.scanner_setting_id,
            scanner_kind=body.scanner_kind,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/accounts/{account_id}/scanner/poll")
def poll_scanner(account_id: int, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.poll_scanner(account_id)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/accounts/{account_id}/plus/check")
def check_plus(account_id: int, response: Response, body: PlusCheckRequest | None = None):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.check_plus(
            account_id,
            advance_pipeline=bool(body and body.advance_pipeline),
            enable_post_actions=True if body and body.advance_pipeline else None,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/accounts/{account_id}/codex")
def start_codex(account_id: int, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.start_codex(account_id)
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)


@router.post("/accounts/{account_id}/reset")
def reset_pipeline(account_id: int, body: ResetRequest | None = None):
    try:
        return service.reset(account_id, force=bool(body and body.force))
    except Exception as exc:  # noqa: BLE001
        _raise_problem(exc)
