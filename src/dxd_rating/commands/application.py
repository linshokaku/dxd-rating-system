import sys

from dxd_rating.platform.discord.gateway.commands import application as _impl
from dxd_rating.platform.discord.gateway.commands.application import *  # noqa: F401,F403

sys.modules[__name__] = _impl
