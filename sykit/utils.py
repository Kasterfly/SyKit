from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Any, TypeVar

Function = TypeVar("Function", bound=Callable[..., Any])
_INTERNAL_PREFIX = "__sykit_"
_CURRENT_SESSION: ContextVar[dict[str, Any] | None] = ContextVar(
    "sykit_current_session",
    default=None,
)


def _session() -> dict[str, Any]:
    session = _CURRENT_SESSION.get()
    if session is None:
        raise RuntimeError(
            "Session utilities can only be used while handling an endpoint."
        )
    return session


def _bind_session(session: dict[str, Any]) -> Token[dict[str, Any] | None]:
    return _CURRENT_SESSION.set(session)


def _reset_session(token: Token[dict[str, Any] | None]) -> None:
    _CURRENT_SESSION.reset(token)


def get_session() -> dict[str, Any]:
    return {
        key: value
        for key, value in _session().items()
        if not key.startswith(_INTERNAL_PREFIX)
    }


def update_session(key: str, value: Any = "") -> None:
    if not isinstance(key, str) or not key:
        raise ValueError("Session keys must be non-empty strings.")
    if key.startswith(_INTERNAL_PREFIX):
        raise ValueError("Session keys beginning with '__sykit_' are reserved.")
    session = _session()
    if value is None or value == "":
        session.pop(key, None)
    else:
        session[key] = value


def _metadata(function: Function, key: str, value: Any) -> Function:
    metadata = dict(getattr(function, "__sykit__", {}))
    metadata[key] = value
    setattr(function, "__sykit__", metadata)
    return function


def _route(kind: str, endpoint: str):
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("SyKit endpoint paths must be non-empty strings.")

    def decorator(function: Function) -> Function:
        function = _metadata(function, "kind", kind)
        return _metadata(function, "endpoint", endpoint)

    return decorator


def expose(endpoint: str, *, max_upload_bytes: int | None = None):
    if max_upload_bytes is not None and (
        isinstance(max_upload_bytes, bool)
        or not isinstance(max_upload_bytes, int)
        or max_upload_bytes < 1
    ):
        raise ValueError("max_upload_bytes must be a positive integer.")
    route = _route("expose", endpoint)

    def decorator(function: Function) -> Function:
        function = route(function)
        if max_upload_bytes is not None:
            function = _metadata(function, "max_upload_bytes", max_upload_bytes)
        return function

    return decorator


def raw(endpoint: str):
    return _route("raw", endpoint)


def sse(endpoint: str):
    return _route("sse", endpoint)


def web_hook(endpoint: str):
    return _route("web_hook", endpoint)


def perms(permissions: dict[str, Any]):
    if not isinstance(permissions, dict):
        raise TypeError("SyKit permissions must be a dictionary.")

    def decorator(function: Function) -> Function:
        return _metadata(function, "permissions", permissions)

    return decorator


def requires(permissions: dict[str, Any]):
    return perms(permissions)


def hidden(function: Function) -> Function:
    return _metadata(function, "hidden", True)


def api_key(scopes: list[str] | Callable[..., Any] | None = None):
    """Require an API key on a @web_hook endpoint.

    Use bare (@api_key) to accept any active key, or pass a list of
    scope names the key must have (@api_key(["orders:write"])).
    """
    if callable(scopes):
        return _metadata(scopes, "api_key", {"scopes": []})
    if scopes is None:
        scopes = []
    if not isinstance(scopes, (list, tuple)) or not all(
        isinstance(scope, str) and scope for scope in scopes
    ):
        raise TypeError("SyKit API key scopes must be a list of strings.")

    def decorator(function: Function) -> Function:
        return _metadata(function, "api_key", {"scopes": list(scopes)})

    return decorator


def cors(origins: list[str] | tuple[str, ...]):
    if not isinstance(origins, (list, tuple)) or not all(
        isinstance(origin, str) for origin in origins
    ):
        raise TypeError("SyKit CORS origins must be a list of strings.")

    def decorator(function: Function) -> Function:
        return _metadata(function, "cors", list(origins))

    return decorator


def limits(options: dict[str, int | str]):
    if not isinstance(options, dict):
        raise TypeError("SyKit limits must be a dictionary.")

    def decorator(function: Function) -> Function:
        return _metadata(function, "limits", options)

    return decorator


__all__ = [
    "api_key",
    "cors",
    "expose",
    "get_session",
    "hidden",
    "limits",
    "perms",
    "raw",
    "requires",
    "sse",
    "update_session",
    "web_hook",
]
