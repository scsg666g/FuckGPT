"""5sim provider registration."""
from core.fivesim_sms import FiveSimClient
from providers.registry import register_provider


register_provider("sms", "fivesim")(FiveSimClient)
