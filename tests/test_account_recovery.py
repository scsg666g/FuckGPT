from __future__ import annotations

from application.account_recovery import (
    AccountReloginResult,
    AccountStateSnapshot,
    check_and_recover_account,
)


def test_account_recovery_relogins_explicitly_invalid_state_and_rechecks():
    checks = iter(
        [
            AccountStateSnapshot(ok=True, valid=False, data={"plan": "free"}),
            AccountStateSnapshot(ok=True, valid=True, data={"plan": "plus"}),
        ]
    )
    actions: list[str] = []
    logs: list[tuple[str, str]] = []

    outcome = check_and_recover_account(
        check_state=lambda: actions.append("check") or next(checks),
        relogin=lambda: actions.append("relogin") or AccountReloginResult(ok=True),
        relogin_invalid=True,
        log_fn=lambda message, level="info": logs.append((level, message)),
        label="test@example.com: ",
    )

    assert actions == ["check", "relogin", "check"]
    assert outcome.relogin_attempted is True
    assert outcome.relogin_ok is True
    assert outcome.recovery_failed is False
    assert outcome.final.valid is True
    assert any("登录已失效" in message for _level, message in logs)
    assert any("刷新完成: 有效" in message for _level, message in logs)


def test_account_recovery_does_not_relogin_indeterminate_or_failed_check():
    for initial in (
        AccountStateSnapshot(ok=True, valid=None),
        AccountStateSnapshot(ok=False, valid=None, error="network timeout"),
    ):
        relogins: list[bool] = []
        outcome = check_and_recover_account(
            check_state=lambda initial=initial: initial,
            relogin=lambda: relogins.append(True) or AccountReloginResult(ok=True),
            relogin_invalid=True,
        )

        assert relogins == []
        assert outcome.relogin_attempted is False
        assert outcome.final is initial


def test_account_recovery_marks_explicitly_invalid_post_login_state_as_failed():
    initial = AccountStateSnapshot(ok=True, valid=False)
    refreshed = AccountStateSnapshot(ok=True, valid=False)

    outcome = check_and_recover_account(
        check_state=lambda: initial,
        relogin=lambda: AccountReloginResult(ok=True, refreshed=refreshed),
        relogin_invalid=True,
    )

    assert outcome.relogin_attempted is True
    assert outcome.relogin_ok is True
    assert outcome.recovery_failed is True
    assert outcome.final is refreshed
    assert outcome.relogin_error == "自动重新登录后账号仍为失效状态"
