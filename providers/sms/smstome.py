"""SMSToMe provider registration."""
from core.smstome_sms import SMSToMeClient
from providers.registry import register_provider


register_provider("sms", "smstome")(SMSToMeClient)
