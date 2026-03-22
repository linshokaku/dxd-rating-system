import sys

from dxd_rating.platform.runtime import outbox as _impl
from dxd_rating.platform.runtime.outbox import *  # noqa: F401,F403

sys.modules[__name__] = _impl
