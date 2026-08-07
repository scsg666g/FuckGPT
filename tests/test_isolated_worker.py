from __future__ import annotations

import subprocess
import sys
import time

import pytest

from core.isolated_worker import (
    IsolatedCall,
    IsolatedWorkerRemoteError,
    IsolatedWorkerStalledError,
    run_isolated_call,
)


def _callback_worker(channel, value):
    channel.log(f"child:{value}")
    return channel.callback("double")(value)


def _preserved_error_worker(channel):
    class PreservedError(RuntimeError):
        preserve_mailbox = True

    raise PreservedError("existing account")


def _silent_worker(channel, seconds):
    time.sleep(seconds)


def _silent_worker_with_descendant(channel, marker_path):
    code = (
        "import pathlib,time; "
        "time.sleep(2); "
        f"pathlib.Path({marker_path!r}).write_text('alive', encoding='utf-8')"
    )
    subprocess.Popen([sys.executable, "-c", code])
    time.sleep(30)


def test_isolated_worker_forwards_logs_and_parent_callbacks():
    logs = []

    result = run_isolated_call(
        IsolatedCall(
            callable_path="tests.test_isolated_worker:_callback_worker",
            args=(21,),
        ),
        callbacks={"double": lambda value: value * 2},
        log_fn=logs.append,
        idle_timeout=5,
        hard_timeout=10,
    )

    assert result == 42
    assert "child:21" in logs
    assert any("浏览器独立子进程已启动" in item for item in logs)


def test_isolated_worker_preserves_mailbox_error_metadata():
    with pytest.raises(IsolatedWorkerRemoteError) as captured:
        run_isolated_call(
            IsolatedCall(
                callable_path="tests.test_isolated_worker:_preserved_error_worker",
            ),
            idle_timeout=5,
            hard_timeout=10,
        )

    assert captured.value.preserve_mailbox is True
    assert "existing account" in str(captured.value)


def test_isolated_worker_watchdog_stops_silent_child():
    started_at = time.monotonic()
    with pytest.raises(IsolatedWorkerStalledError):
        run_isolated_call(
            IsolatedCall(
                callable_path="tests.test_isolated_worker:_silent_worker",
                args=(30,),
            ),
            idle_timeout=1,
            hard_timeout=10,
            poll_interval=0.05,
        )

    assert time.monotonic() - started_at < 6


def test_watchdog_terminates_worker_descendant_process(tmp_path):
    marker = tmp_path / "descendant-survived.txt"

    with pytest.raises(IsolatedWorkerStalledError):
        run_isolated_call(
            IsolatedCall(
                callable_path="tests.test_isolated_worker:_silent_worker_with_descendant",
                args=(str(marker),),
            ),
            idle_timeout=1,
            hard_timeout=10,
            poll_interval=0.05,
        )

    time.sleep(2.5)
    assert not marker.exists()
