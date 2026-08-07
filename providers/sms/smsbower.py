"""SMSBower SMS provider registration."""
from core.smsbower_sms import SMSBowerClient  # noqa: F401
from providers.registry import register_provider


register_provider("sms", "smsbower")(SMSBowerClient)
