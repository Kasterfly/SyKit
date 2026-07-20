"""Registration point for unhandled endpoint error reporting."""

from __future__ import annotations

import inspect
from typing import Any, Callable

ErrorHook = Callable[[Exception, Any], Any]
_ERROR_HOOK: ErrorHook | None = None


def register_error_hook(callback: ErrorHook | None) -> None:
    """Set the endpoint error callback, or clear it with None.

    The callback receives the exception and Starlette request before SyKit
    returns its generic 500 response. It may be synchronous or asynchronous.
    Hook failures are logged and never replace the endpoint response.
    """
    if callback is not None and not callable(callback):
        raise TypeError("The SyKit error hook must be callable or None.")
    global _ERROR_HOOK
    _ERROR_HOOK = callback


async def _notify_error(error: Exception, request: Any) -> None:
    callback = _ERROR_HOOK
    if callback is None:
        return
    result = callback(error, request)
    if inspect.isawaitable(result):
        await result


__all__ = ["ErrorHook", "register_error_hook"]
