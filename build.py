from __future__ import annotations

import ast
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

try:
    from .check_requirements import RequirementError, check_requirements
except ImportError:
    from check_requirements import RequirementError, check_requirements


SRC_DIR = Path("src")
CACHE_DIR = Path("__sykitcache__")
BUILT_DIR = Path("built")
STAGING_DIR = Path(".__sykit_built_tmp__")
BACKUP_DIR = Path(".__sykit_built_backup__")
ENV_PATH = Path(".env")
ENV_EXAMPLE_PATH = Path(".env.example")
GITIGNORE_PATH = Path(".gitignore")
TOOL_DIR = Path(__file__).resolve().parent
SOURCE_FILES_DIR = TOOL_DIR / "files"
FRONTEND_BUILD_DIR = SOURCE_FILES_DIR / "frontend-build"
FRONTEND_MANIFEST_PATH = FRONTEND_BUILD_DIR / "package.json"
FRONTEND_LOCK_PATH = FRONTEND_BUILD_DIR / "package-lock.json"

DECORATOR_METHODS = {
    "expose": "POST",
    "raw": "GET",
    "web_hook": "POST",
}
ENDPOINT_DECORATORS = frozenset(DECORATOR_METHODS) | {
    "api_key",
    "cors",
    "hidden",
    "limits",
    "perms",
    "requires",
}
CLIENT_DECORATORS = {"expose", "raw"}
INJECTED_PARAMETERS = {"session", "request"}
LIMIT_KEYS = {"per-client", "per-key", "per-session", "site-wide", "per-worker"}
SCOPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
LIMIT_WINDOWS = {"s": 1, "m": 60, "hr": 3600}
IGNORED_SOURCE_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svelte-kit",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
    "venv",
}
RESERVED_MODULE_ROOTS = frozenset(sys.stdlib_module_names) | {
    "anyio",
    "app",
    "click",
    "core",
    "dotenv",
    "h11",
    "idna",
    "itsdangerous",
    "main",
    "server",
    "sniffio",
    "starlette",
    "sykit",
    "typing_extensions",
    "uvicorn",
}
JS_RESERVED_WORDS = {
    "arguments",
    "await",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "debugger",
    "default",
    "delete",
    "do",
    "else",
    "enum",
    "export",
    "eval",
    "extends",
    "false",
    "finally",
    "for",
    "function",
    "if",
    "implements",
    "import",
    "in",
    "instanceof",
    "interface",
    "let",
    "new",
    "null",
    "package",
    "private",
    "protected",
    "public",
    "return",
    "static",
    "super",
    "switch",
    "this",
    "throw",
    "true",
    "try",
    "typeof",
    "var",
    "void",
    "while",
    "with",
    "yield",
}
CLIENT_RESERVED_EXPORTS = {"SyKitError", "globalThis", "hidden_api"}
HIDDEN_MANIFEST_ENDPOINT = "__sykit_manifest__"
FRONTEND_PACKAGE_NAMES = {
    "@sveltejs/vite-plugin-svelte",
    "svelte",
    "vite",
}
NPM_VERSION_SPEC = re.compile(r"[A-Za-z0-9.*+<>=~^| -]{1,100}")
PINNED_NPM_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


class BuildError(RuntimeError):
    """A user-facing build failure."""


