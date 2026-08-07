import threading

from application import tasks as tasks_module
from application.tasks import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_CANCEL_REQUESTED,
    TASK_TYPE_ACCOUNT_CHECK_ALL,
    TASK_TYPE_ACCOUNT_PUSH,
    TASK_TYPE_CODEX_OAUTH_BATCH,
    TASK_TYPE_PLATFORM_ACTION,
    TASK_TYPE_REGISTER,
    TASK_STATUS_INTERRUPTED,
    TaskLogger,
    claim_next_runnable_task,
    create_task,
    get_task,
    request_cancel,
    _mutate_task,
)
from services.task_runtime import TaskRuntime, TaskWorkerState


def test_claim_allows_different_task_types_on_same_platform():
    first_register = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    blocked_register = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    account_check = create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform="chatgpt",
        payload={"limit": 1},
        progress_total=1,
    )

    claimed = claim_next_runnable_task()
    assert claimed["id"] == first_register["id"]

    next_claimed = claim_next_runnable_task(
        running_scope_counts={claimed["scope"]: 1},
        max_parallel_per_scope=1,
    )

    assert next_claimed["id"] == account_check["id"]
    assert next_claimed["id"] != blocked_register["id"]
    assert next_claimed["scope"] == "chatgpt:account_check_all"


def test_claim_allows_workflow_children_of_same_task_type():
    first = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={
            "count": 1,
            "source": "workflow",
            "workflow_run_id": "wf_100_a",
            "workflow_step_id": "register",
        },
        progress_total=1,
    )
    second = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={
            "count": 1,
            "source": "workflow",
            "workflow_run_id": "wf_200_b",
            "workflow_step_id": "register",
        },
        progress_total=1,
    )

    claimed = claim_next_runnable_task()
    assert claimed["id"] == first["id"]

    next_claimed = claim_next_runnable_task(
        running_scope_counts={claimed["scope"]: 1},
        busy_account_keys=set(claimed["account_keys"]),
        max_parallel_per_scope=1,
    )

    assert next_claimed["id"] == second["id"]
    assert next_claimed["scope"] != claimed["scope"]


def test_claim_keeps_workflow_children_serial_for_same_account():
    first = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [101],
            "source": "workflow",
            "workflow_run_id": "wf_100_a",
            "workflow_step_id": "codex",
        },
        progress_total=1,
    )
    second = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [101],
            "source": "workflow",
            "workflow_run_id": "wf_200_b",
            "workflow_step_id": "codex",
        },
        progress_total=1,
    )

    claimed = claim_next_runnable_task()
    assert claimed["id"] == first["id"]
    assert "account:101" in claimed["account_keys"]

    next_claimed = claim_next_runnable_task(
        running_scope_counts={claimed["scope"]: 1},
        busy_account_keys=set(claimed["account_keys"]),
        max_parallel_per_scope=1,
    )

    assert next_claimed is None


def test_single_account_codex_tasks_use_account_scopes_across_sources():
    manual = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [101],
            "source": "manual",
        },
        progress_total=1,
    )
    same_account_workflow = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [101],
            "source": "workflow",
            "workflow_run_id": "wf_same_account",
            "workflow_step_id": "codex",
        },
        progress_total=1,
    )
    background = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [102],
            "source": "background",
        },
        progress_total=1,
    )

    claimed_manual = claim_next_runnable_task()
    assert claimed_manual["id"] == manual["id"]
    assert claimed_manual["scope"] == "chatgpt:codex_oauth_batch:account:101"

    claimed_background = claim_next_runnable_task(
        running_scope_counts={claimed_manual["scope"]: 1},
        busy_account_keys=set(claimed_manual["account_keys"]),
        max_parallel_per_scope=1,
    )

    assert claimed_background["id"] == background["id"]
    assert claimed_background["scope"] == "chatgpt:codex_oauth_batch:account:102"
    assert get_task(same_account_workflow["id"])["status"] == "pending"


