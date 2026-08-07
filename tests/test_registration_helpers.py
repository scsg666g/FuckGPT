from types import SimpleNamespace

from core.registration.helpers import build_otp_callback
from core.registration.models import RegistrationContext


def test_otp_callback_refreshes_mail_baseline_before_resend():
    class Mailbox:
        def __init__(self):
            self.current_ids_calls = 0
            self.wait_calls = []

        def get_current_ids(self, account):
            self.current_ids_calls += 1
            return {f"message-{self.current_ids_calls}"}

        def wait_for_code(self, account, **kwargs):
            self.wait_calls.append(kwargs)
            return "654321"

    mailbox = Mailbox()
    context = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=mailbox),
        identity=SimpleNamespace(mailbox_account=object(), before_ids={"message-0"}),
        config=SimpleNamespace(),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    callback = build_otp_callback(context, keyword="verification")

    assert callback is not None
    assert callback() == "654321"
    assert mailbox.wait_calls[-1]["before_ids"] == {"message-0"}

    callback.refresh_before_ids()

    assert callback() == "654321"
    assert mailbox.wait_calls[-1]["before_ids"] == {"message-1"}

