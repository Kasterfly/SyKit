from __future__ import annotations

import inspect
import ipaddress
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
STATIC_DIR = ROOT / "static"
STATIC_ROOT = STATIC_DIR.resolve()
CONFIG_PATH = ROOT / "config.json"
SESSION_COOKIE = "sykit_session"
LOGGER = logging.getLogger("sykit.server")

if str(APP_DIR) not in sys.path:
    sys.path.insert(1, str(APP_DIR))

from core._endpoints import ENDPOINTS  # noqa: E402
from core._limits import (  # noqa: E402
    RateLimiter,
    RateLimitExceeded,
    RateLimitUnavailable,
)

from sykit import util as session_util  # noqa: E402


class EndpointInputError(ValueError):
    pass


class RequestBodyTooLarge(RuntimeError):
    pass


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant {value!r}.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        value[key] = item
    return value


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_object,
    )


def _load_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            value = json.load(
                file,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"Could not load {CONFIG_PATH}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{CONFIG_PATH} must contain a JSON object.")
    return value


CONFIG = _load_config()
LIMITER = RateLimiter(ROOT / ".sykit-limits.sqlite3")


def _positive_integer_setting(name: str, default: int) -> int:
    value = CONFIG.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f'The "{name}" setting must be a positive integer.')
    return value


MAX_REQUEST_BYTES = _positive_integer_setting("max-request-bytes", 1_048_576)