def test_multi_account_codex_batches_parallelize_only_when_accounts_do_not_overlap():
    first = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_ids": [201, 202], "source": "manual"},
        progress_total=2,
    )
    overlapping = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [202, 203],
            "source": "workflow",
            "workflow_run_id": "wf_overlap",
            "workflow_step_id": "codex",
        },
        progress_total=2,
    )
    disjoint = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [204, 205],
            "source": "background",
        },
        progress_total=2,
    )

    claimed_first = claim_next_runnable_task()
    assert claimed_first["id"] == first["id"]
    assert claimed_first["scope"].endswith(first["id"])

    claimed_disjoint = claim_next_runnable_task(
        running_scope_counts={claimed_first["scope"]: 1},
        busy_account_keys=set(claimed_first["account_keys"]),
        max_parallel_per_scope=1,
    )
    assert claimed_disjoint["id"] == disjoint["id"]
    assert claimed_disjoint["scope"].endswith(disjoint["id"])

    still_blocked = claim_next_runnable_task(
        running_scope_counts={
            claimed_first["scope"]: 1,
            claimed_disjoint["scope"]: 1,
        },
        busy_account_keys={
            *claimed_first["account_keys"],
            *claimed_disjoint["account_keys"],
        },
        max_parallel_per_scope=1,
    )
    assert still_blocked is None
    assert get_task(overlapping["id"])["status"] == "pending"


def test_each_task_type_gets_ten_independent_runtime_slots():
    codex_tasks = [
        create_task(
            task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
            platform="chatgpt" if account_id <= 6 else "other-platform",
            payload={
                "platform": "chatgpt" if account_id <= 6 else "other-platform",
                "account_ids": [account_id],
                "source": "manual",
            },
            progress_total=1,
        )
        for account_id in range(1, 12)
    ]
    refresh_tasks = [
        create_task(
            task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
            platform="chatgpt",
            payload={
                "platform": "chatgpt",
                "account_ids": [account_id],
            },
            progress_total=1,
        )
        for account_id in range(101, 111)
    ]

    running_type_counts: dict[str, int] = {}
    running_scope_counts: dict[str, int] = {}
    busy_account_keys: set[str] = set()
    claimed: list[dict] = []
    while True:
        task = claim_next_runnable_task(
            running_type_counts=running_type_counts,
            running_scope_counts=running_scope_counts,
            busy_account_keys=busy_account_keys,
            max_parallel_per_type=10,
            max_parallel_per_scope=10,
        )
        if task is None:
            break
        claimed.append(task)
        task_type = str(task["type"])
        running_type_counts[task_type] = running_type_counts.get(task_type, 0) + 1
        scope = str(task["scope"])
        running_scope_counts[scope] = running_scope_counts.get(scope, 0) + 1
        busy_account_keys.update(task["account_keys"])

    assert running_type_counts == {
        TASK_TYPE_CODEX_OAUTH_BATCH: 10,
        TASK_TYPE_ACCOUNT_CHECK_ALL: 10,
    }
    assert len(claimed) == 20
    assert sum(task["type"] == TASK_TYPE_CODEX_OAUTH_BATCH for task in claimed) == 10
    assert sum(task["type"] == TASK_TYPE_ACCOUNT_CHECK_ALL for task in claimed) == 10
    assert get_task(codex_tasks[-1]["id"])["status"] == "pending"
    assert all(get_task(task["id"])["status"] == "claimed" for task in refresh_tasks)


def test_different_task_types_still_serialize_the_same_account():
    codex = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_ids": [42], "source": "manual"},
        progress_total=1,
    )
    refresh = create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_ids": [42]},
        progress_total=1,
    )

    claimed = claim_next_runnable_task(
        max_parallel_per_type=10,
        max_parallel_per_scope=10,
    )
    assert claimed["id"] == codex["id"]

    blocked = claim_next_runnable_task(
        running_type_counts={TASK_TYPE_CODEX_OAUTH_BATCH: 1},
        running_scope_counts={claimed["scope"]: 1},
        busy_account_keys=set(claimed["account_keys"]),
        max_parallel_per_type=10,
        max_parallel_per_scope=10,
    )
    assert blocked is None
    assert get_task(refresh["id"])["status"] == "pending"


