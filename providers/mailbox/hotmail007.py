"""Hotmail007 mailbox provider registration."""
from core.hotmail007_mailbox import Hotmail007Mailbox  # noqa: F401
from providers.registry import register_provider


register_provider("mailbox", "hotmail007")(Hotmail007Mailbox)