def _load_frontend_manifest() -> tuple[dict[str, Any], dict[str, str]]:
    try:
        with FRONTEND_MANIFEST_PATH.open("r", encoding="utf-8") as file:
            manifest = json.load(
                file,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise BuildError(
            f"Could not read frontend build manifest {FRONTEND_MANIFEST_PATH}: {error}"
        ) from error
    if not isinstance(manifest, dict):
        raise BuildError(f"{FRONTEND_MANIFEST_PATH} must contain a JSON object.")
    dependencies = manifest.get("dependencies")
    if (
        not isinstance(dependencies, dict)
        or set(dependencies) != FRONTEND_PACKAGE_NAMES
        or not all(
            isinstance(package, str)
            and isinstance(spec, str)
            and PINNED_NPM_VERSION.fullmatch(spec)
            for package, spec in dependencies.items()
        )
    ):
        raise BuildError(
            f"{FRONTEND_MANIFEST_PATH} must pin exact versions for exactly: "
            + ", ".join(sorted(FRONTEND_PACKAGE_NAMES))
            + "."
        )
    return manifest, dict(dependencies)


def _frontend_dependencies(config: dict[str, Any]) -> dict[str, str]:
    _manifest, defaults = _load_frontend_manifest()
    configured = config.get("frontend-packages", {})
    if not isinstance(configured, dict):
        raise BuildError('"frontend-packages" must be an object.')
    unknown = sorted(set(configured) - set(defaults))
    if unknown:
        raise BuildError(
            'Unknown "frontend-packages" entries: ' + ", ".join(unknown) + "."
        )

    dependencies = defaults.copy()
    for package, spec in configured.items():
        if not isinstance(spec, str) or not NPM_VERSION_SPEC.fullmatch(spec):
            raise BuildError(
                f'"frontend-packages.{package}" must be a non-empty npm version, '
                "range, or registry tag. URLs, paths, and git sources are not allowed."
            )
        dependencies[package] = spec
    return dependencies


@dataclass(frozen=True)
class ParameterInfo:
    name: str
    injected: bool
    required: bool
    upload: bool = False


@dataclass(frozen=True)
class EndpointInfo:
    kind: str
    method: str
    endpoint: str
    function: str
    module: str
    file: str
    is_async: bool
    parameters: tuple[ParameterInfo, ...]
    permissions: dict[str, Any] | None
    cors: tuple[str, ...] | None
    limits: dict[str, dict[str, int] | None] | None
    hidden: bool = False
    token: str | None = None
    api_key: dict[str, Any] | None = None
    max_upload_bytes: int | None = None

    @property
    def client_parameters(self) -> tuple[ParameterInfo, ...]:
        return tuple(
            parameter for parameter in self.parameters if not parameter.injected
        )


@dataclass(frozen=True)
class TaskInfo:
    function: str
    module: str
    file: str
    is_async: bool
    schedule: dict[str, Any] | None = None

    @property
    def task_id(self) -> str:
        return f"{self.module}:{self.function}"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant {value!r}.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        value[key] = item
    return value


def load_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as file:
            config = json.load(
                file,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise BuildError(f"Could not read {config_path}: {error}") from error
    if not isinstance(config, dict):
        raise BuildError(f"{config_path} must contain a JSON object.")
    return config


def _walk_source(root: Path):
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names[:] = sorted(name for name in names if name not in IGNORED_SOURCE_DIRS)
        yield Path(directory), sorted(files)


def find_sykit_dir(root: Path) -> Path | None:
    matches = sorted(
        directory
        for directory, files in _walk_source(root)
        if directory.name == "sykit" and "config.json" in files
    )
    if not matches:
        return None
    if len(matches) > 1:
        display = ", ".join(str(path) for path in matches)
        raise BuildError(f"Multiple sykit configuration folders found: {display}")
    return matches[0]


def list_python_files(root: Path, sykit_dir: Path) -> list[Path]:
    internal = sykit_dir.resolve()
    files: list[Path] = []
    for directory, names in _walk_source(root):
        resolved_directory = directory.resolve()
        if internal == resolved_directory or internal in resolved_directory.parents:
            continue
        files.extend(directory / name for name in names if name.endswith(".py"))
    return sorted(files)


def validate_module_roots(
    python_files: list[Path], source_root: Path = SRC_DIR
) -> None:
    for path in python_files:
        relative = path.relative_to(source_root).with_suffix("")
        root = relative.parts[0]
        if root == "__init__":
            continue
        if root in RESERVED_MODULE_ROOTS:
            raise BuildError(
                f"{path}: top-level module name {root!r} conflicts with the "
                "generated runtime. Rename it or place it inside an application package."
            )


def _get_call_name(node: ast.expr) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literal_argument(decorator: ast.Call, name: str, path: Path) -> Any:
    if len(decorator.args) != 1 or decorator.keywords:
        raise BuildError(
            f"{path}: @{name} must have exactly one literal positional argument."
        )
    try:
        return ast.literal_eval(decorator.args[0])
    except (ValueError, TypeError) as error:
        raise BuildError(f"{path}: @{name} requires a literal value.") from error


def _route_arguments(
    decorator: ast.Call,
    name: str,
    path: Path,
) -> tuple[Any, int | None]:
    if name != "expose":
        return _literal_argument(decorator, name, path), None
    if len(decorator.args) != 1:
        raise BuildError(
            f"{path}: @expose needs one literal endpoint path and may include "
            "max_upload_bytes as a keyword."
        )
    unknown = [
        keyword.arg
        for keyword in decorator.keywords
        if keyword.arg != "max_upload_bytes"
    ]
    if unknown:
        displayed = "**options" if None in unknown else str(unknown[0])
        raise BuildError(f"{path}: unsupported @expose option {displayed!r}.")
    try:
        endpoint = ast.literal_eval(decorator.args[0])
        limits = [
            ast.literal_eval(keyword.value)
            for keyword in decorator.keywords
            if keyword.arg == "max_upload_bytes"
        ]
    except (ValueError, TypeError) as error:
        raise BuildError(f"{path}: @expose requires literal values.") from error
    if len(limits) > 1:
        raise BuildError(f"{path}: @expose may set max_upload_bytes only once.")
    maximum = limits[0] if limits else None
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1
    ):
        raise BuildError(f"{path}: max_upload_bytes must be a positive integer.")
    return endpoint, maximum


def _normalize_endpoint(value: Any, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuildError(f"{path}: endpoint paths must be non-empty strings.")
    endpoint = value.strip().replace("\\", "/").strip("/")
    if (
        not endpoint
        or any(character in endpoint for character in "?#{}")
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in endpoint
        )
    ):
        raise BuildError(f"{path}: invalid endpoint path {value!r}.")
    segments = endpoint.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise BuildError(f"{path}: invalid endpoint path {value!r}.")
    if endpoint == HIDDEN_MANIFEST_ENDPOINT:
        raise BuildError(
            f"{path}: endpoint path {HIDDEN_MANIFEST_ENDPOINT!r} is reserved by SyKit."
        )
    return endpoint


def _validate_permissions(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BuildError(f"{path}: @requires expects a dictionary.")
    unknown = set(value) - {"Session"}
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise BuildError(f"{path}: unsupported permission sections: {names}.")
    session = value.get("Session", {})
    if not isinstance(session, dict):
        raise BuildError(f'{path}: @requires["Session"] must be a dictionary.')
    if not all(isinstance(key, str) and key for key in session):
        raise BuildError(
            f'{path}: @requires["Session"] keys must be non-empty strings.'
        )
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise BuildError(
            f"{path}: permissions must contain JSON-compatible values."
        ) from error
    return value


def _canonical_hostname(value: str, path: Path) -> str:
    hostname = value.rstrip(".")
    if not hostname or any(character.isspace() for character in hostname):
        raise BuildError(f"{path}: invalid host name {value!r}.")
    try:
        return ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        try:
            ascii_name = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise BuildError(f"{path}: invalid host name {value!r}.") from error
        if len(ascii_name) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in ascii_name.split(".")
        ):
            raise BuildError(f"{path}: invalid host name {value!r}.")
        return ascii_name


def _canonical_origin(value: str, path: Path) -> str:
    if "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise BuildError(f"{path}: invalid CORS origin {value!r}.")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname is not None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
        )
        port = parsed.port
    except ValueError:
        valid = False
        port = None
    if not valid or parsed.hostname is None:
        raise BuildError(f"{path}: invalid CORS origin {value!r}.")
    scheme = parsed.scheme.lower()
    hostname = _canonical_hostname(parsed.hostname, path)
    displayed_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    port_suffix = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{displayed_host}{port_suffix}"


def _validate_cors(value: Any, path: Path) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise BuildError(f"{path}: CORS must be a list of origin strings.")
    return tuple(
        sorted(
            {_canonical_origin(item.strip(), path) for item in value if item.strip()}
        )
    )


def _validate_allowed_hosts(value: Any, path: Path) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise BuildError(f'{path}: "allowed-hosts" must be a non-empty list of hosts.')
    patterns: set[str] = set()
    for item in value:
        pattern = item.strip().lower()
        if pattern == "*":
            patterns.add(pattern)
            continue
        wildcard = pattern.startswith("*.")
        hostname = pattern[2:] if wildcard else pattern
        if hostname.startswith("[") and hostname.endswith("]"):
            hostname = hostname[1:-1]
        if any(character in hostname for character in "/\\?#@"):
            raise BuildError(f"{path}: invalid allowed host {item!r}.")
        canonical = _canonical_hostname(hostname, path)
        if wildcard and ":" in canonical:
            raise BuildError(f"{path}: invalid allowed host {item!r}.")
        patterns.add(f"*.{canonical}" if wildcard else canonical)
    return tuple(sorted(patterns))


def _parse_limit(value: Any, key: str, path: Path) -> dict[str, int] | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise BuildError(
            f"{path}: limit {key!r} must be -1 or a value such as 10s, 10m, or 10hr."
        )
    text = str(value).strip().lower()
    if text == "-1":
        return None
    match = re.fullmatch(r"([1-9]\d*)(s|m|hr)?", text)
    if match is None:
        raise BuildError(
            f"{path}: limit {key!r} must be -1 or a value such as 10s, 10m, or 10hr."
        )
    unit = match.group(2) or "m"
    return {"requests": int(match.group(1)), "window": LIMIT_WINDOWS[unit]}


def _validate_api_key_scopes(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, list) or not all(
        isinstance(scope, str) and SCOPE_PATTERN.fullmatch(scope) for scope in value
    ):
        raise BuildError(
            f"{path}: @api_key expects a list of scope names (letters, "
            'digits, "_", ".", ":", "-").'
        )
    folded = [scope.casefold() for scope in value]
    if len(set(folded)) != len(folded):
        raise BuildError(f"{path}: @api_key scopes may not contain duplicates.")
    return {"scopes": list(value)}


def _validate_limits(
    value: Any,
    path: Path,
) -> dict[str, dict[str, int] | None]:
    if not isinstance(value, dict):
        raise BuildError(f"{path}: @limits expects a dictionary.")
    unknown = set(value) - LIMIT_KEYS
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise BuildError(f"{path}: unsupported limit names: {names}.")
    return {
        key: _parse_limit(value.get(key, -1), key, path) for key in sorted(LIMIT_KEYS)
    }


