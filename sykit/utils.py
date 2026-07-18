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


def expose(endpoint: str):
    return _route("expose", endpoint)


def raw(endpoint: str):
    return _route("raw", endpoint)


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
    "cors",
    "expose",
    "get_session",
    "hidden",
    "limits",
    "perms",
    "raw",
    "requires",
    "update_session",
    "web_hook",
]
