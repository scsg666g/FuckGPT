"""数据库模型 - SQLite via SQLModel"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Index, UniqueConstraint, event, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Field, SQLModel, Session, create_engine, select


def _utcnow():
    return datetime.now(timezone.utc)


def _default_database_url() -> str:
    database_path = Path(__file__).resolve().parent.parent / "account_manager.db"
    return f"sqlite:///{database_path}"


DATABASE_URL = os.getenv("ACCOUNT_MANAGER_DATABASE_URL", _default_database_url())


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


engine = create_engine(DATABASE_URL)


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform", "email", name="uq_accounts_platform_email"),
        Index("ix_accounts_platform_created_id", "platform", "created_at", "id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    password: str
    user_id: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class AccountAuthCredentialModel(SQLModel, table=True):
    __tablename__ = "account_auth_credentials"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "scope",
            "provider_name",
            "key",
            name="uq_account_auth_credentials_key",
        ),
        Index("ix_account_auth_credentials_account_scope", "account_id", "scope"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    scope: str = Field(default="platform", index=True)
    provider_name: str = Field(default="", index=True)
    credential_type: str = Field(default="secret", index=True)
    key: str = Field(default="", index=True)
    value: str = ""
    is_primary: bool = False
    source: str = ""
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class AccountStatusModel(SQLModel, table=True):
    __tablename__ = "account_status"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id", ondelete="CASCADE")
    lifecycle_status: str = Field(default="registered", index=True)
    validity_status: str = Field(default="unknown", index=True)
    display_status: str = Field(default="registered", index=True)
    remote_email: str = ""
    region: str = ""
    checked_at: Optional[datetime] = None
    last_error: str = ""
    invalid_check_count: int = Field(default=0, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class AccountSubscriptionModel(SQLModel, table=True):
    __tablename__ = "account_subscription"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id", ondelete="CASCADE")
    plan_type: str = Field(default="", index=True)
    plan_state: str = Field(default="unknown", index=True)
    source: str = ""
    trial_end_time: int = 0
    cashier_url: str = ""
    raw_json: str = "{}"
    checked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_raw(self) -> dict:
        return json.loads(self.raw_json or "{}")

    def set_raw(self, data: dict):
        self.raw_json = json.dumps(data or {}, ensure_ascii=False, default=str)


class AccountSecurityProfileModel(SQLModel, table=True):
    __tablename__ = "account_security_profile"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id", ondelete="CASCADE")
    phone_bound: bool = Field(default=False, index=True)
    phone_number_masked: str = ""
    mfa_enabled: bool = Field(default=False, index=True)
    amr_json: str = "[]"
    raw_json: str = "{}"
    checked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_amr(self) -> list:
        data = json.loads(self.amr_json or "[]")
        return data if isinstance(data, list) else []

    def set_amr(self, data: list):
        self.amr_json = json.dumps(data or [], ensure_ascii=False, default=str)

    def get_raw(self) -> dict:
        data = json.loads(self.raw_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_raw(self, data: dict):
        self.raw_json = json.dumps(data or {}, ensure_ascii=False, default=str)


class AccountUsageSnapshotModel(SQLModel, table=True):
    __tablename__ = "account_usage_snapshot"
    __table_args__ = (
        Index(
            "ix_account_usage_snapshot_account_provider_checked",
            "account_id",
            "provider",
            "checked_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    provider: str = Field(default="", index=True)
    plan_type: str = Field(default="", index=True)
    used_percent: Optional[float] = None
    limit_reached: bool = Field(default=False, index=True)
    reset_at: int = 0
    credits_json: str = "{}"
    raw_json: str = "{}"
    checked_at: datetime = Field(default_factory=_utcnow, index=True)
    created_at: datetime = Field(default_factory=_utcnow)

    def get_credits(self) -> dict:
        data = json.loads(self.credits_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_credits(self, data: dict):
        self.credits_json = json.dumps(data or {}, ensure_ascii=False, default=str)

    def get_raw(self) -> dict:
        data = json.loads(self.raw_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_raw(self, data: dict):
        self.raw_json = json.dumps(data or {}, ensure_ascii=False, default=str)


class AccountCodexAuthModel(SQLModel, table=True):
    __tablename__ = "account_codex_auth"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id", ondelete="CASCADE")
    codex_email: str = Field(default="", index=True)
    codex_account_id: str = Field(default="", index=True)
    codex_plan_type: str = Field(default="", index=True)
    auth_path: str = ""
    expires_at: Optional[datetime] = None
    last_refresh: Optional[datetime] = None
    has_access_token: bool = Field(default=False, index=True)
    has_refresh_token: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ProviderAccountModel(SQLModel, table=True):
    __tablename__ = "provider_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    provider_type: str = Field(default="mailbox", index=True)
    provider_name: str = Field(default="", index=True)
    login_identifier: str = Field(default="", index=True)
    display_name: str = ""
    credentials_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_credentials(self) -> dict:
        return json.loads(self.credentials_json or "{}")

    def set_credentials(self, data: dict):
        self.credentials_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderResourceModel(SQLModel, table=True):
    __tablename__ = "provider_resources"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    provider_type: str = Field(default="mailbox", index=True)
    provider_name: str = Field(default="", index=True)
    resource_type: str = Field(default="resource", index=True)
    resource_identifier: str = Field(default="", index=True)
    handle: str = ""
    display_name: str = ""
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class MailboxProviderAccountModel(SQLModel, table=True):
    """Provider login credentials shared by one or more mailbox addresses."""

    __tablename__ = "mailbox_provider_accounts"
    __table_args__ = (
        UniqueConstraint("provider_name", "login_identifier", name="uq_mailbox_provider_accounts_login"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_name: str = Field(index=True)
    login_identifier: str = Field(index=True)
    display_name: str = ""
    credentials_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_credentials(self) -> dict:
        return json.loads(self.credentials_json or "{}")

    def set_credentials(self, data: dict):
        self.credentials_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class MailboxResourceModel(SQLModel, table=True):
    """Canonical mailbox resource, independent from any GPT account."""

    __tablename__ = "mailbox_resources"
    __table_args__ = (
        UniqueConstraint("provider_name", "resource_identifier", name="uq_mailbox_resources_provider_identifier"),
        UniqueConstraint("provider_name", "address", name="uq_mailbox_resources_provider_address"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_account_id: int = Field(index=True, foreign_key="mailbox_provider_accounts.id")
    provider_name: str = Field(index=True)
    resource_identifier: str = Field(index=True)
    address: str = Field(index=True)
    parent_address: str = ""
    status: str = Field(default="available", index=True)
    provider_resource_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_provider_resource(self) -> dict:
        return json.loads(self.provider_resource_json or "{}")

    def set_provider_resource(self, data: dict):
        self.provider_resource_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class MailboxAllocationModel(SQLModel, table=True):
    """One registration attempt's claim of a mailbox resource."""

    __tablename__ = "mailbox_allocations"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_mailbox_allocations_attempt"),
        Index(
            "uq_mailbox_allocations_active_resource",
            "resource_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: str = Field(primary_key=True)
    resource_id: int = Field(index=True, foreign_key="mailbox_resources.id")
    attempt_id: str = Field(index=True)
    task_id: str = Field(default="", index=True)
    subtask_id: str = ""
    platform: str = Field(default="chatgpt", index=True)
    status: str = Field(default="active", index=True)
    reason: str = ""
    account_id: Optional[int] = Field(default=None, index=True, foreign_key="accounts.id")
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class MailboxAccountLinkModel(SQLModel, table=True):
    """One-to-one successful GPT account to primary verification mailbox link."""

    __tablename__ = "mailbox_account_links"
    __table_args__ = (
        UniqueConstraint("resource_id", name="uq_mailbox_account_links_resource"),
        UniqueConstraint("allocation_id", name="uq_mailbox_account_links_allocation"),
        UniqueConstraint("account_id", name="uq_mailbox_account_links_account"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    resource_id: int = Field(index=True, foreign_key="mailbox_resources.id")
    allocation_id: str = Field(index=True, foreign_key="mailbox_allocations.id")
    account_id: Optional[int] = Field(default=None, index=True, foreign_key="accounts.id")
    account_id_snapshot: int = Field(index=True)
    account_email: str = ""
    platform: str = Field(default="chatgpt", index=True)
    linked_at: datetime = Field(default_factory=_utcnow)
    archived_at: Optional[datetime] = None


class DataMigrationModel(SQLModel, table=True):
    __tablename__ = "data_migrations"

    key: str = Field(primary_key=True)
    completed_at: datetime = Field(default_factory=_utcnow)
    detail_json: str = "{}"

    def set_detail(self, data: dict):
        self.detail_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderDefinitionModel(SQLModel, table=True):
    __tablename__ = "provider_definitions"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_key", name="uq_provider_definitions_type_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(index=True)
    provider_key: str = Field(index=True)
    label: str = ""
    description: str = ""
    driver_type: str = ""
    default_auth_mode: str = ""
    enabled: bool = True
    is_builtin: bool = False
    category: str = ""  # "free" | "selfhost" | "custom"
    auth_modes_json: str = "[]"
    fields_json: str = "[]"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_auth_modes(self) -> list[dict]:
        return json.loads(self.auth_modes_json or "[]")

    def set_auth_modes(self, data: list[dict]):
        self.auth_modes_json = json.dumps(data or [], ensure_ascii=False)

    def get_fields(self) -> list[dict]:
        return json.loads(self.fields_json or "[]")

    def set_fields(self, data: list[dict]):
        self.fields_json = json.dumps(data or [], ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderSettingModel(SQLModel, table=True):
    __tablename__ = "provider_settings"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_key", name="uq_provider_settings_type_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(index=True)
    provider_key: str = Field(index=True)
    display_name: str = ""
    auth_mode: str = ""
    enabled: bool = True
    is_default: bool = False
    config_json: str = "{}"
    auth_json: str = "{}"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_config(self) -> dict:
        return json.loads(self.config_json or "{}")

    def set_config(self, data: dict):
        self.config_json = json.dumps(data or {}, ensure_ascii=False)

    def get_auth(self) -> dict:
        return json.loads(self.auth_json or "{}")

    def set_auth(self, data: dict):
        self.auth_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class AccountPushDeliveryModel(SQLModel, table=True):
    """Latest delivery state for one account and one remote push target.

    The remote payload is deliberately not persisted because it contains OAuth
    credentials.  ``target_key`` is a stable snapshot rather than a foreign key
    to provider settings, so status remains readable if a target is removed.
    """

    __tablename__ = "account_push_deliveries"
    __table_args__ = (
        UniqueConstraint("account_id", "target_key", name="uq_account_push_delivery_target"),
        Index("ix_account_push_delivery_status", "target_key", "status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    target_key: str = Field(index=True)
    target_label: str = ""
    payload_format: str = Field(default="codex", index=True)
    status: str = Field(default="pending", index=True)
    attempt_count: int = 0
    http_status: int = 0
    last_error: str = ""
    last_attempt_at: Optional[datetime] = None
    pushed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class KakaoPipelineModel(SQLModel, table=True):
    """One resumable Kakao extraction/scanner record per local account."""

    __tablename__ = "kakao_pipelines"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_kakao_pipelines_account"),
        Index(
            "ix_kakao_pipelines_archive_state_updated",
            "archived_at",
            "state",
            "final_result",
            "updated_at",
            "id",
        ),
        Index(
            "ix_kakao_pipelines_archive_purged",
            "archived_at",
            "purged_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id", ondelete="CASCADE")
    state: str = Field(default="idle", index=True)
    payment_method: str = "kakao_pay"

    supplier_setting_id: Optional[int] = Field(default=None, index=True)
    supplier_name: str = ""
    supplier_base_url: str = ""
    supplier_cdk_key: str = ""
    supplier_order_id: str = ""
    supplier_customer_token: str = ""
    supplier_poll_url: str = ""
    supplier_status: str = ""
    supplier_response_json: str = "{}"
    supplier_processing_started_at: Optional[datetime] = None
    supplier_deadline_at: Optional[datetime] = None

    payment_url: str = ""

    scanner_setting_id: Optional[int] = Field(default=None, index=True)
    scanner_driver: str = "customer_api"
    scanner_name: str = ""
    scanner_base_url: str = ""
    scanner_cdk_key: str = ""
    scanner_order_id: str = ""
    scanner_customer_token: str = ""
    scanner_poll_url: str = ""
    scanner_status: str = ""
    scanner_response_json: str = "{}"
    scan_url: str = ""
    scan_expires_at: str = ""
    scanner_submit_attempts: int = 0
    scanner_compensation_attempted: bool = False
    scanner_poll_failures: int = 0
    scanner_recovery_reason: str = ""
    scanner_recovery_check_count: int = 0
    scanner_recovery_started_at: Optional[datetime] = None
    scanner_recovery_next_check_at: Optional[datetime] = None
    scanner_recovery_deadline_at: Optional[datetime] = None
    scanner_processing_started_at: Optional[datetime] = None
    scanner_deadline_at: Optional[datetime] = None

    plus_status: str = ""
    final_result: str = ""
    completion_source: str = ""
    plus_check_count: int = 0
    plus_check_started_at: Optional[datetime] = None
    plus_next_check_at: Optional[datetime] = None
    plus_check_deadline_at: Optional[datetime] = None
    plus_check_paused_at: Optional[datetime] = None

    # Kakao page-local post actions.  ``codex_post_action_armed`` is
    # deliberately opt-in so upgrading an existing database cannot enqueue an
    # OAuth browser task for every legacy completed pipeline.
    codex_post_action_armed: bool = False
    codex_task_id: str = ""
    codex_attempt_count: int = 0
    codex_interrupted_retry_count: int = 0
    codex_skipped_at: Optional[datetime] = None
    codex_enqueue_error: str = ""
    codex_push_task_id: str = ""
    codex_push_attempt_count: int = 0
    codex_push_skip_reason: str = ""
    codex_push_enqueue_error: str = ""
    codex_post_action_done_at: Optional[datetime] = None

    last_error_code: str = ""
    last_error_message: str = ""
    events_json: str = "[]"

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    completed_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    archive_reason: str = ""
    archive_disposition: str = ""
    purged_at: Optional[datetime] = None

    def get_supplier_response(self) -> dict:
        data = json.loads(self.supplier_response_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_supplier_response(self, data: dict):
        self.supplier_response_json = json.dumps(data or {}, ensure_ascii=False, default=str)

    def get_scanner_response(self) -> dict:
        data = json.loads(self.scanner_response_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_scanner_response(self, data: dict):
        self.scanner_response_json = json.dumps(data or {}, ensure_ascii=False, default=str)

    def get_events(self) -> list[dict]:
        data = json.loads(self.events_json or "[]")
        return data if isinstance(data, list) else []

    def set_events(self, data: list[dict]):
        self.events_json = json.dumps(data or [], ensure_ascii=False, default=str)


class TaskModel(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = Field(primary_key=True)
    type: str = Field(index=True)
    platform: str = Field(default="", index=True)
    status: str = Field(default="pending", index=True)
    payload_json: str = "{}"
    result_json: str = "{}"
    progress_current: int = 0
    progress_total: int = 0
    success_count: int = 0
    error_count: int = 0
    error: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_payload(self) -> dict:
        return json.loads(self.payload_json or "{}")

    def set_payload(self, data: dict):
        self.payload_json = json.dumps(data or {}, ensure_ascii=False)

    def get_result(self) -> dict:
        return json.loads(self.result_json or "{}")

    def set_result(self, data: dict):
        self.result_json = json.dumps(data or {}, ensure_ascii=False)


class TaskEventModel(SQLModel, table=True):
    __tablename__ = "task_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    type: str = Field(default="log", index=True)
    level: str = "info"
    message: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)

    def get_detail(self) -> dict:
        return json.loads(self.detail_json or "{}")

    def set_detail(self, data: dict):
        self.detail_json = json.dumps(data or {}, ensure_ascii=False)


class WorkflowDefinitionModel(SQLModel, table=True):
    __tablename__ = "workflow_definitions"
    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_workflow_definition_version"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    version: int = Field(default=1, index=True)
    name: str
    description: str = ""
    enabled: bool = Field(default=True, index=True)
    definition_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_definition(self) -> dict:
        return json.loads(self.definition_json or "{}")

    def set_definition(self, data: dict):
        self.definition_json = json.dumps(data or {}, ensure_ascii=False)


class WorkflowInputPresetModel(SQLModel, table=True):
    __tablename__ = "workflow_input_presets"
    __table_args__ = (
        UniqueConstraint("definition_key", "name", name="uq_workflow_input_preset_name"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    definition_key: str = Field(index=True)
    definition_version: int = Field(default=1, index=True)
    name: str
    is_default: bool = Field(default=False, index=True)
    is_last_used: bool = Field(default=False, index=True)
    input_json: str = "{}"
    launch_mode: str = "single"
    batch_concurrency: int = 1
    batch_count: int = 5
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_input(self) -> dict:
        data = json.loads(self.input_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_input(self, data: dict):
        self.input_json = json.dumps(data or {}, ensure_ascii=False, default=str)


class WorkflowBatchModel(SQLModel, table=True):
    __tablename__ = "workflow_batches"

    id: str = Field(primary_key=True)
    definition_key: str = Field(index=True)
    definition_version: int = Field(default=1, index=True)
    name: str = ""
    status: str = Field(default="pending", index=True)
    total: int = 0
    concurrency: int = 1
    input_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_input(self) -> dict:
        data = json.loads(self.input_json or "{}")
        return data if isinstance(data, dict) else {}

    def set_input(self, data: dict):
        self.input_json = json.dumps(data or {}, ensure_ascii=False, default=str)


class WorkflowRunModel(SQLModel, table=True):
    __tablename__ = "workflow_runs"

    id: str = Field(primary_key=True)
    batch_id: str = Field(default="", index=True)
    batch_item_index: int = Field(default=0, index=True)
    definition_key: str = Field(index=True)
    definition_version: int = Field(default=1, index=True)
    name: str = ""
    status: str = Field(default="pending", index=True)
    input_json: str = "{}"
    context_json: str = "{}"
    output_json: str = "{}"
    definition_json: str = "{}"
    metadata_json: str = "{}"
    current_step_id: str = ""
    error: str = ""
    cancellation_requested_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def _get_json(self, value: str) -> dict:
        return json.loads(value or "{}")

    def get_input(self) -> dict:
        return self._get_json(self.input_json)

    def get_context(self) -> dict:
        return self._get_json(self.context_json)

    def get_output(self) -> dict:
        return self._get_json(self.output_json)

    def get_definition(self) -> dict:
        return self._get_json(self.definition_json)

    def get_metadata(self) -> dict:
        return self._get_json(self.metadata_json)


class WorkflowStepRunModel(SQLModel, table=True):
    __tablename__ = "workflow_step_runs"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "step_id", name="uq_workflow_run_step"),
        Index("ix_workflow_steps_due", "status", "next_run_at"),
    )

    id: str = Field(primary_key=True)
    workflow_run_id: str = Field(index=True, foreign_key="workflow_runs.id", ondelete="CASCADE")
    step_id: str = Field(index=True)
    name: str = ""
    adapter_key: str = Field(index=True)
    status: str = Field(default="pending", index=True)
    attempt: int = 0
    max_attempts: int = 1
    input_json: str = "{}"
    output_json: str = "{}"
    error_json: str = "{}"
    external_ref: str = Field(default="", index=True)
    idempotency_key: str = Field(default="", index=True)
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    timeout_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_input(self) -> dict:
        return json.loads(self.input_json or "{}")

    def get_output(self) -> dict:
        return json.loads(self.output_json or "{}")

    def get_error(self) -> dict:
        return json.loads(self.error_json or "{}")


class WorkflowEventModel(SQLModel, table=True):
    __tablename__ = "workflow_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    workflow_run_id: str = Field(index=True, foreign_key="workflow_runs.id", ondelete="CASCADE")
    step_id: str = Field(default="", index=True)
    type: str = Field(default="log", index=True)
    level: str = "info"
    message: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)

    def get_detail(self) -> dict:
        return json.loads(self.detail_json or "{}")


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True)
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None


def _save_account_in_session(session: Session, account) -> 'AccountModel':
    from core.account_graph import sync_platform_account_graph

    existing = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == account.platform)
        .where(AccountModel.email == account.email)
    ).first()
    if existing:
        existing.password = account.password
        existing.user_id = account.user_id or ""
        existing.updated_at = _utcnow()
        session.add(existing)
        session.flush()
        sync_platform_account_graph(session, existing, account)
        session.flush()
        return existing
    model = AccountModel(
        platform=account.platform,
        email=account.email,
        password=account.password,
        user_id=account.user_id or "",
    )
    session.add(model)
    session.flush()
    sync_platform_account_graph(session, model, account)
    session.flush()
    return model


def save_account(account, *, session: Session | None = None, commit: bool = True) -> 'AccountModel':
    """Persist an account; optionally join a caller-owned transaction."""

    if session is not None:
        model = _save_account_in_session(session, account)
        if commit:
            session.commit()
            session.refresh(model)
        return model

    with Session(engine) as owned_session:
        model = _save_account_in_session(owned_session, account)
        owned_session.commit()
        owned_session.refresh(model)
        return model


def init_db():
    SQLModel.metadata.create_all(engine)
    from core.account_graph import sync_all_account_graphs
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    _ensure_column("provider_definitions", "category", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "scanner_driver", "TEXT DEFAULT 'customer_api'")
    _ensure_column("account_status", "invalid_check_count", "INTEGER DEFAULT 0")
    _ensure_column("kakao_pipelines", "supplier_processing_started_at", "DATETIME")
    _ensure_column("kakao_pipelines", "supplier_deadline_at", "DATETIME")
    _ensure_column("kakao_pipelines", "scanner_submit_attempts", "INTEGER DEFAULT 0")
    _ensure_column("kakao_pipelines", "scanner_compensation_attempted", "BOOLEAN DEFAULT 0")
    _ensure_column("kakao_pipelines", "scanner_poll_failures", "INTEGER DEFAULT 0")
    _ensure_column("kakao_pipelines", "scanner_recovery_reason", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "scanner_recovery_check_count", "INTEGER DEFAULT 0")
    _ensure_column("kakao_pipelines", "scanner_recovery_started_at", "DATETIME")
    _ensure_column("kakao_pipelines", "scanner_recovery_next_check_at", "DATETIME")
    _ensure_column("kakao_pipelines", "scanner_recovery_deadline_at", "DATETIME")
    _ensure_column("kakao_pipelines", "scanner_processing_started_at", "DATETIME")
    _ensure_column("kakao_pipelines", "scanner_deadline_at", "DATETIME")
    _ensure_column("kakao_pipelines", "completion_source", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "plus_check_count", "INTEGER DEFAULT 0")
    _ensure_column("kakao_pipelines", "plus_check_started_at", "DATETIME")
    _ensure_column("kakao_pipelines", "plus_next_check_at", "DATETIME")
    _ensure_column("kakao_pipelines", "plus_check_deadline_at", "DATETIME")
    _ensure_column("kakao_pipelines", "plus_check_paused_at", "DATETIME")
    _ensure_column("kakao_pipelines", "codex_post_action_armed", "BOOLEAN DEFAULT 0")
    _ensure_column("kakao_pipelines", "codex_task_id", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "codex_attempt_count", "INTEGER DEFAULT 0")
    _ensure_column("kakao_pipelines", "codex_interrupted_retry_count", "INTEGER DEFAULT 0")
    _ensure_column("kakao_pipelines", "codex_skipped_at", "DATETIME")
    _ensure_column("kakao_pipelines", "codex_enqueue_error", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "codex_push_task_id", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "codex_push_attempt_count", "INTEGER DEFAULT 0")
    _ensure_column("kakao_pipelines", "codex_push_skip_reason", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "codex_push_enqueue_error", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "codex_post_action_done_at", "DATETIME")
    _ensure_column("kakao_pipelines", "archived_at", "DATETIME")
    _ensure_column("kakao_pipelines", "archive_reason", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "archive_disposition", "TEXT DEFAULT ''")
    _ensure_column("kakao_pipelines", "purged_at", "DATETIME")
    _ensure_index(
        "accounts",
        "ix_accounts_platform_created_id",
        ("platform", "created_at", "id"),
    )
    _ensure_index(
        "kakao_pipelines",
        "ix_kakao_pipelines_archive_state_updated",
        ("archived_at", "state", "final_result", "updated_at", "id"),
    )
    _ensure_index(
        "kakao_pipelines",
        "ix_kakao_pipelines_archive_purged",
        ("archived_at", "purged_at"),
    )
    _ensure_column("workflow_runs", "batch_id", "TEXT DEFAULT ''")
    _ensure_column("workflow_runs", "batch_item_index", "INTEGER DEFAULT 0")
    _ensure_column("workflow_runs", "metadata_json", "TEXT DEFAULT '{}'")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        ProviderDefinitionsRepository().ensure_seeded()
        _migrate_legacy_provider_keys()
        _cleanup_non_real_providers()
        _cleanup_empty_provider_settings()
        sync_all_account_graphs(session)
        session.commit()

    # Any active allocation that survived a process restart has no live
    # registration worker. Preserve the attempt as interrupted and return its
    # mailbox immediately, as required by the domain policy.
    from core.mailbox_lifecycle import MailboxAllocationLifecycle

    mailbox_lifecycle = MailboxAllocationLifecycle()
    if not os.getenv("PYTEST_CURRENT_TEST"):
        mailbox_lifecycle.migrate_legacy_json_once()
    mailbox_lifecycle.interrupt_active()


def _ensure_column(table: str, column: str, col_type: str):
    """给已有表安全地加一列（SQLite 不支持 IF NOT EXISTS ADD COLUMN）。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if table not in tables:
        return
    existing = {c["name"] for c in inspector.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    print(f"[DB] 已添加列 {table}.{column}")


def _ensure_index(table: str, index_name: str, columns: tuple[str, ...]) -> None:
    """Create a simple index for an existing table after its columns migrate."""

    inspector = inspect(engine)
    if table not in set(inspector.get_table_names()):
        return
    existing_columns = {item["name"] for item in inspector.get_columns(table)}
    if not columns or any(column not in existing_columns for column in columns):
        return
    existing_indexes = {item["name"] for item in inspector.get_indexes(table)}
    if index_name in existing_indexes:
        return
    column_sql = ", ".join(columns)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column_sql})"
        )
    print(f"[DB] 已添加索引 {index_name}")


def _cleanup_empty_provider_settings():
    """清理 v1.0.7/v1.0.8 中 PR #42 自动创建的空 ProviderSetting。

    判定条件：config / auth / metadata 三个字段都为空 dict 时认为
    用户从未编辑过，可以安全删除。被删后用户能从前端"新增"按钮
    重新选择对应的 provider。"""
    with Session(engine) as session:
        items = session.exec(select(ProviderSettingModel)).all()
        removed = 0
        for item in items:
            config = item.get_config() or {}
            auth = item.get_auth() or {}
            metadata = item.get_metadata() or {}
            if not config and not auth and not metadata:
                session.delete(item)
                removed += 1
        if removed:
            session.commit()


# 旧版 provider_key → 新版 provider_key 映射
_LEGACY_PROVIDER_KEY_MAP: dict[tuple[str, str], str] = {
    # captcha
    ("captcha", "yescaptcha"): "yescaptcha_api",
    ("captcha", "twocaptcha"): "twocaptcha_api",
}

# 旧版 auth_mode 值 → 新版 auth_mode 值映射
_LEGACY_AUTH_MODE_MAP: dict[str, str] = {
    "endpoint_only": "password",
    "manual_login": "password",
    "bearer_token": "bearer",
    "jwt_token": "token",
    "admin_token": "token",
    "api_key": "apikey",
}


def _migrate_legacy_provider_keys():
    """将旧版 provider_key 和 auth_mode 迁移到新版命名。

    同时迁移 provider_settings 和 provider_definitions 两张表。
    如果新 key 已存在则删除旧记录（避免唯一约束冲突）。
    迁移后还会修正 auth_mode 值，使其匹配新版 definition 的有效值。
    """
    with Session(engine) as session:
        migrated = 0

        # 1. 迁移 provider_key
        for (ptype, old_key), new_key in _LEGACY_PROVIDER_KEY_MAP.items():
            # --- provider_settings ---
            old_setting = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == ptype)
                .where(ProviderSettingModel.provider_key == old_key)
            ).first()
            if old_setting:
                new_setting = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == ptype)
                    .where(ProviderSettingModel.provider_key == new_key)
                ).first()
                if new_setting:
                    session.delete(old_setting)
                else:
                    old_setting.provider_key = new_key
                    session.add(old_setting)
                migrated += 1

            # --- provider_definitions ---
            old_defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == ptype)
                .where(ProviderDefinitionModel.provider_key == old_key)
            ).first()
            if old_defn:
                new_defn = session.exec(
                    select(ProviderDefinitionModel)
                    .where(ProviderDefinitionModel.provider_type == ptype)
                    .where(ProviderDefinitionModel.provider_key == new_key)
                ).first()
                if new_defn:
                    session.delete(old_defn)
                else:
                    old_defn.provider_key = new_key
                    session.add(old_defn)
                migrated += 1

        if migrated:
            session.commit()
            print(f"[DB] 已迁移 {migrated} 条旧版 provider key")

        # 2. 修正 auth_mode 值
        fixed = 0
        all_settings = session.exec(select(ProviderSettingModel)).all()
        for item in all_settings:
            old_mode = item.auth_mode or ""
            if not old_mode:
                continue
            # 查找对应的 definition
            defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == item.provider_type)
                .where(ProviderDefinitionModel.provider_key == item.provider_key)
            ).first()
            if not defn:
                continue
            valid_modes = {m.get("value") for m in defn.get_auth_modes()}
            if not valid_modes or old_mode in valid_modes:
                # 当前值已经有效，跳过
                continue
            # 尝试映射
            new_mode = _LEGACY_AUTH_MODE_MAP.get(old_mode)
            if new_mode and new_mode in valid_modes:
                item.auth_mode = new_mode
            elif defn.default_auth_mode:
                item.auth_mode = defn.default_auth_mode
            else:
                continue
            session.add(item)
            fixed += 1

        if fixed:
            session.commit()
            print(f"[DB] 已修正 {fixed} 条旧版 auth_mode")


def _cleanup_non_real_providers():
    """generic_http 不是真实邮箱，从 DB 中清除其 definition 和空 setting。"""
    remove_keys = [("mailbox", "generic_http")]
    with Session(engine) as session:
        for pt, pk in remove_keys:
            setting = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == pt)
                .where(ProviderSettingModel.provider_key == pk)
            ).first()
            if setting:
                config = setting.get_config() or {}
                auth = setting.get_auth() or {}
                if not config and not auth:
                    session.delete(setting)
            defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == pt)
                .where(ProviderDefinitionModel.provider_key == pk)
            ).first()
            if defn:
                remaining = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == pt)
                    .where(ProviderSettingModel.provider_key == pk)
                ).first()
                if not remaining:
                    session.delete(defn)
        session.commit()


def get_session():
    with Session(engine) as session:
        yield session
