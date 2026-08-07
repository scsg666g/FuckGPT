"""Run a blocking worker in a disposable child process.

Browser automation libraries can occasionally block below their advertised
timeouts when the browser/driver transport disappears.  A Python thread cannot
be stopped safely in that state, so callers that need a hard boundary should
run the blocking operation here and supervise it from the parent process.
"""

from __future__ import annotations

import importlib
import multiprocessing
import os
import signal
import subprocess
import time
import traceback
import uuid
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any, Callable


class IsolatedWorkerError(RuntimeError):
    """Base error raised by the parent-side worker supervisor."""


class IsolatedWorkerRemoteError(IsolatedWorkerError):
    """The child completed by raising an exception."""

    def __init__(self, message: str, *, preserve_mailbox: bool = False):
        super().__init__(message)
        self.preserve_mailbox = bool(preserve_mailbox)


class IsolatedWorkerStalledError(IsolatedWorkerError):
    """The child stopped producing observable activity."""


class IsolatedWorkerDeadlineError(IsolatedWorkerError):
    """The child exceeded its total runtime limit."""


@dataclass(slots=True)
class IsolatedCall:
    callable_path: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] | None = None
    pass_channel: bool = True


class ChildChannel:
    """Child-side log and parent-callback bridge."""

    def __init__(self, connection: Connection):
        self._connection = connection

    def log(self, message: str) -> None:
        self._connection.send({"type": "log", "message": str(message)})

    def callback(
        self,
        name: str,
        *,
        attribute_values: dict[str, Any] | None = None,
    ) -> "RemoteCallback":
        return RemoteCallback(
            self._connection,
            str(name),
            attribute_values=attribute_values,
        )


class RemoteCallback:
    """Callable proxy whose implementation remains in the parent process."""

    def __init__(
        self,
        connection: Connection,
        name: str,
        *,
        attribute_values: dict[str, Any] | None = None,
    ):
        self._connection = connection
        self._name = str(name)
        self._attribute_values = dict(attribute_values or {})

    def _invoke(self, method: str, *args, **kwargs):
        request_id = uuid.uuid4().hex
        self._connection.send(
            {
                "type": "callback",
                "name": self._name,
                "method": str(method),
                "request_id": request_id,
                "args": args,
                "kwargs": kwargs,
            }
        )
        while True:
            response = self._connection.recv()
            if response.get("type") != "callback_result":
                continue
            if response.get("request_id") != request_id:
                continue
            if response.get("ok"):
                return response.get("result")
            raise RuntimeError(str(response.get("error") or "父进程回调失败"))

    def __call__(self, *args, **kwargs):
        return self._invoke("__call__", *args, **kwargs)

    def __getattr__(self, name: str):
        if name in self._attribute_values:
            return self._attribute_values[name]

        def remote_method(*args, **kwargs):
            return self._invoke(name, *args, **kwargs)

        return remote_method


def _resolve_callable(path: str) -> Callable[..., Any]:
    module_name, separator, attribute_name = str(path or "").rpartition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError(f"无效的子进程 callable_path: {path!r}")
    module = importlib.import_module(module_name)
    target = getattr(module, attribute_name)
    if not callable(target):
        raise TypeError(f"子进程目标不可调用: {path}")
    return target


def _isolated_child_main(connection: Connection, call: IsolatedCall) -> None:
    if os.name != "nt":
        try:
            os.setsid()
        except Exception:
            pass
    try:
        target = _resolve_callable(call.callable_path)
        args = tuple(call.args or ())
        kwargs = dict(call.kwargs or {})
        if call.pass_channel:
            result = target(ChildChannel(connection), *args, **kwargs)
        else:
            result = target(*args, **kwargs)
        connection.send({"type": "result", "result": result})
    except BaseException as exc:  # child must report ordinary and fatal failures
        try:
            connection.send(
                {
                    "type": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "preserve_mailbox": bool(getattr(exc, "preserve_mailbox", False)),
                }
            )
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _terminate_process_tree(process: multiprocessing.Process) -> None:
    """Terminate the worker and browser/driver descendants it launched."""
    pid = int(process.pid or 0)
    if pid <= 0:
        return

    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=creationflags,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass

    if process.is_alive():
        try:
            process.terminate()
        except Exception:
            pass
    process.join(timeout=3)
    if process.is_alive():
        try:
            process.kill()
        except Exception:
            pass
        process.join(timeout=2)


def _execute_parent_callback(callback: Callable[..., Any], message: dict[str, Any]) -> Any:
    method = str(message.get("method") or "__call__")
    target = callback if method == "__call__" else getattr(callback, method)
    return target(*tuple(message.get("args") or ()), **dict(message.get("kwargs") or {}))