def apply_endpoint_defaults(
    config: dict[str, Any],
    endpoints: list[EndpointInfo],
    config_path: Path,
) -> list[EndpointInfo]:
    max_request_bytes = config.get("max-request-bytes", 1_048_576)
    if (
        isinstance(max_request_bytes, bool)
        or not isinstance(max_request_bytes, int)
        or max_request_bytes < 1
    ):
        raise BuildError(f'{config_path}: "max-request-bytes" must be positive.')
    default_permissions = _validate_permissions(
        config.get("default-perms", {}), config_path
    )
    default_cors = _validate_cors(config.get("default-CORS", []), config_path)
    default_limits = _validate_limits(config.get("default-limits", {}), config_path)
    for endpoint in endpoints:
        if (
            endpoint.max_upload_bytes is not None
            and endpoint.max_upload_bytes > max_request_bytes
        ):
            raise BuildError(
                f"{endpoint.file}: max_upload_bytes cannot exceed the global "
                f'"max-request-bytes" value ({max_request_bytes}).'
            )
    return [
        replace(
            endpoint,
            permissions=(
                default_permissions
                if endpoint.permissions is None
                else endpoint.permissions
            ),
            cors=default_cors if endpoint.cors is None else endpoint.cors,
            limits=default_limits if endpoint.limits is None else endpoint.limits,
        )
        for endpoint in endpoints
    ]


def validate_hidden_endpoints(endpoints: list[EndpointInfo]) -> None:
    for endpoint in endpoints:
        if not endpoint.hidden:
            continue
        session_permissions = (endpoint.permissions or {}).get("Session") or {}
        if not session_permissions:
            raise BuildError(
                f"{endpoint.file}: @hidden endpoint {endpoint.function!r} needs "
                'session permissions (add @perms or set "default-perms").'
            )


def assign_hidden_tokens(endpoints: list[EndpointInfo]) -> list[EndpointInfo]:
    return [
        replace(endpoint, token=secrets.token_hex(16))
        if endpoint.hidden and endpoint.kind in CLIENT_DECORATORS
        else endpoint
        for endpoint in endpoints
    ]


def _module_name(
    path: Path,
    source_root: Path,
    declaration: str = "endpoint",
) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts:
        raise BuildError(f"{path}: root __init__.py cannot declare a {declaration}.")
    invalid = [part for part in parts if not part.isidentifier()]
    if invalid:
        raise BuildError(
            f"{path}: {declaration} modules must use valid Python identifiers; "
            f"invalid component {invalid[0]!r}."
        )
    return ".".join(parts)


def _annotation_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_none_annotation(node: ast.expr) -> bool:
    return (isinstance(node, ast.Constant) and node.value is None) or (
        isinstance(node, ast.Name) and node.id == "None"
    )


def _is_upload_annotation(node: ast.expr | None) -> bool:
    if node is None:
        return False
    if _annotation_name(node) == "Upload":
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        members: list[ast.expr] = []

        def collect(item: ast.expr) -> None:
            if isinstance(item, ast.BinOp) and isinstance(item.op, ast.BitOr):
                collect(item.left)
                collect(item.right)
            else:
                members.append(item)

        collect(node)
        return sum(_annotation_name(item) == "Upload" for item in members) == 1 and all(
            _annotation_name(item) == "Upload" or _is_none_annotation(item)
            for item in members
        )
    if isinstance(node, ast.Subscript) and _annotation_name(node.value) in {
        "Optional",
        "Union",
    }:
        values = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        return sum(_annotation_name(item) == "Upload" for item in values) == 1 and all(
            _annotation_name(item) == "Upload" or _is_none_annotation(item)
            for item in values
        )
    return False


def _parameter_info(
    node: ast.FunctionDef | ast.AsyncFunctionDef, path: Path
) -> tuple[ParameterInfo, ...]:
    arguments = node.args
    if arguments.posonlyargs:
        raise BuildError(
            f"{path}:{node.lineno}: endpoint functions cannot use positional-only parameters."
        )
    if arguments.vararg or arguments.kwarg:
        raise BuildError(
            f"{path}:{node.lineno}: endpoint functions cannot use *args or **kwargs."
        )

    positional = list(arguments.args)
    positional_defaults = [False] * (len(positional) - len(arguments.defaults)) + [
        True
    ] * len(arguments.defaults)
    parameters: list[ParameterInfo] = []
    for argument, has_default in zip(positional, positional_defaults):
        parameters.append(
            ParameterInfo(
                name=argument.arg,
                injected=argument.arg in INJECTED_PARAMETERS,
                required=not has_default and argument.arg not in INJECTED_PARAMETERS,
                upload=_is_upload_annotation(argument.annotation),
            )
        )
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
        parameters.append(
            ParameterInfo(
                name=argument.arg,
                injected=argument.arg in INJECTED_PARAMETERS,
                required=default is None and argument.arg not in INJECTED_PARAMETERS,
                upload=_is_upload_annotation(argument.annotation),
            )
        )

    for parameter in parameters:
        if parameter.injected and parameter.upload:
            raise BuildError(
                f"{path}:{node.lineno}: injected parameter {parameter.name!r} "
                "cannot be annotated as Upload."
            )
        if parameter.name in JS_RESERVED_WORDS and not parameter.injected:
            raise BuildError(
                f"{path}:{node.lineno}: parameter {parameter.name!r} is reserved in JavaScript."
            )
    return tuple(parameters)


