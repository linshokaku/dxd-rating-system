import sys

from dxd_rating.platform.db import session as _impl
from dxd_rating.platform.db.session import *  # noqa: F401,F403

sys.modules[__name__] = _impl
