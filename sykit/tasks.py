from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Any, TypeVar

from .utils import _metadata

Function = TypeVar("Function", bound=Callable[..., Any])
Enqueuer = Callable[[Callable[..., Any], tuple[Any, ...], dict[str, Any]], str]
_CURRENT_ENQUEUER: ContextVar[Enqueuer | None] = ContextVar(
    "sykit_task_enqueuer",
    default=None,
)


def task(function: Function) -> Function:
    """Mark a top-level function as a persistent background task."""
    if not callable(function):
        raise TypeError("@task must decorate a function.")
    return _metadata(function, "task", True)


def scheduled(expression: str):
    """Run a parameterless task on a five-field UTC cron schedule."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("@scheduled requires a non-empty cron expression.")

    def decorator(function: Function) -> Function:
        function = task(function)
        return _metadata(function, "schedule", expression.strip())

    return decorator


def enqueue(function: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """Persist a call to a decorated task and return its task id."""
    metadata = getattr(function, "__sykit__", {})
    if (
        not callable(function)
        or not isinstance(metadata, dict)
        or not metadata.get("task")
    ):
        raise TypeError(
            "enqueue() expects a function decorated with @task or @scheduled."
        )
    enqueuer = _CURRENT_ENQUEUER.get()
    if enqueuer is None:
        raise RuntimeError(
            "enqueue() can only be used while handling an endpoint or background task."
        )
    return enqueuer(function, args, kwargs)


def _bind_enqueuer(enqueuer: Enqueuer) -> Token[Enqueuer | None]:
    return _CURRENT_ENQUEUER.set(enqueuer)


def _reset_enqueuer(token: Token[Enqueuer | None]) -> None:
    _CURRENT_ENQUEUER.reset(token)


__all__ = ["enqueue", "scheduled", "task"]
