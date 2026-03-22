import sys

from dxd_rating.platform.runtime import match_runtime as _impl
from dxd_rating.platform.runtime.match_runtime import *  # noqa: F401,F403

sys.modules[__name__] = _impl
