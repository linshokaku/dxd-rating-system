from bot.services.errors import PlayerAlreadyRegisteredError, RegistrationError
from bot.services.registration import register_player

__all__ = [
    "PlayerAlreadyRegisteredError",
    "RegistrationError",
    "register_player",
]
