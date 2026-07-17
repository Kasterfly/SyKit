from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent
SOURCE_SYKIT = TOOL_DIR / "sykit"
SOURCE_CONFIG_PATH = SOURCE_SYKIT / "config.json"
STARTER_DIR = TOOL_DIR / "files" / "frontend"
SRC_PATH = Path("src")


class InitError(RuntimeError):
    """A user-facing initialization failure."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant {value!r}.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        value[key] = item
    return value


def load_source_config() -> dict[str, Any]:
    try:
        with SOURCE_CONFIG_PATH.open("r", encoding="utf-8") as file:
            value = json.load(
                file,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise InitError(f"Could not read {SOURCE_CONFIG_PATH}: {error}") from error
    if not isinstance(value, dict):
        raise InitError(f"{SOURCE_CONFIG_PATH} must contain a JSON object.")
    return value


def _destination(config: dict[str, Any]) -> Path:
    configured = config.get("sykit-folder-path", "")
    if not isinstance(configured, str):
        raise InitError('The "sykit-folder-path" setting must be a string.')
    parent = Path(configured.strip())
    if parent.is_absolute() or ".." in parent.parts:
        raise InitError(
            'The "sykit-folder-path" setting must stay inside the src directory.'
        )
    return SRC_PATH / parent / "sykit"


def _ensure_safe_destination(destination: Path) -> None:
    if SRC_PATH.is_symlink():
        raise InitError("Refusing to initialize through a symbolic src directory.")
    source_root = SRC_PATH.resolve()
    resolved = destination.resolve()
    if resolved != source_root and source_root not in resolved.parents:
        raise InitError(f"Refusing to initialize outside {source_root}: {resolved}")
    if destination.is_symlink():
        raise InitError(f"Refusing to overwrite symbolic path {destination}.")


def _copy_starter_file(name: str) -> bool:
    destination = SRC_PATH / name
    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STARTER_DIR / name, destination)
    return True


def run() -> bool:
    try:
        config = load_source_config()
        destination = _destination(config)

        if SRC_PATH.exists() and not SRC_PATH.is_dir():
            raise InitError(f"{SRC_PATH} exists but is not a directory.")
        SRC_PATH.mkdir(parents=True, exist_ok=True)
        _ensure_safe_destination(destination)

        if destination.is_file():
            raise InitError(f"{destination} exists but is not a directory.")
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ensure_safe_destination(destination)
            shutil.copytree(
                SOURCE_SYKIT,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
        else:
            retired_util = destination / "util.py"
            if retired_util.is_file():
                retired_util.unlink()
            for source in SOURCE_SYKIT.glob("*.py"):
                shutil.copy2(source, destination / source.name)

        created: list[str] = []
        if not (SRC_PATH / "index.html").exists():
            created = [
                name
                for name in (
                    "index.html",
                    "main.js",
                    "App.svelte",
                    "endpoints.py",
                    "svelte.config.js",
                )
                if _copy_starter_file(name)
            ]
        if created:
            print("Initialized SyKit and starter files: " + ", ".join(created))
        else:
            print(
                "Initialized SyKit configuration; existing application files "
                "were preserved."
            )
        return True
    except (InitError, OSError) as error:
        print(f"Initialization failed: {error}")
        return False


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
