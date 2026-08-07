from __future__ import annotations

from providers.captcha.local_solver import LocalSolverCaptcha


def test_local_solver_is_not_started_during_provider_construction(monkeypatch):
    calls = []
    monkeypatch.setattr("services.solver_manager.start", lambda: calls.append("start"))
    solver = LocalSolverCaptcha()
    assert solver.solver_url == "http://localhost:8889"
    assert calls == []


def test_local_solver_starts_when_a_turnstile_job_is_submitted(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            if calls.count("request") == 1:
                return {"taskId": "task-1"}
            return {"status": "ready", "solution": {"token": "token-1"}}

    monkeypatch.setattr("services.solver_manager.start", lambda: calls.append("start"))
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: (calls.append("request") or Response()))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    assert LocalSolverCaptcha().solve_turnstile("https://example.com", "site-key") == "token-1"
    assert calls[0] == "start"
