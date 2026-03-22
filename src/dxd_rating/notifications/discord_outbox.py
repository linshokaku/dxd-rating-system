import sys

from dxd_rating.platform.discord.rest import discord_outbox as _impl
from dxd_rating.platform.discord.rest.discord_outbox import *  # noqa: F401,F403

sys.modules[__name__] = _impl
