from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from core.db import AccountPushDeliveryModel, engine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountPushDeliveriesRepository:
    def mark_pending(
        self,
        account_ids: list[int],
        *,
        target_key: str,
        target_label: str,
        payload_format: str,
    ) -> None:
        if not account_ids:
            return
        now = _utcnow()
        with Session(engine) as session:
            existing = session.exec(
                select(AccountPushDeliveryModel)
                .where(AccountPushDeliveryModel.account_id.in_(account_ids))
                .where(AccountPushDeliveryModel.target_key == target_key)
            ).all()
            by_account = {item.account_id: item for item in existing}
            for account_id in account_ids:
                item = by_account.get(account_id) or AccountPushDeliveryModel(
                    account_id=account_id,
                    target_key=target_key,
                )
                item.target_label = target_label
                item.payload_format = payload_format
                item.status = "pending"
                item.attempt_count = int(item.attempt_count or 0) + 1
                item.http_status = 0
                item.last_error = ""
                item.last_attempt_at = now
                item.updated_at = now
                session.add(item)
            session.commit()

    def record_results(self, results: list[dict], *, target_key: str) -> None:
        if not results:
            return
        now = _utcnow()
        account_ids = [int(result["account_id"]) for result in results]
        with Session(engine) as session:
            items = session.exec(
                select(AccountPushDeliveryModel)
                .where(AccountPushDeliveryModel.account_id.in_(account_ids))
                .where(AccountPushDeliveryModel.target_key == target_key)
            ).all()
            by_account = {item.account_id: item for item in items}
            for result in results:
                account_id = int(result["account_id"])
                item = by_account.get(account_id)
                if not item:
                    continue
                item.status = "success" if result.get("ok") else "failed"
                item.http_status = int(result.get("http_status") or 0)
                item.last_error = str(result.get("error") or "")[:500]
                if result.get("ok"):
                    item.pushed_at = now
                item.updated_at = now
                session.add(item)
            session.commit()
