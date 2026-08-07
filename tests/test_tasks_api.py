from application.tasks import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_TYPE_ACCOUNT_CHECK_ALL,
    TASK_TYPE_REGISTER,
    TaskLogger,
    append_task_event,
    create_task,
)


def test_list_tasks_returns_recent_task_records(client):
    first = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    second = create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform="chatgpt",
        payload={"limit": 1},
        progress_total=1,
    )

    response = client.get("/api/tasks")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["running"] == 0
    assert [item["id"] for item in data["items"]] == [second["id"], first["id"]]
    assert data["items"][0]["task_id"] == second["id"]
    assert data["items"][0]["type"] == TASK_TYPE_ACCOUNT_CHECK_ALL


def test_list_tasks_filters_by_status_and_type(client):
    pending = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    failed = create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform="chatgpt",
        payload={"limit": 1},
        progress_total=1,
    )
    from application.tasks import TaskLogger

    TaskLogger(failed["id"]).finish(TASK_STATUS_FAILED, error="failed")

    response = client.get(
        "/api/tasks",
        params={"status": TASK_STATUS_PENDING, "type": TASK_TYPE_REGISTER},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == pending["id"]


def test_task_events_support_latest_tail_and_older_page(client):
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 1},
        progress_total=1,
    )
    TaskLogger(task["id"]).finish(TASK_STATUS_FAILED, error="test fixture")
    for index in range(5):
        append_task_event(task["id"], f"log {index + 1}")

    latest_response = client.get(
        f"/api/tasks/{task['id']}/events",
        params={"latest": "true", "limit": 2},
    )

    assert latest_response.status_code == 200
    latest = latest_response.json()
    assert [item["message"] for item in latest["items"]] == ["log 4", "log 5"]
    assert latest["has_more_before"] is True

    older_response = client.get(
        f"/api/tasks/{task['id']}/events",
        params={"before": latest["before"], "limit": 2},
    )

    assert older_response.status_code == 200
    older = older_response.json()
    assert [item["message"] for item in older["items"]] == ["log 2", "log 3"]
    assert older["has_more_before"] is True
