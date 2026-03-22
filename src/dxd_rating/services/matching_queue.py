import sys

from dxd_rating.contexts.matchmaking.application import matching_queue as _impl
from dxd_rating.contexts.matchmaking.application.matching_queue import *  # noqa: F401,F403

sys.modules[__name__] = _impl
