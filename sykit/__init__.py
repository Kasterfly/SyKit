import sys as _sys

from . import utils as util
from .utils import (
    cors,
    expose,
    get_session,
    hidden,
    limits,
    perms,
    raw,
    requires,
    update_session,
    web_hook,
)

__version__ = "0.3.0"


# Keep ``import sykit.util`` working while maintaining one utility module.
_sys.modules[f"{__name__}.util"] = util

__all__ = [
    "__version__",
    "cors",
    "expose",
    "get_session",
    "hidden",
    "limits",
    "perms",
    "raw",
    "requires",
    "update_session",
    "util",
    "web_hook",
]