def parse_decorators(path: Path, source_root: Path = SRC_DIR) -> list[EndpointInfo]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except OSError as error:
        raise BuildError(f"Could not read {path}: {error}") from error
    except SyntaxError as error:
        raise BuildError(
            f"Syntax error in {path}:{error.lineno}: {error.msg}"
        ) from error

    results: list[EndpointInfo] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        endpoint_kind: str | None = None
        endpoint_path: str | None = None
        max_upload_bytes: int | None = None
        permissions: dict[str, Any] | None = None
        cors: tuple[str, ...] | None = None
        endpoint_limits: dict[str, dict[str, int] | None] | None = None
        is_hidden = False
        api_key_info: dict[str, Any] | None = None
        for decorator in node.decorator_list:
            name = _get_call_name(decorator)
            if isinstance(decorator, (ast.Name, ast.Attribute)):
                bare_name = (
                    decorator.id if isinstance(decorator, ast.Name) else decorator.attr
                )
                if bare_name in {
                    *DECORATOR_METHODS,
                    "cors",
                    "limits",
                    "perms",
                    "requires",
                }:
                    raise BuildError(
                        f"{path}:{node.lineno}: @{bare_name} must be called with an argument."
                    )
                if bare_name == "hidden":
                    if is_hidden:
                        raise BuildError(
                            f"{path}:{node.lineno}: only one @hidden decorator is allowed."
                        )
                    is_hidden = True
                if bare_name == "api_key":
                    if api_key_info is not None:
                        raise BuildError(
                            f"{path}:{node.lineno}: only one @api_key decorator is allowed."
                        )
                    api_key_info = {"scopes": []}
            if name == "hidden":
                raise BuildError(
                    f"{path}:{node.lineno}: @hidden must be used without arguments."
                )
            if name in DECORATOR_METHODS:
                if endpoint_kind is not None:
                    raise BuildError(
                        f"{path}:{node.lineno}: an endpoint function may only have one route decorator."
                    )
                decorator = cast(ast.Call, decorator)
                endpoint_kind = name
                route_value, max_upload_bytes = _route_arguments(
                    decorator,
                    name,
                    path,
                )
                endpoint_path = _normalize_endpoint(
                    route_value,
                    path,
                )
            elif name in {"perms", "requires"}:
                if permissions is not None:
                    raise BuildError(
                        f"{path}:{node.lineno}: only one permissions decorator is allowed."
                    )
                decorator = cast(ast.Call, decorator)
                permissions = _validate_permissions(
                    _literal_argument(decorator, name, path), path
                )
            elif name == "cors":
                if cors is not None:
                    raise BuildError(
                        f"{path}:{node.lineno}: only one @cors decorator is allowed."
                    )
                decorator = cast(ast.Call, decorator)
                cors = _validate_cors(_literal_argument(decorator, name, path), path)
            elif name == "limits":
                if endpoint_limits is not None:
                    raise BuildError(
                        f"{path}:{node.lineno}: only one @limits decorator is allowed."
                    )
                decorator = cast(ast.Call, decorator)
                endpoint_limits = _validate_limits(
                    _literal_argument(decorator, name, path), path
                )
            elif name == "api_key":
                if api_key_info is not None:
                    raise BuildError(
                        f"{path}:{node.lineno}: only one @api_key decorator is allowed."
                    )
                decorator = cast(ast.Call, decorator)
                api_key_info = _validate_api_key_scopes(
                    _literal_argument(decorator, name, path), path
                )

        if endpoint_kind is None or endpoint_path is None:
            continue
        parameters = _parameter_info(node, path)
        upload_parameters = [item for item in parameters if item.upload]
        if upload_parameters and endpoint_kind != "expose":
            raise BuildError(
                f"{path}:{node.lineno}: Upload parameters are only supported on "
                "@expose endpoints."
            )
        if max_upload_bytes is not None and not upload_parameters:
            raise BuildError(
                f"{path}:{node.lineno}: max_upload_bytes requires at least one "
                "Upload parameter."
            )
        if is_hidden and cors is not None:
            raise BuildError(
                f"{path}:{node.lineno}: @hidden cannot be combined with @cors; "
                "a custom CORS rule would make the endpoint detectable."
            )
        if api_key_info is not None:
            if endpoint_kind != "web_hook":
                raise BuildError(
                    f"{path}:{node.lineno}: @api_key is only for @web_hook "
                    "endpoints; browser calls carry sessions, not keys."
                )
            if is_hidden:
                raise BuildError(
                    f"{path}:{node.lineno}: @api_key cannot be combined with @hidden."
                )
        if (
            endpoint_limits is not None
            and endpoint_limits.get("per-key") is not None
            and api_key_info is None
        ):
            raise BuildError(
                f"{path}:{node.lineno}: a per-key rate limit requires @api_key."
            )
        if endpoint_kind in CLIENT_DECORATORS:
            if node.name in JS_RESERVED_WORDS:
                raise BuildError(
                    f"{path}:{node.lineno}: endpoint name {node.name!r} is "
                    "reserved in JavaScript."
                )
            if node.name in CLIENT_RESERVED_EXPORTS:
                raise BuildError(
                    f"{path}:{node.lineno}: endpoint name {node.name!r} is "
                    "reserved by the generated $python client."
                )
        results.append(
            EndpointInfo(
                kind=endpoint_kind,
                method=DECORATOR_METHODS[endpoint_kind],
                endpoint=endpoint_path,
                function=node.name,
                module=_module_name(path, source_root),
                file=path.relative_to(source_root).as_posix(),
                is_async=isinstance(node, ast.AsyncFunctionDef),
                parameters=parameters,
                permissions=permissions,
                cors=cors,
                limits=endpoint_limits,
                hidden=is_hidden,
                api_key=api_key_info,
                max_upload_bytes=max_upload_bytes,
            )
        )
    return results


def _cron_field(
    value: str,
    minimum: int,
    maximum: int,
    name: str,
    location: str,
) -> tuple[tuple[int, ...], bool]:
    values: set[int] = set()
    if not value or value.startswith(",") or value.endswith(","):
        raise BuildError(f"{location}: invalid {name} field in @scheduled.")
    for component in value.split(","):
        if not component or component.count("/") > 1:
            raise BuildError(f"{location}: invalid {name} field in @scheduled.")
        base, separator, step_text = component.partition("/")
        if separator:
            if not step_text.isdigit() or int(step_text) < 1:
                raise BuildError(
                    f"{location}: {name} steps in @scheduled must be positive integers."
                )
            step = int(step_text)
        else:
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif base.count("-") == 1:
            start_text, end_text = base.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise BuildError(f"{location}: invalid {name} range in @scheduled.")
            start, end = int(start_text), int(end_text)
        elif base.isdigit():
            start = int(base)
            end = maximum if separator else start
        else:
            raise BuildError(f"{location}: invalid {name} field in @scheduled.")

        if start < minimum or end > maximum or start > end:
            raise BuildError(
                f"{location}: {name} values in @scheduled must be between "
                f"{minimum} and {maximum}."
            )
        values.update(range(start, end + 1, step))

    if name == "day-of-week":
        values = {0 if item == 7 else item for item in values}
        complete = set(range(0, 7))
    else:
        complete = set(range(minimum, maximum + 1))
    return tuple(sorted(values)), values == complete


def parse_cron(expression: Any, path: Path, line: int) -> dict[str, Any]:
    location = f"{path}:{line}"
    if not isinstance(expression, str) or not expression.strip():
        raise BuildError(f"{location}: @scheduled requires a cron string.")
    parts = expression.split()
    if len(parts) != 5:
        raise BuildError(
            f"{location}: @scheduled requires five cron fields: minute, hour, "
            "day-of-month, month, and day-of-week."
        )
    definitions = (
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day-of-month", 1, 31),
        ("month", 1, 12),
        ("day-of-week", 0, 7),
    )
    parsed = [
        _cron_field(value, minimum, maximum, name, location)
        for value, (name, minimum, maximum) in zip(parts, definitions)
    ]
    return {
        "minute": parsed[0][0],
        "hour": parsed[1][0],
        "day": parsed[2][0],
        "month": parsed[3][0],
        "weekday": parsed[4][0],
        "day_any": parsed[2][1],
        "weekday_any": parsed[4][1],
    }


def parse_tasks(path: Path, source_root: Path = SRC_DIR) -> list[TaskInfo]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except OSError as error:
        raise BuildError(f"Could not read {path}: {error}") from error
    except SyntaxError as error:
        raise BuildError(
            f"Syntax error in {path}:{error.lineno}: {error.msg}"
        ) from error

    results: list[TaskInfo] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declaration: str | None = None
        schedule: dict[str, Any] | None = None
        has_endpoint_decorator = False
        for decorator in node.decorator_list:
            call_name = _get_call_name(decorator)
            if isinstance(decorator, (ast.Name, ast.Attribute)):
                bare_name = (
                    decorator.id if isinstance(decorator, ast.Name) else decorator.attr
                )
                if bare_name == "scheduled":
                    raise BuildError(
                        f"{path}:{node.lineno}: @scheduled must be called with a "
                        "cron expression."
                    )
                if bare_name == "task":
                    if declaration is not None:
                        raise BuildError(
                            f"{path}:{node.lineno}: only one background task "
                            "decorator is allowed."
                        )
                    declaration = "task"
                if bare_name in ENDPOINT_DECORATORS:
                    has_endpoint_decorator = True
            if call_name in ENDPOINT_DECORATORS:
                has_endpoint_decorator = True
            if call_name == "task":
                raise BuildError(
                    f"{path}:{node.lineno}: @task must be used without arguments."
                )
            if call_name == "scheduled":
                if declaration is not None:
                    raise BuildError(
                        f"{path}:{node.lineno}: only one background task decorator "
                        "is allowed."
                    )
                declaration = "scheduled"
                decorator = cast(ast.Call, decorator)
                expression = _literal_argument(decorator, "scheduled", path)
                schedule = parse_cron(expression, path, node.lineno)

        if declaration is None:
            continue
        if has_endpoint_decorator:
            raise BuildError(
                f"{path}:{node.lineno}: background tasks cannot use endpoint "
                "decorators."
            )
        arguments = node.args
        if declaration == "scheduled" and (
            arguments.posonlyargs
            or arguments.args
            or arguments.vararg
            or arguments.kwonlyargs
            or arguments.kwarg
        ):
            raise BuildError(
                f"{path}:{node.lineno}: scheduled tasks cannot declare parameters."
            )
        results.append(
            TaskInfo(
                function=node.name,
                module=_module_name(path, source_root, "background task"),
                file=path.relative_to(source_root).as_posix(),
                is_async=isinstance(node, ast.AsyncFunctionDef),
                schedule=schedule,
            )
        )
    return results


