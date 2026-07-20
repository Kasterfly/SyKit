import sys as _sys

from . import utils as util
from .errors import register_error_hook
from .tasks import enqueue, scheduled, task
from .uploads import Upload
from .utils import (
    api_key,
    cors,
    expose,
    get_session,
    hidden,
    limits,
    perms,
    raw,
    requires,
    sse,
    update_session,
    web_hook,
)

# Keep this literal in sync with README, CHANGELOG.md, and the release tag.
__version__ = "0.13.0"


# Keep ``import sykit.util`` working while maintaining one utility module.
_sys.modules[f"{__name__}.util"] = util

__all__ = [
    "__version__",
    "Upload",
    "api_key",
    "cors",
    "enqueue",
    "expose",
    "get_session",
    "hidden",
    "limits",
    "perms",
    "raw",
    "register_error_hook",
    "requires",
    "scheduled",
    "sse",
    "task",
    "update_session",
    "util",
    "web_hook",
]