def test_claim_allows_codex_oauth_actions_for_different_accounts():
    first = create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 101, "action_id": "codex_oauth_authorize"},
        progress_total=1,
    )
    second = create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 102, "action_id": "codex_oauth_authorize"},
        progress_total=1,
    )

    claimed = claim_next_runnable_task()
    assert claimed["id"] == first["id"]
    next_claimed = claim_next_runnable_task(
        running_scope_counts={claimed["scope"]: 1},
        busy_account_keys=set(claimed["account_keys"]),
        max_parallel_per_scope=1,
    )

    assert next_claimed["id"] == second["id"]
    assert next_claimed["scope"] != claimed["scope"]


def test_codex_oauth_auto_push_can_start_before_batch_finishes():
    batch = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_ids": [101, 102]},
        progress_total=2,
    )
    push = create_task(
        task_type=TASK_TYPE_ACCOUNT_PUSH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [101],
            "target_key": "nvtokens",
            "payload_format": "codex",
            "source": "codex_oauth",
        },
        progress_total=1,
    )
    second_push = create_task(
        task_type=TASK_TYPE_ACCOUNT_PUSH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [102],
            "target_key": "nvtokens",
            "payload_format": "codex",
            "source": "codex_oauth",
        },
        progress_total=1,
    )

    claimed_batch = claim_next_runnable_task()
    assert claimed_batch["id"] == batch["id"]
    assert "account:101" in claimed_batch["account_keys"]

    claimed_push = claim_next_runnable_task(
        running_scope_counts={claimed_batch["scope"]: 1},
        busy_account_keys=set(claimed_batch["account_keys"]),
        max_parallel_per_scope=1,
    )

    assert claimed_push["id"] == push["id"]
    assert claimed_push["account_keys"] == []
    assert claimed_push["scope"] == "chatgpt:account_push:nvtokens:101"

    second_claimed_push = claim_next_runnable_task(
        running_scope_counts={
            claimed_batch["scope"]: 1,
            claimed_push["scope"]: 1,
        },
        busy_account_keys=set(claimed_batch["account_keys"]),
        max_parallel_per_scope=1,
    )
    assert second_claimed_push["id"] == second_push["id"]
    assert second_claimed_push["scope"] == "chatgpt:account_push:nvtokens:102"


def test_kakao_linked_push_holds_the_account_key():
    push = create_task(
        task_type=TASK_TYPE_ACCOUNT_PUSH,
        platform="chatgpt",
        payload={
            "platform": "chatgpt",
            "account_ids": [101],
            "target_key": "nvtokens",
            "payload_format": "codex",
            "source": "kakao_pipeline",
        },
        progress_total=1,
    )
    oauth = create_task(
        task_type=TASK_TYPE_CODEX_OAUTH_BATCH,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_ids": [101]},
        progress_total=1,
    )

    claimed_push = claim_next_runnable_task()
    assert claimed_push["id"] == push["id"]
    assert claimed_push["account_keys"] == ["account:101"]

    blocked = claim_next_runnable_task(
        running_scope_counts={claimed_push["scope"]: 1},
        busy_account_keys=set(claimed_push["account_keys"]),
        max_parallel_per_scope=1,
    )
    assert blocked is None
    assert get_task(oauth["id"])["status"] == "pending"


def test_runtime_hard_caps_each_live_task_type_at_ten_without_a_global_cap(monkeypatch):
    monkeypatch.setenv("TASK_MAX_PARALLEL_PER_TYPE", "99")
    assert TaskRuntime().max_parallel_per_type == 10
    assert TaskRuntime().max_parallel_per_scope == 10
    assert TaskRuntime(max_parallel_per_type=99).max_parallel_per_type == 10
    assert TaskRuntime(max_parallel_per_type=4).max_parallel_per_type == 4
    assert TaskRuntime(max_parallel_tasks=3).max_parallel_per_type == 3
    assert TaskRuntime(max_parallel_tasks=3).max_parallel_tasks == 3
    monkeypatch.delenv("TASK_MAX_PARALLEL_PER_TYPE")
    monkeypatch.setenv("TASK_MAX_PARALLEL", "2")
    assert TaskRuntime().max_parallel_per_type == 2

    release = threading.Event()
    runtime = TaskRuntime(max_parallel_per_type=99, max_parallel_per_scope=10)
    workers: list[threading.Thread] = []
    try:
        for index in range(20):
            worker = threading.Thread(target=release.wait)
            worker.start()
            workers.append(worker)
            runtime._workers[f"worker-{index}"] = TaskWorkerState(
                thread=worker,
                platform="chatgpt",
                task_type=(
                    TASK_TYPE_CODEX_OAUTH_BATCH
                    if index < 10
                    else TASK_TYPE_REGISTER
                ),
                scope=f"scope-{index}",
            )

        running_type_counts, running_scope_counts, _busy_account_keys = runtime._accounting_snapshot()

        assert running_type_counts == {
            TASK_TYPE_CODEX_OAUTH_BATCH: 10,
            TASK_TYPE_REGISTER: 10,
        }
        assert len(running_scope_counts) == 20
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=2)


