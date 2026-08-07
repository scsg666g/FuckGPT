"""Durable workflow runtime for single-process orchestration."""
from __future__ import annotations

import os
import threading
from application.workflows import recover_incomplete_workflow_runs, run_due_workflow_once


def _bounded_int(value: object, default: int, *, minimum: int = 1, maximum: int = 50) -> int:
    try:
        result = int(value if value not in (None, "") else default)
    except Exception:
        result = default
    return min(max(result, minimum), maximum)


class WorkflowRuntime:
    def __init__(self, *, poll_interval: float = 0.5, max_parallel_steps: int | None = None):
        self.poll_interval = poll_interval
        configured_parallelism = (
            max_parallel_steps
            if max_parallel_steps is not None
            else os.environ.get("WORKFLOW_MAX_PARALLEL_STEPS")
        )
        self.max_parallel_steps = _bounded_int(configured_parallelism, 5, minimum=1, maximum=50)
        self._running = False
        self._workers: list[threading.Thread] = []
        self._wake_event = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            recover_incomplete_workflow_runs()
            self._workers = [
                threading.Thread(
                    target=self._worker_loop,
                    args=(index + 1,),
                    daemon=True,
                    name=f"workflow-worker-{index + 1}",
                )
                for index in range(self.max_parallel_steps)
            ]
            for worker in self._workers:
                worker.start()
            print(f"[WorkflowRuntime] 已启动，并发={self.max_parallel_steps}")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        self._wake_event.set()
        for worker in list(self._workers):
            if worker is not threading.current_thread():
                worker.join(timeout=2.0)
        with self._lock:
            self._workers = []
        print("[WorkflowRuntime] 停止中")

    def wake_up(self) -> None:
        self._wake_event.set()

    def _worker_loop(self, worker_index: int) -> None:
        del worker_index
        while self._running:
            progressed = False
            while self._running and run_due_workflow_once():
                progressed = True
            if progressed:
                continue
            self._wake_event.wait(self.poll_interval)
            self._wake_event.clear()


workflow_runtime = WorkflowRuntime()