def _api_prefix() -> str:
    configured = CONFIG.get("endpoints", "/api/")
    if not isinstance(configured, str):
        raise RuntimeError('The "endpoints" setting must be a string.')
    value = "/" + configured.strip().strip("/")
    if value == "/":
        raise RuntimeError('The "endpoints" prefix cannot be the site root.')
    segments = value.strip("/").split("/")
    if (
        any(character in value for character in "?#{}\\")
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise RuntimeError(f"Invalid endpoint prefix {configured!r}.")
    return value + "/"


API_PREFIX = _api_prefix()
HIDDEN_MANIFEST_ENDPOINT = "__sykit_manifest__"
API_CATCHALL_METHODS = [
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "DELETE",
    "PATCH",
    # OPTIONS and TRACE must reach the same 404 as unknown API paths, or
    # their 405 "Allow" header reveals which hidden endpoints exist.
    "OPTIONS",
    "TRACE",
]


def _error(
    status_code: int,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {"error": message},
        status_code=status_code,
        headers=headers,
    )


async def _json_body(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise RequestBodyTooLarge
    if not body:
        return {}
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        raise EndpointInputError("Expected an application/json request body.")
    try:
        value = _strict_json_loads(body)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise EndpointInputError("Request body is not valid JSON.") from error
    if not isinstance(value, dict):
        raise EndpointInputError(
            "Request JSON must be an object keyed by parameter name."
        )
    return value


def _query_values(request: Request) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, raw_value in request.query_params.items():
        try:
            values[name] = _strict_json_loads(raw_value)
        except (json.JSONDecodeError, ValueError, RecursionError):
            values[name] = raw_value
    return values


async def _provided_values(
    request: Request, metadata: dict[str, Any]
) -> dict[str, Any]:
    kind = metadata["kind"]
    if kind == "raw":
        return _query_values(request)
    normal_parameters = [
        item for item in metadata["parameters"] if not item["injected"]
    ]
    if kind == "web_hook" and not normal_parameters:
        return {}
    return await _json_body(request)


def _not_found() -> JSONResponse:
    return _error(404, "Endpoint not found.")


def _check_permissions(request: Request, metadata: dict[str, Any]) -> Response | None:
    permissions = metadata.get("permissions") or {}
    required_session = permissions.get("Session") or {}
    if not required_session:
        return None
    # A hidden endpoint answers exactly like a nonexistent one, so a failed
    # permission check must not reveal that the route exists.
    hidden = bool(metadata.get("hidden"))
    if SESSION_COOKIE not in request.cookies or not request.session:
        return _not_found() if hidden else _error(401, "A valid session is required.")
    session = request.session
    for key, expected in required_session.items():
        if key not in session or session[key] != expected:
            return _not_found() if hidden else _error(403, "Session permission denied.")
    return None


def _session_permits(request: Request, metadata: dict[str, Any]) -> bool:
    permissions = metadata.get("permissions") or {}
    required_session = permissions.get("Session") or {}
    if not required_session:
        return True
    if SESSION_COOKIE not in request.cookies or not request.session:
        return False
    session = request.session
    return all(
        key in session and session[key] == expected
        for key, expected in required_session.items()
    )


def _call_values(
    request: Request,
    metadata: dict[str, Any],
    provided: dict[str, Any],
) -> dict[str, Any]:
    accepted = {
        parameter["name"]
        for parameter in metadata["parameters"]
        if not parameter["injected"]
    }
    unexpected = sorted(set(provided) - accepted)
    if unexpected:
        raise EndpointInputError(
            "Unexpected parameter(s): " + ", ".join(unexpected) + "."
        )

    values = dict(provided)
    for parameter in metadata["parameters"]:
        if parameter["name"] == "session":
            values["session"] = request.session
        elif parameter["name"] == "request":
            values["request"] = request
    return values


async def _dispatch(request: Request, record: dict[str, Any]) -> Response:
    metadata = record["metadata"]
    permission_error = _check_permissions(request, metadata)
    if permission_error is not None:
        return permission_error
    try:
        client = request.client.host if request.client else ""
        await LIMITER.check(
            f"{metadata['method']}:{metadata['endpoint']}",
            metadata.get("limits"),
            request.session,
            client,
        )
    except RateLimitExceeded as error:
        return _error(
            429,
            "Rate limit exceeded.",
            {"Retry-After": str(error.retry_after)},
        )
    except RateLimitUnavailable:
        LOGGER.exception("The shared rate-limit store is unavailable.")
        return _error(503, "Rate limiting is temporarily unavailable.")
    try:
        provided = await _provided_values(request, metadata)
        values = _call_values(request, metadata, provided)
        function = record["function"]
        try:
            bound = record["signature"].bind(**values)
        except TypeError as error:
            raise EndpointInputError(str(error)) from error
        bound.apply_defaults()

        session_token = session_util._bind_session(request.session)
        try:
            if record["is_async"]:
                result = await function(*bound.args, **bound.kwargs)
            else:
                result = await run_in_threadpool(
                    function,
                    *bound.args,
                    **bound.kwargs,
                )
        finally:
            session_util._reset_session(session_token)
        if isinstance(result, Response):
            return result
        return JSONResponse(result)
    except RequestBodyTooLarge:
        return _error(
            413,
            f"Request body exceeds the {MAX_REQUEST_BYTES}-byte limit.",
        )
    except EndpointInputError as error:
        return _error(400, str(error))
    except HTTPException as error:
        return _error(
            error.status_code,
            str(error.detail),
            dict(error.headers or {}),
        )
    except Exception:
        LOGGER.exception(
            "Unhandled exception in %s.%s",
            metadata.get("module"),
            metadata.get("name"),
        )
        return _error(500, "The endpoint failed.")


def _handler(record: dict[str, Any]):
    async def endpoint(request: Request) -> Response:
        return await _dispatch(request, record)

    endpoint.__name__ = f"sykit_{record['metadata']['name']}"
    return endpoint


async def _hidden_manifest(request: Request) -> Response:
    visible: dict[str, Any] = {}
    for record in ENDPOINTS:
        metadata = record["metadata"]
        token = metadata.get("token")
        if not metadata.get("hidden") or not token:
            continue
        if not _session_permits(request, metadata):
            continue
        visible[token] = {
            "e": metadata["endpoint"],
            "m": metadata["method"],
            "p": [
                parameter["name"]
                for parameter in metadata["parameters"]
                if not parameter["injected"]
            ],
        }
    return JSONResponse(visible)


async def _api_not_found(request: Request) -> Response:
    return _not_found()


async def _spa(request: Request) -> Response:
    requested = request.path_params.get("path", "")
    requested_path = "/" + requested
    api_root = API_PREFIX.rstrip("/") or "/"
    if (
        API_PREFIX == "/"
        or requested_path == api_root
        or requested_path.startswith(API_PREFIX)
    ):
        return _error(404, "Endpoint not found.")

    candidate = (STATIC_DIR / requested).resolve()
    if candidate != STATIC_ROOT and STATIC_ROOT not in candidate.parents:
        return _error(404, "File not found.")
    if candidate.is_file():
        cache_control = (
            "public, max-age=31536000, immutable"
            if requested.startswith("assets/")
            else "no-cache"
        )
        return FileResponse(candidate, headers={"Cache-Control": cache_control})
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return _error(404, "Frontend build not found.")


def _routes() -> list[Route]:
    routes: list[Route] = []
    for record in ENDPOINTS:
        metadata = record["metadata"]
        record["signature"] = inspect.signature(record["function"])
        record["is_async"] = inspect.iscoroutinefunction(record["function"])
        path = f"{API_PREFIX}{metadata['endpoint']}"
        routes.append(Route(path, _handler(record), methods=[metadata["method"]]))
    # Unknown API paths answer 404 for every method so a hidden endpoint's
    # denial is indistinguishable from a route that does not exist.
    routes.extend(
        [
            Route(
                f"{API_PREFIX}{HIDDEN_MANIFEST_ENDPOINT}",
                _hidden_manifest,
                methods=["POST"],
            ),
            Route(
                API_PREFIX.rstrip("/"),
                _api_not_found,
                methods=API_CATCHALL_METHODS,
            ),
            Route(
                f"{API_PREFIX}{{path:path}}",
                _api_not_found,
                methods=API_CATCHALL_METHODS,
            ),
            Route("/", _spa, methods=["GET", "HEAD"]),
            Route("/{path:path}", _spa, methods=["GET", "HEAD"]),
        ]
    )
    return routes


def _canonical_hostname(value: str) -> str | None:
    hostname = value.rstrip(".")
    if not hostname or any(character.isspace() for character in hostname):
        return None
    try:
        return ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        try:
            ascii_name = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        if len(ascii_name) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in ascii_name.split(".")
        ):
            return None
        return ascii_name


def _host_parts(value: str) -> tuple[str, int | None] | None:
    if "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        if (
            parsed.hostname is None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            return None
        port = parsed.port
    except ValueError:
        return None
    hostname = _canonical_hostname(parsed.hostname)
    return None if hostname is None else (hostname, port)


def _origin_parts(value: str) -> tuple[str, str, int] | None:
    if "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if (
            scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            return None
        hostname = _canonical_hostname(parsed.hostname)
        if hostname is None:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, hostname, port
    except ValueError:
        return None


def _canonical_origin(value: str) -> str | None:
    parts = _origin_parts(value)
    if parts is None:
        return None
    scheme, hostname, port = parts
    displayed_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    port_suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{displayed_host}{port_suffix}"


def _same_origin(scope: dict[str, Any], origin: str) -> bool:
    origin_parts = _origin_parts(origin)
    if origin_parts is None:
        return False
    headers = dict(scope.get("headers", []))
    host = headers.get(b"host", b"").decode("latin-1")
    host_parts = _host_parts(host)
    scheme = str(scope.get("scheme", "http")).lower()
    if host_parts is None or scheme not in {"http", "https"}:
        return False
    hostname, configured_port = host_parts
    port = configured_port or (443 if scheme == "https" else 80)
    return origin_parts == (scheme, hostname, port)


def _normalize_allowed_hosts(configured: Any) -> tuple[str, ...]:
    if (
        not isinstance(configured, list)
        or not configured
        or not all(isinstance(item, str) and item.strip() for item in configured)
    ):
        raise RuntimeError(
            'The "allowed-hosts" setting must be a non-empty list of hosts.'
        )
    patterns: set[str] = set()
    for item in configured:
        pattern = item.strip().lower()
        if pattern == "*":
            patterns.add(pattern)
            continue
        wildcard = pattern.startswith("*.")
        hostname = pattern[2:] if wildcard else pattern
        if hostname.startswith("[") and hostname.endswith("]"):
            hostname = hostname[1:-1]
        if any(character in hostname for character in "/\\?#@"):
            raise RuntimeError(f"Invalid allowed host: {item!r}.")
        canonical = _canonical_hostname(hostname)
        if canonical is None or (wildcard and ":" in canonical):
            raise RuntimeError(f"Invalid allowed host: {item!r}.")
        patterns.add(f"*.{canonical}" if wildcard else canonical)
    return tuple(sorted(patterns))


def _host_allowed(hostname: str, patterns: tuple[str, ...]) -> bool:
    return any(
        pattern == "*"
        or hostname == pattern
        or (pattern.startswith("*.") and hostname.endswith(pattern[1:]))
        for pattern in patterns
    )


class HostPolicyMiddleware:
    def __init__(self, application, allowed_hosts: tuple[str, ...]) -> None:
        self.application = application
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            raw_host = headers.get(b"host", b"").decode("latin-1")
            parts = _host_parts(raw_host)
            if parts is None or not _host_allowed(parts[0], self.allowed_hosts):
                response = _error(400, "Invalid Host header.")
                await response(scope, receive, send)
                return
        await self.application(scope, receive, send)


class RequestBodyLimitMiddleware:
    def __init__(self, application, maximum_bytes: int) -> None:
        self.application = application
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        content_lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if content_lengths:
            try:
                decoded = {value.decode("ascii") for value in content_lengths}
                if len(decoded) != 1:
                    raise ValueError
                text = decoded.pop()
                if not text.isdigit():
                    raise ValueError
                content_length = int(text)
            except (UnicodeDecodeError, ValueError):
                response = _error(400, "Invalid Content-Length header.")
                await response(scope, receive, send)
                return
            if content_length > self.maximum_bytes:
                response = _error(
                    413,
                    f"Request body exceeds the {self.maximum_bytes}-byte limit.",
                )
                await response(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.maximum_bytes:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.application(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            response = _error(
                413,
                f"Request body exceeds the {self.maximum_bytes}-byte limit.",
            )
            await response(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, application, csp: str | None = None) -> None:
        self.application = application
        self.csp = csp

    async def __call__(self, scope, receive, send) -> None:
        async def add_headers(message) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "SAMEORIGIN")
                headers.setdefault(
                    "Referrer-Policy",
                    "strict-origin-when-cross-origin",
                )
                if self.csp:
                    headers.setdefault("Content-Security-Policy", self.csp)
            await send(message)

        await self.application(scope, receive, add_headers)


class EndpointCORSPolicyMiddleware:
    def __init__(
        self,
        application,
        rules: dict[tuple[str, str], frozenset[str]],
        default_origins: frozenset[str],
    ) -> None:
        self.application = application
        self.rules = rules
        self.default_origins = default_origins

    def _allowed_origins(self, scope) -> frozenset[str]:
        method = scope.get("method", "GET").upper()
        headers = dict(scope.get("headers", []))
        if method == "OPTIONS":
            requested_method = headers.get(b"access-control-request-method")
            if requested_method:
                method = requested_method.decode("latin-1").upper()
        # Starlette automatically serves HEAD through GET routes. Treat both
        # methods as the same policy target so a HEAD request cannot fall back
        # to a broader default CORS rule.
        if method == "HEAD":
            method = "GET"
        return self.rules.get(
            (method, scope.get("path", "")),
            self.default_origins,
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            raw_origin = headers.get(b"origin")
            path = scope.get("path", "")
            api_root = API_PREFIX.rstrip("/")
            is_api = path == api_root or path.startswith(API_PREFIX)
            fetch_site = headers.get(b"sec-fetch-site", b"").lower()
            if is_api and fetch_site == b"cross-site" and not raw_origin:
                response = _error(403, "Cross-site API request is not allowed.")
                await response(scope, receive, send)
                return
            if raw_origin:
                origin = raw_origin.decode("latin-1")
                canonical_origin = _canonical_origin(origin)
                allowed = self._allowed_origins(scope)
                if canonical_origin not in allowed and not _same_origin(scope, origin):
                    response = _error(403, "Origin is not allowed.")
                    await response(scope, receive, send)
                    return
        await self.application(scope, receive, send)


def _normalize_origins(configured: Any, setting: str) -> list[str]:
    if not isinstance(configured, list) or not all(
        isinstance(item, str) for item in configured
    ):
        raise RuntimeError(f'The "{setting}" setting must be a list of origins.')
    origins: set[str] = set()
    for item in configured:
        if not item.strip():
            continue
        origin = _canonical_origin(item.strip())
        if origin is None:
            raise RuntimeError(f"Invalid CORS origin: {item!r}.")
        origins.add(origin)
    return sorted(origins)


def _cors_policy() -> tuple[
    dict[tuple[str, str], frozenset[str]],
    frozenset[str],
    list[str],
]:
    default = frozenset(
        _normalize_origins(CONFIG.get("default-CORS", []), "default-CORS")
    )
    all_origins = set(default)
    rules: dict[tuple[str, str], frozenset[str]] = {}
    for record in ENDPOINTS:
        metadata = record["metadata"]
        origins = frozenset(
            _normalize_origins(
                metadata.get("cors", list(default)),
                f"CORS for {metadata['name']}",
            )
        )
        path = f"{API_PREFIX}{metadata['endpoint']}"
        rules[(metadata["method"].upper(), path)] = origins
        all_origins.update(origins)
    return rules, default, sorted(all_origins)


def create_app():
    secret = os.environ.get("SYKIT_SESSION_SECRET")
    if not secret or len(secret.encode("utf-8")) < 32:
        raise RuntimeError(
            "SYKIT_SESSION_SECRET must contain at least 32 bytes. "
            "Set it to a long, random value before starting SyKit."
        )
    https_only = CONFIG.get("session-https-only", False)
    if not isinstance(https_only, bool):
        raise RuntimeError('The "session-https-only" setting must be true or false.')
    max_age = _positive_integer_setting("session-max-age", 1_209_600)
    application = Starlette(routes=_routes())
    application = SessionMiddleware(
        application,
        secret_key=secret,
        session_cookie=SESSION_COOKIE,
        same_site="lax",
        https_only=https_only,
        max_age=max_age,
    )
    application = RequestBodyLimitMiddleware(application, MAX_REQUEST_BYTES)
    rules, default_origins, origins = _cors_policy()
    if origins:
        application = CORSMiddleware(
            application,
            allow_origins=origins,
            allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
            allow_credentials=True,
        )
    application = EndpointCORSPolicyMiddleware(
        application,
        rules,
        default_origins,
    )
    allowed_hosts = _normalize_allowed_hosts(
        CONFIG.get("allowed-hosts", ["127.0.0.1", "localhost", "::1"])
    )
    application = HostPolicyMiddleware(application, allowed_hosts)
    csp = CONFIG.get("content-security-policy")
    if csp is not None and not isinstance(csp, str):
        raise RuntimeError('The "content-security-policy" setting must be a string.')
    application = SecurityHeadersMiddleware(application, csp)
    return application


app = create_app()


def run() -> None:
    host = CONFIG.get("host-ip", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise RuntimeError('The "host-ip" setting must be a non-empty string.')
    port = _positive_integer_setting("host-port", 8000)
    if port > 65535:
        raise RuntimeError('The "host-port" setting must be at most 65535.')
    workers = _positive_integer_setting("workers", 1)
    uvicorn.run(
        "server:app",
        host=host.strip(),
        port=port,
        workers=workers,
        # Direct clients must not spoof the scheme or their address through
        # X-Forwarded-* headers. Re-enable only behind a trusted reverse proxy.
        proxy_headers=False,
    )


__all__ = ["app", "create_app", "run"]
