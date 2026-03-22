import sys

from dxd_rating.platform.runtime import application as _impl
from dxd_rating.platform.runtime.application import *  # noqa: F401,F403

sys.modules[__name__] = _impl
