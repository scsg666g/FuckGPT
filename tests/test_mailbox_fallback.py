from core.base_mailbox import BaseMailbox, FallbackMailbox, MailboxAccount


class _StubMailbox(BaseMailbox):
    def __init__(self, marker: str):
        self.marker = marker

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=f"{self.marker}@example.com")

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        return self.marker

    def get_current_ids(self, account: MailboxAccount) -> set:
        return {self.marker}


def test_fallback_mailbox_resolves_rebuilt_account_from_nested_provider_account():
    api_mailbox = _StubMailbox("api")
    fallback = FallbackMailbox(
        [
            ("hotmail007", _StubMailbox("hotmail")),
            ("api_mailbox", api_mailbox),
        ]
    )
    rebuilt = MailboxAccount(
        email="rebuilt@example.com",
        extra={"provider_account": {"provider_name": "api_mailbox"}},
    )

    assert fallback.get_current_ids(rebuilt) == {"api"}


def test_fallback_mailbox_prefers_explicit_provider_key_over_nested_context():
    fallback = FallbackMailbox(
        [
            ("hotmail007", _StubMailbox("hotmail")),
            ("api_mailbox", _StubMailbox("api")),
        ]
    )
    account = MailboxAccount(
        email="explicit@example.com",
        extra={
            "mailbox_provider_key": "hotmail007",
            "provider_account": {"provider_name": "api_mailbox"},
        },
    )

    assert fallback.get_current_ids(account) == {"hotmail"}
