import sys

from dxd_rating.platform.db import schema as _impl
from dxd_rating.platform.db.schema import *  # noqa: F401,F403

sys.modules[__name__] = _impl
