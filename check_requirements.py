from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


class RequirementError(RuntimeError):
    pass


NODE_REQUIREMENT = "Node.js 22.12+ or 24.x"


def _find_windows_executable(
    command: str,
    directories: list[str],
    current_directory: Path,
    path_extensions: str,
) -> str | None:
    try:
        current = current_directory.resolve()
    except OSError:
        current = current_directory
    suffixes = ("",)
    if not Path(command).suffix:
        suffixes = tuple(suffix for suffix in path_extensions.split(";") if suffix)
    for raw_directory in directories:
        if not raw_directory:
            continue
        try:
            directory = Path(raw_directory).resolve()
        except OSError:
            continue
        if directory == current:
            continue
        for suffix in suffixes:
            candidate = directory / f"{command}{suffix}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def find_executable(command: str) -> str | None:
    """Resolve a PATH command without Windows' implicit current directory."""
    if os.name != "nt":
        return shutil.which(command)
    return _find_windows_executable(
        command,
        os.get_exec_path(),
        Path.cwd(),
        os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
    )


def parse_version(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+|\d+)", value)
    if not match:
        raise RequirementError(f"Could not parse version from {value!r}.")
    return tuple(int(part) for part in match.group(1).split("."))


def _node_version() -> str:
    executable = find_executable("node")
    if not executable:
        raise RequirementError("Node.js was not found on PATH.")
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RequirementError("Node.js could not report its version.") from error
    if result.returncode != 0:
        raise RequirementError("Node.js could not report its version.")
    return result.stdout.strip()


def validate_node_version(value: str) -> tuple[int, int, int]:
    parsed = parse_version(value)
    version = (parsed + (0, 0, 0))[:3]
    major, minor, _patch = version
    supported = (major == 22 and minor >= 12) or major == 24
    if not supported:
        raise RequirementError(
            f"SyKit requires {NODE_REQUIREMENT}; found Node.js {value}."
        )
    return version


def _svelte_version(cache_dir: Path) -> str:
    package_path = cache_dir / "node_modules" / "svelte" / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        version = package["version"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RequirementError(
            f"Could not determine the installed Svelte version from {package_path}."
        ) from error
    return str(version)


def check_requirements(
    *,
    cache_dir: Path | None = None,
    include_svelte: bool = False,
) -> None:
    validate_node_version(_node_version())
    if include_svelte:
        if cache_dir is None:
            raise RequirementError("A cache directory is required to check Svelte.")
        installed = _svelte_version(cache_dir)
        if parse_version(installed)[0] != 5:
            raise RequirementError(
                f"Svelte {installed} is installed, but SyKit requires Svelte 5.x "
                "for runes support."
            )


__all__ = [
    "RequirementError",
    "NODE_REQUIREMENT",
    "check_requirements",
    "find_executable",
    "parse_version",
    "validate_node_version",
]