def detect_tasks(
    python_files: list[Path], source_root: Path = SRC_DIR
) -> list[TaskInfo]:
    tasks: list[TaskInfo] = []
    names: dict[str, TaskInfo] = {}
    for path in python_files:
        for task_info in parse_tasks(path, source_root):
            previous = names.get(task_info.task_id)
            if previous is not None:
                raise BuildError(
                    f"Duplicate background task {task_info.task_id!r} in "
                    f"{previous.file} and {task_info.file}."
                )
            names[task_info.task_id] = task_info
            tasks.append(task_info)
    return sorted(tasks, key=lambda item: item.task_id)


def detect_endpoints(
    python_files: list[Path], source_root: Path = SRC_DIR
) -> list[EndpointInfo]:
    endpoints: list[EndpointInfo] = []
    for path in python_files:
        endpoints.extend(parse_decorators(path, source_root))

    routes: dict[tuple[str, str], EndpointInfo] = {}
    exports: dict[str, EndpointInfo] = {}
    for endpoint in endpoints:
        route_key = (endpoint.method, endpoint.endpoint)
        if route_key in routes:
            previous = routes[route_key]
            raise BuildError(
                f"Duplicate {endpoint.method} endpoint {endpoint.endpoint!r} in "
                f"{previous.file} and {endpoint.file}."
            )
        routes[route_key] = endpoint
        if endpoint.kind in CLIENT_DECORATORS:
            if endpoint.function in exports:
                previous = exports[endpoint.function]
                raise BuildError(
                    f"Duplicate $python export {endpoint.function!r} in "
                    f"{previous.file} and {endpoint.file}."
                )
            exports[endpoint.function] = endpoint
    return sorted(
        endpoints, key=lambda item: (item.endpoint, item.method, item.function)
    )


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def generate_backend_manifest(endpoints: list[EndpointInfo]) -> str:
    lines = [
        '"""Generated by SyKit. Do not edit."""',
        "",
        "from importlib import import_module",
        "",
        "",
        "def _load(module_name, function_name):",
        "    module = import_module(module_name)",
        "    return getattr(module, function_name)",
        "",
        "",
        "ENDPOINTS = [",
    ]
    for endpoint in endpoints:
        metadata = {
            "kind": endpoint.kind,
            "method": endpoint.method,
            "endpoint": endpoint.endpoint,
            "name": endpoint.function,
            "module": endpoint.module,
            "file": endpoint.file,
            "is_async": endpoint.is_async,
            "parameters": [asdict(parameter) for parameter in endpoint.parameters],
            "permissions": endpoint.permissions,
            "cors": list(endpoint.cors or ()),
            "limits": endpoint.limits,
            "hidden": endpoint.hidden,
            "token": endpoint.token,
            "api_key": endpoint.api_key,
            "max_upload_bytes": endpoint.max_upload_bytes,
        }
        python_metadata = repr(metadata)
        lines.extend(
            [
                "    {",
                f'        "metadata": {python_metadata},',
                f'        "function": _load({_json_dump(endpoint.module)}, {_json_dump(endpoint.function)}),',
                "    },",
            ]
        )
    lines.extend(
        [
            "]",
            "",
        ]
    )
    return "\n".join(lines)


def generate_task_manifest(tasks: list[TaskInfo]) -> str:
    lines = [
        '"""Generated by SyKit. Do not edit."""',
        "",
        "from importlib import import_module",
        "",
        "",
        "def _load(module_name, function_name):",
        "    module = import_module(module_name)",
        "    return getattr(module, function_name)",
        "",
        "",
        "TASKS = [",
    ]
    for task_info in tasks:
        metadata = {
            "id": task_info.task_id,
            "name": task_info.function,
            "module": task_info.module,
            "file": task_info.file,
            "is_async": task_info.is_async,
            "schedule": task_info.schedule,
        }
        lines.extend(
            [
                "    {",
                f'        "metadata": {metadata!r},',
                f'        "function": _load({_json_dump(task_info.module)}, {_json_dump(task_info.function)}),',
                "    },",
            ]
        )
    lines.extend(["]", ""])
    return "\n".join(lines)


def _js_object_expression(parameters: tuple[ParameterInfo, ...]) -> str:
    if not parameters:
        return "{}"
    names = ", ".join(parameter.name for parameter in parameters)
    return f"$sykitCompact({{{names}}})"


