"""SMSPool provider registration."""
from core.smspool_sms import SMSPoolClient
from providers.registry import register_provider


register_provider("sms", "smspool")(SMSPoolClient)
