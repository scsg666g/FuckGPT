"""HeroSMS provider registration."""
from core.herosms_sms import HeroSMSClient
from providers.registry import register_provider


register_provider("sms", "herosms")(HeroSMSClient)