def generate_client_module(
    config: dict[str, Any], endpoints: list[EndpointInfo]
) -> str:
    prefix = normalize_prefix(config.get("endpoints", "/api/"))
    for endpoint in endpoints:
        if endpoint.kind in CLIENT_DECORATORS and (
            endpoint.function in JS_RESERVED_WORDS
            or endpoint.function in CLIENT_RESERVED_EXPORTS
        ):
            raise BuildError(
                f"Cannot export reserved $python name {endpoint.function!r}."
            )
    lines = [
        "// Generated by SyKit. Do not edit.",
        "const $sykitGlobal = globalThis;",
        f"const $sykitApiPrefix = {_json_dump(prefix)};",
        "",
        "export class SyKitError extends $sykitGlobal.Error {",
        "  constructor(message, status, details) {",
        "    super(message);",
        '    this.name = "SyKitError";',
        "    this.status = status;",
        "    this.details = details;",
        "  }",
        "}",
        "",
        "function $sykitCompact(values) {",
        "  return $sykitGlobal.Object.fromEntries($sykitGlobal.Object.entries(values).filter(([, value]) => value !== void 0));",
        "}",
        "",
        "function $sykitEndpointUrl(endpoint) {",
        "  return `${$sykitApiPrefix}${endpoint}`;",
        "}",
        "",
        "async function $sykitDecodeResponse(response) {",
        "  const text = await response.text();",
        "  let data = null;",
        "  if (text) {",
        "    try { data = $sykitGlobal.JSON.parse(text); } catch { data = text; }",
        "  }",
        "  if (!response.ok) {",
        "    const message = data?.error || data?.detail || `SyKit request failed (${response.status})`;",
        "    throw new SyKitError(message, response.status, data);",
        "  }",
        "  return data;",
        "}",
        "",
        "async function $sykitPost(endpoint, values) {",
        "  const response = await $sykitGlobal.fetch($sykitEndpointUrl(endpoint), {",
        '    method: "POST",',
        '    headers: { "Content-Type": "application/json" },',
        '    credentials: "include",',
        "    body: $sykitGlobal.JSON.stringify(values),",
        "  });",
        "  return $sykitDecodeResponse(response);",
        "}",
        "",
        "async function $sykitPostMultipart(endpoint, values, uploadNames) {",
        "  const uploads = new $sykitGlobal.Set(uploadNames);",
        "  const body = new $sykitGlobal.FormData();",
        "  for (const [name, value] of $sykitGlobal.Object.entries(values)) {",
        "    if (uploads.has(name)) {",
        '      if (typeof $sykitGlobal.Blob !== "function" || !(value instanceof $sykitGlobal.Blob)) {',
        "        throw new $sykitGlobal.TypeError(`SyKit upload parameter ${name} must be a File or Blob.`);",
        "      }",
        "      body.append(name, value);",
        "      continue;",
        "    }",
        "    const encoded = $sykitGlobal.JSON.stringify(value);",
        "    if (encoded === void 0) throw new $sykitGlobal.TypeError(`SyKit parameter ${name} is not JSON serializable.`);",
        "    body.append(name, encoded);",
        "  }",
        "  const response = await $sykitGlobal.fetch($sykitEndpointUrl(endpoint), {",
        '    method: "POST",',
        '    credentials: "include",',
        "    body,",
        "  });",
        "  return $sykitDecodeResponse(response);",
        "}",
        "",
        "async function $sykitGet(endpoint, values) {",
        "  const query = new $sykitGlobal.URLSearchParams();",
        "  for (const [name, value] of $sykitGlobal.Object.entries(values)) query.set(name, $sykitGlobal.JSON.stringify(value));",
        '  const suffix = query.size ? `?${query}` : "";',
        '  const response = await $sykitGlobal.fetch(`${$sykitEndpointUrl(endpoint)}${suffix}`, { credentials: "include" });',
        "  return $sykitDecodeResponse(response);",
        "}",
        "",
    ]
    has_hidden = any(
        endpoint.hidden and endpoint.kind in CLIENT_DECORATORS for endpoint in endpoints
    )
    if has_hidden:
        lines.extend(
            [
                "let $sykitHiddenPromise = null;",
                "",
                "function hidden_api() {",
                '  throw new SyKitError("Endpoint not found.", 404, { error: "Endpoint not found." });',
                "}",
                "",
                "async function $sykitHiddenManifest() {",
                "  try {",
                f"    const data = await $sykitPost({_json_dump(HIDDEN_MANIFEST_ENDPOINT)}, {{}});",
                '    if (data && typeof data === "object" && !$sykitGlobal.Array.isArray(data)) return data;',
                "  } catch {}",
                "  return {};",
                "}",
                "",
                "async function $sykitHiddenCall(token, values) {",
                "  let map = $sykitHiddenPromise ? await $sykitHiddenPromise : null;",
                "  if (!map || !map[token]) {",
                "    $sykitHiddenPromise = $sykitHiddenManifest();",
                "    map = await $sykitHiddenPromise;",
                "  }",
                "  const record = map[token];",
                "  if (!record) return hidden_api();",
                "  const named = {};",
                "  (record.p || []).forEach((name, index) => { named[name] = values[index]; });",
                "  const compact = $sykitCompact(named);",
                '  if (record.m === "GET") return $sykitGet(record.e, compact);',
                "  return record.u?.length ? $sykitPostMultipart(record.e, compact, record.u) : $sykitPost(record.e, compact);",
                "}",
                "",
            ]
        )
    for endpoint in endpoints:
        if endpoint.kind not in CLIENT_DECORATORS:
            continue
        parameters = endpoint.client_parameters
        signature = ", ".join(parameter.name for parameter in parameters)
        if endpoint.hidden:
            if not endpoint.token:
                raise BuildError(
                    f"Hidden endpoint {endpoint.function!r} has no client token."
                )
            arguments = "[" + ", ".join(p.name for p in parameters) + "]"
            lines.extend(
                [
                    f"export async function {endpoint.function}({signature}) {{",
                    f"  return $sykitHiddenCall({_json_dump(endpoint.token)}, {arguments});",
                    "}",
                    "",
                ]
            )
            continue
        values = _js_object_expression(parameters)
        uploads = [parameter.name for parameter in parameters if parameter.upload]
        if uploads:
            call = (
                f"$sykitPostMultipart({_json_dump(endpoint.endpoint)}, {values}, "
                f"{_json_dump(uploads)})"
            )
        else:
            helper = "$sykitPost" if endpoint.kind == "expose" else "$sykitGet"
            call = f"{helper}({_json_dump(endpoint.endpoint)}, {values})"
        lines.extend(
            [
                f"export async function {endpoint.function}({signature}) {{",
                f"  return {call};",
                "}",
                "",
            ]
        )
    return "\n".join(lines)


def normalize_prefix(value: Any) -> str:
    if not isinstance(value, str):
        raise BuildError('The "endpoints" configuration value must be a string.')
    prefix = "/" + value.strip().strip("/")
    if prefix == "/":
        raise BuildError('The "endpoints" prefix cannot be the site root.')
    segments = prefix.strip("/").split("/")
    if (
        any(character in prefix for character in "?#{}\\")
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in prefix
        )
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise BuildError(f"Invalid endpoint prefix {value!r}.")
    return prefix + "/"


def normalize_health_path(
    value: Any,
    name: str,
    endpoint_prefix: str,
    *,
    optional: bool = False,
) -> str:
    if not isinstance(value, str):
        raise BuildError(f'The "{name}" configuration value must be a string.')
    text = value.strip()
    if optional and not text:
        return ""
    path = "/" + text.strip("/")
    segments = path.strip("/").split("/")
    if (
        path == "/"
        or not path.isascii()
        or any(character in path for character in "?#{}\\")
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in path
        )
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise BuildError(f'Invalid "{name}" path {value!r}.')
    endpoint_root = endpoint_prefix.rstrip("/")
    if path == endpoint_root or path.startswith(endpoint_prefix):
        raise BuildError(f'The "{name}" path must stay outside the endpoint prefix.')
    return path


