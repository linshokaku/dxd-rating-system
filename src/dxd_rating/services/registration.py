import sys

from dxd_rating.contexts.players.application import registration as _impl
from dxd_rating.contexts.players.application.registration import *  # noqa: F401,F403

sys.modules[__name__] = _impl
