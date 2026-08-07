"""Register remote temporary-mailbox drivers."""

from core.remote_mailboxes import (
    DuckMailMailbox,
    GPTMailMailbox,
    MaliAPIMailbox,
    MoeMailMailbox,
    TempMailLolMailbox,
)
from providers.registry import register_provider


register_provider("mailbox", "tempmail_lol")(TempMailLolMailbox)
register_provider("mailbox", "moemail")(MoeMailMailbox)
register_provider("mailbox", "duckmail")(DuckMailMailbox)
register_provider("mailbox", "gptmail")(GPTMailMailbox)
register_provider("mailbox", "maliapi")(MaliAPIMailbox)