def test_cancelled_batch_parent_waits_for_started_children_before_releasing_slot():
    child_started = threading.Event()
    release_child = threading.Event()
    parent_finished = threading.Event()
    pool = tasks_module.ThreadPoolExecutor(max_workers=1)

    def _child_work():
        child_started.set()
        release_child.wait()

    pool.submit(_child_work)
    assert child_started.wait(timeout=2)

    parent = threading.Thread(
        target=lambda: (
            tasks_module._shutdown_task_pool(pool, cancel_futures=True),
            parent_finished.set(),
        )
    )
    parent.start()
    try:
        assert parent_finished.wait(timeout=0.05) is False
    finally:
        release_child.set()
        parent.join(timeout=2)

    assert parent_finished.is_set()


def test_cancel_requested_worker_keeps_slot_until_its_thread_exits():
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    claimed = claim_next_runnable_task()
    request_cancel(task["id"])

    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    worker.start()
    runtime = TaskRuntime(max_parallel_per_type=1, max_parallel_per_scope=1)
    runtime._workers[task["id"]] = TaskWorkerState(
        thread=worker,
        platform="chatgpt",
        task_type=TASK_TYPE_REGISTER,
        scope=claimed["scope"],
    )

    try:
        running_type_counts, running_scope_counts, busy_account_keys = runtime._accounting_snapshot()

        assert running_type_counts == {TASK_TYPE_REGISTER: 1}
        assert running_scope_counts == {claimed["scope"]: 1}
        assert busy_account_keys == set()
    finally:
        release.set()
        worker.join(timeout=2)

    runtime._reap_workers()
    running_type_counts, running_scope_counts, busy_account_keys = runtime._accounting_snapshot()

    assert running_type_counts == {}
    assert running_scope_counts == {}
    assert busy_account_keys == set()


def test_interrupted_task_is_seen_as_stop_requested():
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    _mutate_task(task["id"], lambda model: setattr(model, "status", TASK_STATUS_INTERRUPTED))

    assert TaskLogger(task["id"]).is_cancel_requested() is True


def test_terminal_task_is_not_overwritten_by_late_worker_finish():
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    _mutate_task(task["id"], lambda model: setattr(model, "status", TASK_STATUS_INTERRUPTED))

    TaskLogger(task["id"]).finish("succeeded")

    assert get_task(task["id"])["status"] == TASK_STATUS_INTERRUPTED


def test_finish_atomically_turns_interleaved_cancel_request_into_cancelled(monkeypatch):
    task = create_task(
        task_type=TASK_TYPE_ACCOUNT_PUSH,
        platform="chatgpt",
        payload={"account_ids": [1], "target_key": "nvtokens"},
        progress_total=1,
    )
    logger = TaskLogger(task["id"])
    assert logger.mark_running() is True

    real_mutate_task = tasks_module._mutate_task
    cancel_injected = False

    def _mutate_after_cancel_request(task_id, mutation):
        nonlocal cancel_injected
        if task_id == task["id"] and not cancel_injected:
            cancel_injected = True
            real_mutate_task(
                task_id,
                lambda model: setattr(model, "status", TASK_STATUS_CANCEL_REQUESTED),
            )
        return real_mutate_task(task_id, mutation)

    monkeypatch.setattr(tasks_module, "_mutate_task", _mutate_after_cancel_request)

    logger.finish(tasks_module.TASK_STATUS_SUCCEEDED)

    saved = get_task(task["id"])
    assert saved["status"] == TASK_STATUS_CANCELLED
    assert saved["error"] == "任务已取消"
