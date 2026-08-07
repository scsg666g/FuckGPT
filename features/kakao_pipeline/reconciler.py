from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .service import BACKGROUND_POLL_STATES, KakaoPipelineService


logger = logging.getLogger(__name__)

_REMOTE_POLL_DELAY_SECONDS = 3.0
_ERROR_RETRY_DELAYS_SECONDS = (3.0, 5.0, 10.0, 20.0, 30.0, 60.0)


@dataclass
class _Schedule:
    next_poll_at: float = 0.0
    failures: int = 0


class KakaoPipelineReconciler:
    """Advance persisted Kakao work independently from the visible account page."""

    def __init__(
        self,
        service: KakaoPipelineService | None = None,
        *,
        scan_interval: float = 1.0,
        max_workers: int = 4,
        work_limit: int = 500,
    ) -> None:
        self.service = service or KakaoPipelineService()
        self.scan_interval = max(float(scan_interval), 0.05)
        self.max_workers = min(max(int(max_workers), 1), 16)
        self.work_limit = min(max(int(work_limit), self.max_workers), 500)
        self._guard = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._inflight: set[int] = set()
        self._schedules: dict[int, _Schedule] = {}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    def start(self) -> None:
        with self._guard:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="kakao-pipeline",
            )
            self._thread = threading.Thread(
                target=self._loop,
                name="kakao-pipeline-reconciler",
                daemon=True,
            )
            self._thread.start()
        logger.info("Kakao 后台流水线已启动，并发=%s", self.max_workers)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        deadline = time.monotonic() + max(float(timeout), 0.0)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(deadline - time.monotonic(), 0.0))
        with self._guard:
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        while time.monotonic() < deadline:
            with self._guard:
                if not self._inflight:
                    break
            time.sleep(0.02)
        with self._guard:
            self._executor = None
            self._thread = None
        logger.info("Kakao 后台流水线已停止")

    def run_once(self) -> int:
        """Schedule one scan. Exposed for deterministic tests and diagnostics."""
        executor = self._executor
        if executor is None or self._stop_event.is_set():
            return 0

        work = self.service.list_background_work(limit=self.work_limit)
        active_ids = {
            int(item.get("account_id") or 0)
            for item in work
            if int(item.get("account_id") or 0) > 0
        }
        now = time.monotonic()
        scheduled = 0

        with self._guard:
            for account_id in list(self._schedules):
                if account_id not in active_ids and account_id not in self._inflight:
                    self._schedules.pop(account_id, None)

            available = max(self.max_workers - len(self._inflight), 0)
            if available <= 0:
                return 0

            for item in work:
                if scheduled >= available:
                    break
                account_id = int(item.get("account_id") or 0)
                state = str(item.get("state") or "")
                if account_id <= 0 or state not in BACKGROUND_POLL_STATES or account_id in self._inflight:
                    continue
                schedule = self._schedules.setdefault(account_id, _Schedule())
                if schedule.next_poll_at > now:
                    continue
                self._inflight.add(account_id)
                future = executor.submit(self.service.advance_background, account_id, expected_state=state)
                future.add_done_callback(
                    lambda completed, current_id=account_id: self._complete(current_id, completed)
                )
                scheduled += 1
        return scheduled

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("Kakao 后台流水线扫描失败")
            self._stop_event.wait(self.scan_interval)

    def _complete(self, account_id: int, future: Future[Any]) -> None:
        now = time.monotonic()
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001
            with self._guard:
                schedule = self._schedules.setdefault(account_id, _Schedule())
                schedule.failures += 1
                delay = _ERROR_RETRY_DELAYS_SECONDS[
                    min(schedule.failures - 1, len(_ERROR_RETRY_DELAYS_SECONDS) - 1)
                ]
                schedule.next_poll_at = now + delay
                self._inflight.discard(account_id)
            logger.warning("Kakao 后台刷新失败 account_id=%s: %s", account_id, exc)
            return

        state = (
            str(result.get("_background_state") or result.get("state") or "")
            if isinstance(result, dict)
            else ""
        )
        with self._guard:
            self._inflight.discard(account_id)
            if state not in BACKGROUND_POLL_STATES:
                self._schedules.pop(account_id, None)
                return

            schedule = self._schedules.setdefault(account_id, _Schedule())
            schedule.failures = 0
            schedule.next_poll_at = now + _REMOTE_POLL_DELAY_SECONDS


kakao_pipeline_reconciler = KakaoPipelineReconciler()
