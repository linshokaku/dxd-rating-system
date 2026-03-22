import sys

from dxd_rating.contexts.restrictions.application import access_restrictions as _impl
from dxd_rating.contexts.restrictions.application.access_restrictions import *  # noqa: F401,F403

sys.modules[__name__] = _impl