def _safe_remove(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    resolved = path.resolve()
    root = Path.cwd().resolve()
    if resolved.parent != root:
        raise BuildError(f"Refusing to remove unexpected path: {resolved}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_frontend_sources(source: Path, destination: Path, sykit_dir: Path) -> None:
    if destination.exists() or destination.is_symlink():
        cache_root = CACHE_DIR.resolve()
        resolved = destination.resolve()
        if resolved != cache_root and cache_root not in resolved.parents:
            raise BuildError(f"Refusing to replace unexpected cache path: {resolved}")
        if destination.is_symlink():
            destination.unlink()
        else:
            shutil.rmtree(destination)
    internal_relative = sykit_dir.relative_to(source)

    def ignore(directory: str, names: list[str]) -> set[str]:
        directory_path = Path(directory)
        ignored = {name for name in names if name in IGNORED_SOURCE_DIRS}
        try:
            relative = directory_path.relative_to(source)
        except ValueError:
            return ignored
        if relative == internal_relative.parent and internal_relative.name in names:
            ignored.add(internal_relative.name)
        ignored.update(
            name for name in names if name.endswith((".py", ".pyc", ".pyo", ".pyi"))
        )
        return ignored

    shutil.copytree(source, destination, ignore=ignore)


def _write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _vite_config() -> str:
    return """import path from \"node:path\";
import { fileURLToPath } from \"node:url\";
import { defineConfig } from \"vite\";
import { svelte } from \"@sveltejs/vite-plugin-svelte\";

const here = path.dirname(fileURLToPath(import.meta.url));
const outDir = process.env.SYKIT_OUT_DIR;
if (!outDir) throw new Error("SYKIT_OUT_DIR was not provided");

export default defineConfig({
  root: path.join(here, "frontend"),
  plugins: [svelte()],
  resolve: {
    alias: {
      $python: path.join(here, "generated", "endpoints.mjs"),
    },
  },
  cacheDir: path.join(here, "node_modules", ".vite"),
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
  },
});
"""


def _npm_command() -> str:
    if os.name == "nt":
        command = shutil.which("npm.cmd")
    else:
        command = shutil.which("npm")
    if not command:
        raise BuildError("npm was not found on PATH.")
    return command


def prepare_frontend_cache(
    config: dict[str, Any],
    sykit_dir: Path,
    client_module: str,
) -> None:
    cache_enabled = bool(config.get("cache-svelte", True))
    if not cache_enabled:
        _safe_remove(CACHE_DIR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    default_manifest, default_dependencies = _load_frontend_manifest()
    dependencies = _frontend_dependencies(config)
    package = {**default_manifest, "dependencies": dependencies}
    package_content = json.dumps(package, indent=2) + "\n"
    package_changed = _write_if_changed(CACHE_DIR / "package.json", package_content)
    _write_if_changed(CACHE_DIR / "vite.config.mjs", _vite_config())
    _write_if_changed(CACHE_DIR / "generated" / "endpoints.mjs", client_module)
    _copy_frontend_sources(SRC_DIR, CACHE_DIR / "frontend", sykit_dir)
    if not any(
        (CACHE_DIR / "frontend" / name).is_file()
        for name in ("svelte.config.js", "svelte.config.mjs", "svelte.config.cjs")
    ):
        _write_if_changed(
            CACHE_DIR / "frontend" / "svelte.config.js",
            "export default {};\n",
        )

    if not (CACHE_DIR / "frontend" / "index.html").is_file():
        raise BuildError(
            "src/index.html was not found. Run `init` for the starter or add a Vite SPA entry."
        )

    node_modules = CACHE_DIR / "node_modules"
    lockfile = CACHE_DIR / "package-lock.json"
    uses_default_lock = dependencies == default_dependencies
    lock_changed = False
    if uses_default_lock:
        try:
            lock_content = FRONTEND_LOCK_PATH.read_text(encoding="utf-8")
        except OSError as error:
            raise BuildError(
                f"Could not read frontend lockfile {FRONTEND_LOCK_PATH}: {error}"
            ) from error
        lock_changed = _write_if_changed(lockfile, lock_content)

    if (
        package_changed
        or lock_changed
        or not node_modules.is_dir()
        or not lockfile.is_file()
    ):
        print("Installing Svelte build dependencies...")
        install_command = "ci" if uses_default_lock else "install"
        result = subprocess.run(
            [
                _npm_command(),
                install_command,
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ],
            cwd=CACHE_DIR,
            check=False,
        )
        if result.returncode != 0:
            raise BuildError(f"npm {install_command} failed.")

    check_requirements(cache_dir=CACHE_DIR, include_svelte=True)


def _copy_python_sources(destination: Path, sykit_dir: Path) -> None:
    for source in list_python_files(SRC_DIR, sykit_dir):
        target = destination / source.relative_to(SRC_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    runtime_package = destination / "sykit"
    runtime_package.mkdir(parents=True, exist_ok=True)
    for source in (TOOL_DIR / "sykit").glob("*.py"):
        shutil.copy2(source, runtime_package / source.name)


DOCKERIGNORE = """\
__pycache__/
*.pyc
.sykit-limits.sqlite3
.sykit-sessions.sqlite3
.sykit-tasks.sqlite3
"""


def generate_dockerfile(port: int) -> str:
    return (
        "# Generated by SyKit. Do not edit; regenerated on every build.\n"
        "FROM python:3.12-slim\n"
        "WORKDIR /app\n"
        "COPY . .\n"
        "RUN python -m pip install --no-cache-dir -r requirements.txt\n"
        f"EXPOSE {port}\n"
        'CMD ["python", "main.py"]\n'
    )


def generate_compose(port: int, health_path: str = "/healthz") -> str:
    health_url = f"http://127.0.0.1:{port}{health_path}"
    health_command = (
        f"import urllib.request; urllib.request.urlopen({health_url!r}, timeout=2)"
    )
    return (
        "# Generated by SyKit. Do not edit; regenerated on every build.\n"
        "services:\n"
        "  app:\n"
        "    build: .\n"
        "    ports:\n"
        f'      - "{port}:{port}"\n'
        "    environment:\n"
        "      SYKIT_SESSION_SECRET: "
        "${SYKIT_SESSION_SECRET:?set a long random value}\n"
        "    healthcheck:\n"
        f"      test: {json.dumps(['CMD', 'python', '-c', health_command])}\n"
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 3\n"
        "      start_period: 5s\n"
        "    stop_grace_period: 1m\n"
        "    restart: unless-stopped\n"
    )


def write_docker_files(
    port: int,
    host: str,
    health_path: str = "/healthz",
) -> None:
    (STAGING_DIR / "Dockerfile").write_text(generate_dockerfile(port), encoding="utf-8")
    (STAGING_DIR / "compose.yaml").write_text(
        generate_compose(port, health_path),
        encoding="utf-8",
    )
    (STAGING_DIR / ".dockerignore").write_text(DOCKERIGNORE, encoding="utf-8")
    stripped = host.strip()
    if stripped in {"127.0.0.1", "localhost", "::1"}:
        print(
            'Warning: "docker" is enabled but "host-ip" is a loopback '
            f'address ({stripped}); set it to "0.0.0.0" so the app is '
            "reachable from outside the container."
        )
    print("Docker files written: Dockerfile, compose.yaml, .dockerignore")


DOTENV_MAIN_PY = '''\
"""Generated by SyKit. Do not edit."""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from server import app, run  # noqa: E402,F401

if __name__ == "__main__":
    run()
'''


def _check_dotenv_installed() -> None:
    if importlib.util.find_spec("dotenv") is None:
        raise BuildError(
            '"use-dotenv" is enabled but the python-dotenv package is not '
            "installed. Run: pip install python-dotenv"
        )


def _ensure_env_files() -> None:
    if not ENV_PATH.exists():
        if not ENV_EXAMPLE_PATH.exists():
            shutil.copy2(SOURCE_FILES_DIR / ".env.example", ENV_EXAMPLE_PATH)
            print(f"Created {ENV_EXAMPLE_PATH}.")
        shutil.copy2(ENV_EXAMPLE_PATH, ENV_PATH)
        if os.name == "posix":
            os.chmod(ENV_PATH, 0o600)
        print(f"Created {ENV_PATH} from {ENV_EXAMPLE_PATH}.")
    _ensure_gitignore_env()


def _ensure_gitignore_env() -> None:
    try:
        existing = (
            GITIGNORE_PATH.read_text(encoding="utf-8")
            if GITIGNORE_PATH.is_file()
            else ""
        )
    except OSError:
        return
    entries = {line.strip() for line in existing.splitlines()}
    if entries & {".env", "/.env", ".env*", "*.env"}:
        return
    with GITIGNORE_PATH.open("a", encoding="utf-8") as file:
        if existing and not existing.endswith("\n"):
            file.write("\n")
        file.write(".env\n")
    print("Added .env to .gitignore.")


def _dotenv_provides_secret() -> bool:
    try:
        content = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in content.splitlines():
        name, _, value = line.strip().partition("=")
        if name.strip() == "SYKIT_SESSION_SECRET" and value.strip():
            return True
    return False


def _run_dev_server(use_dotenv: bool) -> bool:
    environment = os.environ.copy()
    has_secret = bool(environment.get("SYKIT_SESSION_SECRET")) or (
        use_dotenv and _dotenv_provides_secret()
    )
    if not has_secret:
        environment["SYKIT_SESSION_SECRET"] = secrets.token_urlsafe(48)
        print(
            "SYKIT_SESSION_SECRET is not set; using a temporary secret for "
            "this dev run."
        )
    print("Starting the built app (Ctrl+C to stop)...")
    try:
        completed = subprocess.run(
            [sys.executable, str((BUILT_DIR / "main.py").resolve())],
            check=False,
            env=environment,
        )
    except KeyboardInterrupt:
        return True
    return completed.returncode == 0


def prepare_staging(
    config_path: Path,
    backend_manifest: str,
    task_manifest: str,
    client_module: str,
    use_dotenv: bool,
) -> None:
    _safe_remove(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True)
    (STAGING_DIR / "core").mkdir()
    (STAGING_DIR / "app").mkdir()

    if use_dotenv:
        (STAGING_DIR / "main.py").write_text(DOTENV_MAIN_PY, encoding="utf-8")
    else:
        shutil.copy2(SOURCE_FILES_DIR / "main.py", STAGING_DIR / "main.py")
    shutil.copy2(SOURCE_FILES_DIR / "server.py", STAGING_DIR / "server.py")
    license_path = TOOL_DIR / "LICENSE"
    if license_path.is_file():
        shutil.copy2(license_path, STAGING_DIR / "SYKIT-LICENSE")
    shutil.copy2(TOOL_DIR / "requirements.txt", STAGING_DIR / "requirements.txt")
    shutil.copy2(config_path, STAGING_DIR / "config.json")
    # Copy every core runtime module so packages can ship extra core files
    # (session stores, for example) without editing the build. The generated
    # _endpoints.py below overwrites the placeholder.
    for source in sorted((SOURCE_FILES_DIR / "core").glob("*.py")):
        shutil.copy2(source, STAGING_DIR / "core" / source.name)
    (STAGING_DIR / "core" / "_endpoints.py").write_text(
        backend_manifest, encoding="utf-8"
    )
    (STAGING_DIR / "core" / "_tasks.py").write_text(task_manifest, encoding="utf-8")
    (STAGING_DIR / "core" / "endpoints.mjs").write_text(client_module, encoding="utf-8")
    _copy_python_sources(STAGING_DIR / "app", config_path.parent)


def compile_frontend() -> None:
    environment = os.environ.copy()
    environment["SYKIT_OUT_DIR"] = str((STAGING_DIR / "static").resolve())
    result = subprocess.run(
        [_npm_command(), "run", "build"],
        cwd=CACHE_DIR,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise BuildError("Svelte/Vite compilation failed.")


def publish_staging() -> None:
    _safe_remove(BACKUP_DIR)
    if BUILT_DIR.exists():
        BUILT_DIR.rename(BACKUP_DIR)
    try:
        STAGING_DIR.rename(BUILT_DIR)
    except Exception:
        if BACKUP_DIR.exists() and not BUILT_DIR.exists():
            BACKUP_DIR.rename(BUILT_DIR)
        raise
    _safe_remove(BACKUP_DIR)


def run(dev: bool = False) -> bool:
    try:
        if not SRC_DIR.is_dir():
            raise BuildError("src directory not found. Run `init` first.")
        sykit_dir = find_sykit_dir(SRC_DIR)
        if sykit_dir is None:
            raise BuildError(
                "sykit/config.json was not found under src. Run `init` first."
            )
        config_path = sykit_dir / "config.json"
        config = load_config(config_path)
        endpoint_prefix = normalize_prefix(config.get("endpoints", "/api/"))
        health_path = normalize_health_path(
            config.get("health-path", "/healthz"),
            "health-path",
            endpoint_prefix,
        )
        readiness_path = normalize_health_path(
            config.get("readiness-path", ""),
            "readiness-path",
            endpoint_prefix,
            optional=True,
        )
        if readiness_path == health_path:
            raise BuildError(
                'The "health-path" and "readiness-path" settings must differ.'
            )
        port = config.get("host-port", 8000)
        workers = config.get("workers", 1)
        task_concurrency = config.get("task-concurrency", 1)
        max_request_bytes = config.get("max-request-bytes", 1_048_576)
        session_max_age = config.get("session-max-age", 1209600)
        for name, value in (
            ("host-port", port),
            ("workers", workers),
            ("task-concurrency", task_concurrency),
            ("max-request-bytes", max_request_bytes),
            ("session-max-age", session_max_age),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise BuildError(f'"{name}" must be an integer.')
        if workers < 1:
            raise BuildError('"workers" must be at least 1.')
        if task_concurrency < 1:
            raise BuildError('"task-concurrency" must be at least 1.')
        if not 1 <= port <= 65535:
            raise BuildError('"host-port" must be between 1 and 65535.')
        if max_request_bytes < 1:
            raise BuildError('"max-request-bytes" must be at least 1.')
        if session_max_age < 1:
            raise BuildError('"session-max-age" must be at least 1.')
        host = config.get("host-ip", "127.0.0.1")
        if not isinstance(host, str) or not host.strip():
            raise BuildError('"host-ip" must be a non-empty string.')
        for name in ("cache-svelte", "docker", "session-https-only", "use-dotenv"):
            if name in config and not isinstance(config[name], bool):
                raise BuildError(f'"{name}" must be true or false.')
        if "content-security-policy" in config and not isinstance(
            config["content-security-policy"], str
        ):
            raise BuildError('"content-security-policy" must be a string.')
        if "task-store" in config and not isinstance(config["task-store"], str):
            raise BuildError('"task-store" must be a string.')
        for name, default, choices in (
            ("log-format", "text", {"text", "json"}),
            (
                "log-level",
                "INFO",
                {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
            ),
        ):
            value = config.get(name, default)
            if not isinstance(value, str):
                expected = ", ".join(sorted(choice.lower() for choice in choices))
                raise BuildError(f'"{name}" must be one of: {expected}.')
            normalized = value.lower() if name == "log-format" else value.upper()
            if normalized not in choices:
                expected = ", ".join(sorted(choice.lower() for choice in choices))
                raise BuildError(f'"{name}" must be one of: {expected}.')
        use_dotenv = bool(config.get("use-dotenv", False))
        if use_dotenv:
            _check_dotenv_installed()
        _validate_allowed_hosts(
            config.get("allowed-hosts", ["127.0.0.1", "localhost", "::1"]),
            config_path,
        )
        check_requirements()
        python_files = list_python_files(SRC_DIR, sykit_dir)
        validate_module_roots(python_files)
        endpoints = detect_endpoints(python_files)
        tasks = detect_tasks(python_files)
        endpoints = apply_endpoint_defaults(config, endpoints, config_path)
        validate_hidden_endpoints(endpoints)
        endpoints = assign_hidden_tokens(endpoints)
        backend_manifest = generate_backend_manifest(endpoints)
        task_manifest = generate_task_manifest(tasks)
        client_module = generate_client_module(config, endpoints)

        prepare_frontend_cache(config, sykit_dir, client_module)
        prepare_staging(
            config_path,
            backend_manifest,
            task_manifest,
            client_module,
            use_dotenv,
        )
        if bool(config.get("docker", False)):
            write_docker_files(port, host, health_path)
        compile_frontend()
        publish_staging()
        if not bool(config.get("cache-svelte", True)):
            _safe_remove(CACHE_DIR)
        if use_dotenv:
            _ensure_env_files()
        print(f"Build complete: {BUILT_DIR.resolve()}")
        if dev:
            return _run_dev_server(use_dotenv)
        return True
    except (BuildError, RequirementError, OSError) as error:
        message = str(error)
        filename = getattr(error, "filename", None)
        if filename and str(filename) not in message:
            message += f" ({filename})"
        print(f"Build failed: {message}", file=sys.stderr)
        _safe_remove(STAGING_DIR)
        return False


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
