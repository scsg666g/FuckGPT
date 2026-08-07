from __future__ import annotations

from fastapi import APIRouter, HTTPException

from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["tasks"])
service = TasksQueryService()


@router.get("")
def list_tasks(
    limit: int = 50,
    offset: int = 0,
    status: str = "",
    platform: str = "",
    type: str = "",
):
    return service.list_tasks(
        limit=limit,
        offset=offset,
        status=status,
        platform=platform,
        task_type=type,
    )


@router.get("/{task_id}")
def get_task(task_id: str):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/events")
def list_task_events(
    task_id: str,
    since: int = 0,
    before: int = 0,
    limit: int = 200,
    latest: bool = False,
):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return service.list_events(
        task_id,
        since=since,
        before=before,
        limit=limit,
        latest=latest,
    )
