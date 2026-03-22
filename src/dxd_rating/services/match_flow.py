import sys

from dxd_rating.contexts.matches.application import match_flow as _impl
from dxd_rating.contexts.matches.application.match_flow import *  # noqa: F401,F403

sys.modules[__name__] = _impl