def run_isolated_call(
    call: IsolatedCall,
    *,
    callbacks: dict[str, Callable[..., Any]] | None = None,
    log_fn: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    idle_timeout: float = 120.0,
    hard_timeout: float | None = None,
    poll_interval: float = 0.2,
) -> Any:
    """Run *call* under a parent-side inactivity and total-runtime watchdog."""
    idle_limit = max(float(idle_timeout), 1.0)
    configured_hard_limit = float(hard_timeout or 0)
    hard_limit = (
        max(configured_hard_limit, idle_limit)
        if configured_hard_limit > 0
        else None
    )
    poll_seconds = min(max(float(poll_interval), 0.02), 1.0)
    callback_map = dict(callbacks or {})
    emit = log_fn if callable(log_fn) else (lambda _message: None)
    is_cancelled = cancel_check if callable(cancel_check) else (lambda: False)

    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=True)
    process = context.Process(
        target=_isolated_child_main,
        args=(child_connection, call),
        name="browser-worker",
    )
    process.start()
    child_connection.close()
    started_at = time.monotonic()
    last_activity_at = started_at
    warning_logged = False
    completed = False

    emit(
        f"浏览器独立子进程已启动: pid={process.pid}, "
        f"无活动超时={idle_limit:g}s, "
        f"总时限={f'{hard_limit:g}s' if hard_limit is not None else '关闭'}"
    )
    try:
        while True:
            if is_cancelled():
                raise RuntimeError("任务已取消")

            now = time.monotonic()
            elapsed = now - started_at
            inactive = now - last_activity_at
            if hard_limit is not None and elapsed >= hard_limit:
                raise IsolatedWorkerDeadlineError(
                    f"浏览器 Worker 超过总时限 {hard_limit:g}s，已终止子进程 pid={process.pid}"
                )
            if inactive >= idle_limit:
                raise IsolatedWorkerStalledError(
                    f"浏览器 Worker 连续 {idle_limit:g}s 无日志/回调/结果，已终止子进程 pid={process.pid}"
                )
            if not warning_logged and inactive >= max(idle_limit / 2, 10.0):
                warning_logged = True
                emit(
                    f"浏览器 Worker 已 {inactive:.0f}s 无活动，watchdog 将在 "
                    f"{idle_limit:g}s 时终止该子进程"
                )

            if parent_connection.poll(poll_seconds):
                try:
                    message = parent_connection.recv()
                except EOFError:
                    message = None
                last_activity_at = time.monotonic()
                warning_logged = False
                if not isinstance(message, dict):
                    if not process.is_alive():
                        raise IsolatedWorkerError(
                            f"浏览器 Worker 异常退出且未返回结果: pid={process.pid}, exitcode={process.exitcode}"
                        )
                    continue

                message_type = message.get("type")
                if message_type == "log":
                    emit(str(message.get("message") or ""))
                    continue
                if message_type == "callback":
                    request_id = str(message.get("request_id") or "")
                    callback_name = str(message.get("name") or "")
                    callback = callback_map.get(callback_name)
                    try:
                        if not callable(callback):
                            raise RuntimeError(f"父进程未提供回调: {callback_name}")
                        callback_result = _execute_parent_callback(callback, message)
                        response = {
                            "type": "callback_result",
                            "request_id": request_id,
                            "ok": True,
                            "result": callback_result,
                        }
                    except BaseException as exc:
                        response = {
                            "type": "callback_result",
                            "request_id": request_id,
                            "ok": False,
                            "error": str(exc),
                        }
                    parent_connection.send(response)
                    last_activity_at = time.monotonic()
                    continue
                if message_type == "error":
                    error_type = str(message.get("error_type") or "RuntimeError")
                    error_message = str(message.get("error") or "子进程执行失败")
                    raise IsolatedWorkerRemoteError(
                        f"{error_type}: {error_message}",
                        preserve_mailbox=bool(message.get("preserve_mailbox")),
                    )
                if message_type == "result":
                    completed = True
                    return message.get("result")

            if not process.is_alive():
                # The pipe may become readable a fraction after process exit.
                if parent_connection.poll(0.1):
                    continue
                raise IsolatedWorkerError(
                    f"浏览器 Worker 异常退出且未返回结果: pid={process.pid}, exitcode={process.exitcode}"
                )
    finally:
        try:
            parent_connection.close()
        except Exception:
            pass
        if completed:
            process.join(timeout=3)
        if process.is_alive():
            _terminate_process_tree(process)
