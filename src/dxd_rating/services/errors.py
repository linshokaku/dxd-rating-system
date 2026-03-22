import sys

from dxd_rating.contexts.common.application import errors as _impl
from dxd_rating.contexts.common.application.errors import *  # noqa: F401,F403

sys.modules[__name__] = _impl
