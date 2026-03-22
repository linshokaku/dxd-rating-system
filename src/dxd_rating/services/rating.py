import sys

from dxd_rating.contexts.matches.domain import rating as _impl
from dxd_rating.contexts.matches.domain.rating import *  # noqa: F401,F403

sys.modules[__name__] = _impl
