"""Persistent task runtime for single-process execution."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
import time

from application.tasks import (
    claim_next_runnable_task,
    execute_task,
    mark_incomplete_tasks_interrupted,
)


@dataclass(slots=True)
class TaskWorkerState:
    thread: threading.Thread
    platform: str = ""
    task_type: str = ""
    scope: str = ""
    account_keys: set[str] = field(default_factory=set)


def _bounded_int(value: object, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except Exception:
        result = default
    return min(max(result, minimum), maximum)


class TaskRuntime:
    def __init__(
        self,
        *,
        max_parallel_per_type: int | None = None,
        # Backward-compatible constructor alias.  It no longer represents a
        # global limit; callers should migrate to ``max_parallel_per_type``.
        max_parallel_tasks: int | None = None,
        max_parallel_per_scope: int | None = None,
        poll_interval: float = 0.5,
    ):
        configured_per_type = max_parallel_per_type
        if configured_per_type is None:
            configured_per_type = max_parallel_tasks
        if configured_per_type is None:
            configured_per_type = os.environ.get("TASK_MAX_PARALLEL_PER_TYPE")
        if configured_per_type in (None, ""):
            # Preserve the old setting as a per-type limit.  It no longer
            # limits the sum of unrelated task types.
            configured_per_type = os.environ.get("TASK_MAX_PARALLEL")
        self.max_parallel_per_type = _bounded_int(
            configured_per_type,
            10,
            minimum=1,
            maximum=10,
        )
        # Read-only compatibility for callers that still inspect the old
        # attribute.  Its meaning is now explicitly per task type.
        self.max_parallel_tasks = self.max_parallel_per_type
        self.max_parallel_per_scope = _bounded_int(
            max_parallel_per_scope
            if max_parallel_per_scope is not None
            else os.environ.get("TASK_MAX_PARALLEL_PER_SCOPE"),
            10,
            minimum=1,
            maximum=10,
        )
        self.poll_interval = poll_interval
        self._running = False
        self._dispatcher: threading.Thread | None = None
        self._workers: dict[str, TaskWorkerState] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            mark_incomplete_tasks_interrupted()
            self._dispatcher = threading.Thread(target=self._loop, daemon=True, name="task-runtime")
            self._dispatcher.start()
            print("[TaskRuntime] 已启动")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        print("[TaskRuntime] 停止中")

    def wake_up(self) -> None:
        # Polling loop wakes quickly already; this method exists as an explicit runtime hook.
        return

    def _loop(self) -> None:
        while self._running:
            self._reap_workers()
            running_type_counts, running_scope_counts, busy_account_keys = self._accounting_snapshot()
            while self._running:
                task_info = claim_next_runnable_task(
                    running_type_counts=running_type_counts,
                    running_scope_counts=running_scope_counts,
                    busy_account_keys=busy_account_keys,
                    max_parallel_per_type=self.max_parallel_per_type,
                    max_parallel_per_scope=self.max_parallel_per_scope,
                )
                if not task_info:
                    break
                task_id = task_info["id"]
                worker = threading.Thread(
                    target=self._run_task,
                    args=(task_id,),
                    daemon=True,
                    name=f"task-worker-{task_id}",
                )
                with self._lock:
                    self._workers[task_id] = TaskWorkerState(
                        thread=worker,
                        platform=str(task_info.get("platform", "") or ""),
                        task_type=str(task_info.get("type", "") or ""),
                        scope=str(task_info.get("scope", "") or ""),
                        account_keys=set(task_info.get("account_keys") or []),
                    )
                    task_type = str(task_info.get("type", "") or "")
                    running_type_counts[task_type] = running_type_counts.get(task_type, 0) + 1
                    if task_info.get("scope"):
                        scope = str(task_info["scope"])
                        running_scope_counts[scope] = running_scope_counts.get(scope, 0) + 1
                    busy_account_keys.update(set(task_info.get("account_keys") or []))
                worker.start()
            time.sleep(self.poll_interval)
        self._reap_workers()

    def _accounting_snapshot(self) -> tuple[dict[str, int], dict[str, int], set[str]]:
        with self._lock:
            workers = list(self._workers.items())

        running_type_counts: dict[str, int] = {}
        running_scope_counts: dict[str, int] = {}
        busy_account_keys: set[str] = set()
        for _task_id, state in workers:
            # A cancel-requested task may still be inside a remote call, so it
            # keeps its type slot and account locks until the worker exits.
            if not state.thread.is_alive():
                continue
            running_type_counts[state.task_type] = running_type_counts.get(state.task_type, 0) + 1
            if state.scope:
                running_scope_counts[state.scope] = running_scope_counts.get(state.scope, 0) + 1
            busy_account_keys.update(state.account_keys)
        return running_type_counts, running_scope_counts, busy_account_keys

    def _run_task(self, task_id: str) -> None:
        # Keep the worker registered through the thread's actual lifetime.
        # The dispatcher reaps it only after ``is_alive()`` becomes false, so
        # there is no window where a finishing task releases its type slot
        # before its worker has exited.
        execute_task(task_id)

    def _reap_workers(self) -> None:
        with self._lock:
            finished = [task_id for task_id, worker in self._workers.items() if not worker.thread.is_alive()]
            for task_id in finished:
                self._workers.pop(task_id, None)


task_runtime = TaskRuntime()
