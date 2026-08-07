from __future__ import annotations

import time

from application.workflow_registry import register_step_adapter
from application.workflows import run_due_workflow_once
from domain.workflows import StepAdapter, StepTransition


class _ApiImmediateAdapter(StepAdapter):
    key = "test.api.immediate"

    def start(self, *, inputs, idempotency_key, attempt):
        return StepTransition.succeeded({"inputs": inputs, "attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


class _ApiNeedsInputAdapter(StepAdapter):
    key = "test.api.needs_input"

    def start(self, *, inputs, idempotency_key, attempt):
        if not inputs.get("target"):
            return StepTransition.needs_attention("缺少 target", code="target_missing")
        return StepTransition.succeeded({"target": inputs["target"], "attempt": attempt})

    def resume(self, *, inputs, external_ref, attempt):
        return self.start(inputs=inputs, idempotency_key=external_ref, attempt=attempt)


def _wait_run_status(client, run_id: str, expected: str, *, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        run_due_workflow_once()
        response = client.get(f"/api/workflows/runs/{run_id}")
        last = response.json()
        if last.get("status") == expected:
            return last
        time.sleep(0.05)
    return last


def test_workflow_definition_and_run_api(client):
    register_step_adapter(_ApiImmediateAdapter())
    response = client.post(
        "/api/workflows/definitions",
        json={
            "definition": {
                "key": "test_api_run",
                "version": 1,
                "name": "API run",
                "steps": [
                    {
                        "id": "first",
                        "uses": "test.api.immediate",
                        "input": {"value": {"$path": "workflow.inputs.value"}},
                    }
                ],
            }
        },
    )
    assert response.status_code == 200

    listing = client.get("/api/workflows/definitions")
    assert listing.status_code == 200
    assert any(item["key"] == "test_api_run" for item in listing.json()["items"])

    created = client.post(
        "/api/workflows/runs",
        json={"definition_key": "test_api_run", "input": {"value": "ok"}},
    )
    assert created.status_code == 200
    run_id = created.json()["id"]

    detail = _wait_run_status(client, run_id, "succeeded")
    assert detail["status"] == "succeeded"
    assert detail["steps"][0]["output"]["inputs"]["value"] == "ok"

    events = client.get(f"/api/workflows/runs/{run_id}/events")
    assert events.status_code == 200
    assert events.json()["items"]


def test_workflow_step_input_patch_and_retry_api(client):
    register_step_adapter(_ApiNeedsInputAdapter())
    client.post(
        "/api/workflows/definitions",
        json={
            "definition": {
                "key": "test_api_attention",
                "version": 1,
                "name": "API attention",
                "steps": [{"id": "needs", "uses": "test.api.needs_input", "input": {}}],
            }
        },
    )
    created = client.post(
        "/api/workflows/runs",
        json={"definition_key": "test_api_attention", "input": {}},
    )
    run_id = created.json()["id"]
    assert _wait_run_status(client, run_id, "needs_attention")["status"] == "needs_attention"

    patched = client.patch(
        f"/api/workflows/runs/{run_id}/steps/needs/input",
        json={"input": {"target": "nvtokens"}},
    )
    assert patched.status_code == 200
    retried = client.post(f"/api/workflows/runs/{run_id}/steps/needs/retry")
    assert retried.status_code == 200

    detail = _wait_run_status(client, run_id, "succeeded")
    assert detail["status"] == "succeeded"
    assert detail["steps"][0]["output"]["target"] == "nvtokens"


def test_workflow_batch_and_summary_api(client):
    register_step_adapter(_ApiImmediateAdapter())
    response = client.post(
        "/api/workflows/definitions",
        json={
            "definition": {
                "key": "test_api_batch",
                "version": 1,
                "name": "API batch",
                "steps": [
                    {
                        "id": "first",
                        "uses": "test.api.immediate",
                        "input": {"value": {"$path": "workflow.inputs.value"}},
                    }
                ],
            }
        },
    )
    assert response.status_code == 200

    created = client.post(
        "/api/workflows/runs/batch",
        json={
            "definition_key": "test_api_batch",
            "concurrency": 2,
            "items": [
                {"input": {"value": "a"}, "metadata": {"row": 1}},
                {"input": {"value": "b"}, "metadata": {"row": 2}},
            ],
        },
    )
    assert created.status_code == 200
    batch = created.json()
    assert batch["total"] == 2
    assert len(batch["runs"]) == 2
    assert batch["runs"][0]["batch_id"] == batch["id"]

    listing = client.get(f"/api/workflows/runs?batch_id={batch['id']}")
    assert listing.status_code == 200
    assert listing.json()["total"] == 2

    batches = client.get("/api/workflows/batches")
    assert batches.status_code == 200
    assert any(item["id"] == batch["id"] for item in batches.json()["items"])

    run_id = batch["runs"][0]["id"]
    run_summary = client.get(f"/api/workflows/runs/{run_id}/summary")
    assert run_summary.status_code == 200
    assert run_summary.json()["batch_id"] == batch["id"]

    for item in batch["runs"]:
        assert _wait_run_status(client, item["id"], "succeeded")["status"] == "succeeded"
    batch_summary = client.get(f"/api/workflows/batches/{batch['id']}/summary")
    assert batch_summary.status_code == 200
    assert batch_summary.json()["summary"]["succeeded"] == 2


def test_workflow_template_editor_and_batch_control_api(client):
    register_step_adapter(_ApiNeedsInputAdapter())

    adapters = client.get("/api/workflows/adapters")
    assert adapters.status_code == 200
    assert any(item["key"] == "test.api.needs_input" for item in adapters.json()["items"])

    saved = client.post(
        "/api/workflows/definitions",
        json={
            "definition": {
                "key": "test_api_editable",
                "version": 1,
                "name": "API editable",
                "description": "editable template",
                "sample_input": {"target": ""},
                "ui_schema": {
                    "sections": [
                        {
                            "title": "Input",
                            "fields": [{"path": "target", "label": "Target", "type": "text"}],
                        }
                    ]
                },
                "steps": [{"id": "needs", "uses": "test.api.needs_input", "input": {}}],
            }
        },
    )
    assert saved.status_code == 200
    assert saved.json()["definition"]["sample_input"]["target"] == ""

    invalid = client.post(
        "/api/workflows/definitions",
        json={
            "definition": {
                "key": "test_api_invalid_policy",
                "version": 1,
                "steps": [{"id": "needs", "uses": "test.api.needs_input", "on_failure": "continue_anyway"}],
            }
        },
    )
    assert invalid.status_code == 400

    created = client.post(
        "/api/workflows/runs/batch",
        json={
            "definition_key": "test_api_editable",
            "concurrency": 2,
            "items": [{"input": {}}, {"input": {}}],
        },
    )
    assert created.status_code == 200
    batch = created.json()

    for item in batch["runs"]:
        assert _wait_run_status(client, item["id"], "needs_attention")["status"] == "needs_attention"

    paused = client.post(f"/api/workflows/batches/{batch['id']}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    resumed = client.post(f"/api/workflows/batches/{batch['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "needs_attention"

    retried = client.post(f"/api/workflows/batches/{batch['id']}/retry-failed")
    assert retried.status_code == 200
    assert retried.json()["retried"] == 2

    for item in batch["runs"]:
        assert _wait_run_status(client, item["id"], "needs_attention")["status"] == "needs_attention"

    cancelled = client.post(f"/api/workflows/batches/{batch['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["summary"]["cancelled"] == 2


def test_workflow_input_presets_persist_defaults_last_used_and_merge_new_template_fields(client):
    definition_v1 = {
        "key": "test_api_presets",
        "version": 1,
        "name": "Preset workflow",
        "sample_input": {"registration": {"count": 1, "concurrency": 1}, "target": "default"},
        "steps": [{"id": "needs", "uses": "test.api.needs_input", "input": {}}],
    }
    register_step_adapter(_ApiNeedsInputAdapter())
    assert client.post("/api/workflows/definitions", json={"definition": definition_v1}).status_code == 200

    created = client.post(
        "/api/workflows/definitions/test_api_presets/presets",
        json={
            "name": "代理服务",
            "definition_version": 1,
            "input": {"registration": {"concurrency": 3}},
            "launch_mode": "batch",
            "batch_concurrency": 4,
            "batch_count": 12,
            "is_default": True,
        },
    )
    assert created.status_code == 200
    preset = created.json()
    assert preset["is_default"] is True

    last_used = client.put(
        "/api/workflows/definitions/test_api_presets/presets/last-used",
        json={
            "definition_version": 1,
            "input": {"target": "last"},
            "launch_mode": "single",
            "batch_concurrency": 2,
            "batch_count": 6,
        },
    )
    assert last_used.status_code == 200
    assert last_used.json()["is_last_used"] is True

    definition_v2 = {
        **definition_v1,
        "version": 2,
        "sample_input": {
            "registration": {"count": 2, "concurrency": 1, "executor_type": "headless"},
            "target": "v2-default",
        },
    }
    assert client.post("/api/workflows/definitions", json={"definition": definition_v2}).status_code == 200

    listing = client.get("/api/workflows/definitions/test_api_presets/presets?version=2")
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["default_id"] == preset["id"]
    assert payload["items"][0]["version_mismatch"] is True
    assert payload["items"][0]["input"]["registration"] == {
        "count": 2,
        "concurrency": 3,
        "executor_type": "headless",
    }
    assert payload["last_used"]["input"]["target"] == "last"

    updated = client.put(
        f"/api/workflows/definitions/test_api_presets/presets/{preset['id']}",
        json={
            "name": "546789",
            "definition_version": 2,
            "input": {"target": "scanner"},
            "launch_mode": "single",
            "is_default": False,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "546789"
    assert updated.json()["version_mismatch"] is False

    deleted = client.delete(f"/api/workflows/definitions/test_api_presets/presets/{preset['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/workflows/definitions/test_api_presets/presets?version=2").json()["items"] == []
