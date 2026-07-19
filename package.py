from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

TOOL_DIR = Path(__file__).resolve().parent
PACKAGES_DIR = TOOL_DIR / ".packages"
INDEX_PATH = PACKAGES_DIR / "index.json"
AUTHORS_PATH = PACKAGES_DIR / "authors.md"
MANIFEST_NAME = "SyKitPackage.json"
RECORD_NAME = "record.json"
SOURCE_COPY_NAME = "package"
BEFORE_NAME = "before"
AFTER_NAME = "after"
ROLLBACK_NAME = ".rollback"
ADD_DIR = "add"
EDIT_DIR = "edit"
REMOVE_DIR = "remove"
PACKAGE_ENTRIES = {MANIFEST_NAME, ADD_DIR, EDIT_DIR, REMOVE_DIR}
PROTECTED_ROOTS = frozenset({".git", ".packages", "__pycache__"})
IGNORED_COPY_PATTERNS = ("__pycache__", "*.pyc", "*.pyo")
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
RESERVED_PACKAGE_IDS = frozenset(
    {INDEX_PATH.name.casefold(), AUTHORS_PATH.name.casefold(), ROLLBACK_NAME.casefold()}
)
WINDOWS_DEVICE_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)
MANIFEST_KEYS = {"id", "name", "desc", "package-req", "credit", "sykit-req", "deps"}
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
DEP_MAX_LENGTH = 200
CHANGE_ACTIONS = {"add", "edit", "remove"}
EDIT_ACTIONS = {
    "replace-file",
    "append",
    "prepend",
    "insert-before",
    "insert-after",
    "replace",
}
ANCHOR_ACTIONS = {"insert-before", "insert-after", "replace"}
DEFAULT_PACKAGE_REPO = "Kasterfly/SyKit-Packages"
DEFAULT_MAX_DOWNLOAD_MB = 50
# Codepoints that can reorder or hide attacker-authored text in a terminal:
# bidi overrides, zero-width characters, and line/paragraph separators.
UNSAFE_DISPLAY_CODEPOINTS = frozenset(
    {
        0x200B,
        0x200C,
        0x200D,
        0x200E,
        0x200F,
        0x2028,
        0x2029,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2060,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
        0xFEFF,
    }
)


class PackageError(RuntimeError):
    """A user-facing package failure."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant {value!r}.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        value[key] = item
    return value


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(
                file,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise PackageError(f"Could not read {path}: {error}") from error


def _write_json(path: Path, value: Any) -> None:
    content = json.dumps(value, indent=4) + "\n"
    _write_bytes_atomic(path, content.encode("utf-8"))


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.unlink(missing_ok=True)
        with temporary.open("xb") as file:
            file.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _optional_file_state(path: Path) -> bytes | None:
    if path.is_file():
        return path.read_bytes()
    if path.exists() or path.is_symlink():
        raise PackageError(f"Expected {path} to be a file, but it is not.")
    return None


def _restore_file_state(path: Path, state: bytes | None) -> None:
    if state is None:
        path.unlink(missing_ok=True)
    else:
        _write_bytes_atomic(path, state)


def _is_windows_reserved_component(value: str) -> bool:
    stem = value.rstrip(" .").split(".", 1)[0].casefold()
    return stem in WINDOWS_DEVICE_NAMES


def _validate_package_id(value: Any, origin: str) -> str:
    valid = (
        isinstance(value, str)
        and ID_PATTERN.fullmatch(value) is not None
        and not value.endswith(".")
        and value.casefold() not in RESERVED_PACKAGE_IDS
        and not _is_windows_reserved_component(value)
    )
    if not valid:
        raise PackageError(
            f"{origin}: package ids must start with a letter or digit, use only "
            'letters, digits, ".", "_" or "-", and must not use a reserved name.'
        )
    return value


@dataclass
class Manifest:
    id: str
    name: str
    desc: str
    requires: tuple[str, ...]
    credit: tuple[str, ...]
    sykit_req: str
    deps: tuple[str, ...]


@dataclass
class Change:
    target: str
    action: str
    before: bytes | None
    after: bytes | None


def _require_clean_text(value: str, origin: str) -> None:
    """Reject terminal control characters (C0, DEL, C1) in printed metadata."""
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
        raise PackageError(f"{origin} may not contain control characters.")


def _sanitize_text(value: str) -> str:
    """Neutralize characters that could forge or reorder terminal output.

    Analyzer output can embed attacker-authored content (URLs, file payloads),
    so control characters, bidi overrides, and zero-width characters are
    replaced before printing.
    """
    return "".join(
        "?"
        if (
            ord(character) < 32
            or 127 <= ord(character) <= 159
            or ord(character) in UNSAFE_DISPLAY_CODEPOINTS
        )
        else character
        for character in value
    )


def _print_lines(lines: list[str]) -> None:
    for line in lines:
        print(_sanitize_text(line))


def _load_manifest(package_dir: Path) -> Manifest:
    manifest_path = package_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise PackageError(f"{package_dir} does not contain {MANIFEST_NAME}.")
    value = _load_json(manifest_path)
    if not isinstance(value, dict):
        raise PackageError(f"{manifest_path} must contain a JSON object.")
    unknown = sorted(set(value) - MANIFEST_KEYS)
    if unknown:
        raise PackageError(f"{manifest_path} has unknown keys: {', '.join(unknown)}.")
    package_id = _validate_package_id(value.get("id"), f'{manifest_path}: "id"')
    name = value.get("name", package_id)
    desc = value.get("desc", "")
    if not isinstance(name, str) or not isinstance(desc, str):
        raise PackageError(f'{manifest_path}: "name" and "desc" must be strings.')
    _require_clean_text(name, f'{manifest_path}: "name"')
    _require_clean_text(desc, f'{manifest_path}: "desc"')
    requires = value.get("package-req", [])
    if not isinstance(requires, list):
        raise PackageError(
            f'{manifest_path}: "package-req" must be a list of package ids.'
        )
    try:
        requires = [
            _validate_package_id(entry, f'{manifest_path}: "package-req"')
            for entry in requires
        ]
    except PackageError as error:
        raise PackageError(str(error)) from error
    folded_requirements = [entry.casefold() for entry in requires]
    if len(set(folded_requirements)) != len(folded_requirements):
        raise PackageError(
            f'{manifest_path}: "package-req" may not contain duplicate ids.'
        )
    if package_id.casefold() in folded_requirements:
        raise PackageError(f"{manifest_path}: a package may not require itself.")
    credit = value.get("credit", [])
    if isinstance(credit, str):
        credit = [credit]
    if not isinstance(credit, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in credit
    ):
        raise PackageError(
            f'{manifest_path}: "credit" must be a string or a list of '
            "non-empty strings."
        )
    for entry in credit:
        _require_clean_text(entry, f'{manifest_path}: "credit"')
    sykit_req = value.get("sykit-req", "")
    if not isinstance(sykit_req, str):
        raise PackageError(
            f'{manifest_path}: "sykit-req" must be a version string like "0.4.1".'
        )
    if sykit_req:
        _parse_version(sykit_req, f'{manifest_path}: "sykit-req"')
    deps = value.get("deps", [])
    if isinstance(deps, str):
        deps = [deps]
    if not isinstance(deps, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in deps
    ):
        raise PackageError(
            f'{manifest_path}: "deps" must be a string or a list of non-empty '
            "dependency strings."
        )
    deps = [entry.strip() for entry in deps]
    for entry in deps:
        if (
            len(entry) > DEP_MAX_LENGTH
            or not entry[0].isalnum()
            or not all(32 <= ord(character) <= 126 for character in entry)
        ):
            raise PackageError(
                f'{manifest_path}: "deps" entry {entry!r} must be a printable '
                f"ASCII requirement of at most {DEP_MAX_LENGTH} characters that "
                "starts with a letter or digit."
            )
    folded_deps = [entry.casefold() for entry in deps]
    if len(set(folded_deps)) != len(folded_deps):
        raise PackageError(f'{manifest_path}: "deps" may not contain duplicates.')
    return Manifest(
        package_id,
        name,
        desc,
        tuple(requires),
        tuple(entry.strip() for entry in credit),
        sykit_req,
        tuple(deps),
    )


def _parse_version(value: Any, origin: str) -> tuple[int, ...]:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        raise PackageError(f'{origin}: expected a version like "0.4.1", not {value!r}.')
    return tuple(int(part) for part in value.split("."))


def _current_sykit_version() -> str:
    try:
        from sykit import __version__
    except ImportError as error:
        raise PackageError(
            "Could not determine the SyKit version (sykit/__init__.py is "
            f"missing or broken): {error}"
        ) from error
    return __version__


def _check_sykit_requirement(manifest: Manifest) -> None:
    if not manifest.sykit_req:
        return
    current = _current_sykit_version()
    required = _parse_version(manifest.sykit_req, f"package '{manifest.id}'")
    installed = _parse_version(current, "sykit/__init__.py")
    if installed < required:
        raise PackageError(
            f"Package '{manifest.id}' requires SyKit {manifest.sykit_req} or "
            f"newer; this SyKit is {current}. Update SyKit first."
        )


def _normalize_target(raw: Any, origin: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PackageError(f"{origin}: target paths must be non-empty strings.")
    if raw != raw.strip():
        raise PackageError(
            f"{origin}: target path {raw!r} may not begin or end with whitespace."
        )
    text = raw.replace("\\", "/")
    if text.startswith("/"):
        raise PackageError(
            f"{origin}: target path {raw!r} must be relative to the SyKit folder."
        )
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise PackageError(f"{origin}: target path {raw!r} names no file.")
    for part in parts:
        if (
            part == ".."
            or ":" in part
            or part != part.rstrip(" .")
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            or _is_windows_reserved_component(part)
        ):
            raise PackageError(
                f"{origin}: target path {raw!r} must stay inside the SyKit folder."
            )
    if parts[0].casefold() in PROTECTED_ROOTS:
        raise PackageError(f"{origin}: target path {raw!r} may not touch {parts[0]}/.")
    return "/".join(parts)


def _target_path(target: str) -> Path:
    path = TOOL_DIR / PurePosixPath(target)
    resolved = path.resolve()
    tool_root = TOOL_DIR.resolve()
    if resolved != tool_root and tool_root not in resolved.parents:
        raise PackageError(f"Target path escapes the SyKit folder: {target}")
    relative = resolved.relative_to(tool_root)
    if relative.parts and relative.parts[0].casefold() in PROTECTED_ROOTS:
        raise PackageError(f"Target path touches a protected SyKit folder: {target}")
    return path


def _is_ignored(path: Path) -> bool:
    return any(part.casefold() == "__pycache__" for part in path.parts) or (
        path.suffix.casefold() in {".pyc", ".pyo"}
    )


def _package_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if _is_ignored(path):
            continue
        if path.is_symlink():
            raise PackageError(
                f"{path} is a symbolic link; packages may not contain links."
            )
        if path.is_file():
            files.append(path)
    return files


def _hash_package_folder(package_dir: Path) -> str:
    """Content hash of a package folder (paths and bytes, order-independent)."""
    digest = hashlib.sha256()
    for path in _package_files(package_dir):
        relative = path.relative_to(package_dir).as_posix()
        data = path.read_bytes()
        digest.update(f"{relative}\x00{len(data)}\x00".encode("utf-8"))
        digest.update(data)
    return f"sha256:{digest.hexdigest()}"


def _ensure_package_store() -> None:
    tool_root = TOOL_DIR.resolve()
    expected = tool_root / PACKAGES_DIR.name
    resolved = PACKAGES_DIR.resolve()
    if (
        PACKAGES_DIR.is_symlink()
        or resolved != expected
        or (PACKAGES_DIR.exists() and not PACKAGES_DIR.is_dir())
    ):
        raise PackageError(
            f"Package state folder {PACKAGES_DIR} must be a real directory inside "
            "the SyKit folder."
        )


def _tool_settings() -> dict[str, Any]:
    """Optional package settings from the SyKit tool's own sykit/config.json.

    There is deliberately no setting that disables the pre-install analysis,
    the confirmation prompt, or the critical-finding gate.
    """
    config: dict[str, Any] = {}
    path = TOOL_DIR / "sykit" / "config.json"
    if path.is_file():
        value = _load_json(path)
        if isinstance(value, dict):
            config = value
    repo = config.get("package-default-repo", DEFAULT_PACKAGE_REPO)
    if not isinstance(repo, str) or not repo.strip():
        raise PackageError(
            'The "package-default-repo" setting must be a string like "Owner/Repo".'
        )
    cap = config.get("package-max-download-mb", DEFAULT_MAX_DOWNLOAD_MB)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise PackageError(
            'The "package-max-download-mb" setting must be a positive integer.'
        )
    return {
        "default-repo": repo.strip(),
        "max-download-bytes": cap * 1024 * 1024,
    }


def _decode(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PackageError(f"{label} is not UTF-8 text: {error}") from error


def _edit_operations(spec: Any, origin: str) -> list[dict[str, Any]]:
    if isinstance(spec, dict):
        spec = [spec]
    if not isinstance(spec, list) or not spec:
        raise PackageError(
            f"{origin} must contain an edit object or a non-empty list of them."
        )
    operations: list[dict[str, Any]] = []
    for entry in spec:
        if not isinstance(entry, dict):
            raise PackageError(f"{origin}: every edit must be a JSON object.")
        unknown = sorted(set(entry) - {"action", "anchor", "content"})
        if unknown:
            raise PackageError(f"{origin} has unknown keys: {', '.join(unknown)}.")
        action = entry.get("action")
        if action not in EDIT_ACTIONS:
            raise PackageError(
                f'{origin}: "action" must be one of {", ".join(sorted(EDIT_ACTIONS))}.'
            )
        anchor = entry.get("anchor")
        if action in ANCHOR_ACTIONS:
            if not isinstance(anchor, str) or not anchor:
                raise PackageError(
                    f'{origin}: "{action}" needs a non-empty "anchor" string.'
                )
        elif anchor is not None:
            raise PackageError(
                f'{origin}: "anchor" is only valid for '
                f"{', '.join(sorted(ANCHOR_ACTIONS))}."
            )
        content = entry.get("content")
        if content is not None and not isinstance(content, str):
            raise PackageError(f'{origin}: "content" must be a string.')
        operations.append({"action": action, "anchor": anchor, "content": content})
    return operations


def _apply_edit(
    before: bytes,
    payload: bytes,
    operations: list[dict[str, Any]],
    target: str,
    origin: str,
) -> bytes:
    state = before
    for position, operation in enumerate(operations, start=1):
        label = f"{origin} (edit {position})"
        action = operation["action"]
        inline = operation["content"]
        if action == "replace-file":
            state = payload if inline is None else inline.encode("utf-8")
            continue
        text = _decode(state, f"SyKit file {target}")
        content = inline if inline is not None else _decode(payload, origin)
        if action == "append":
            text = text + content
        elif action == "prepend":
            text = content + text
        else:
            anchor = operation["anchor"]
            index = text.find(anchor)
            if index < 0:
                preview = anchor if len(anchor) <= 60 else anchor[:57] + "..."
                raise PackageError(
                    f"{label}: anchor not found in {target}: {preview!r}"
                )
            if action == "insert-before":
                text = text[:index] + content + text[index:]
            elif action == "insert-after":
                end = index + len(anchor)
                text = text[:end] + content + text[end:]
            else:
                text = text[:index] + content + text[index + len(anchor) :]
        state = text.encode("utf-8")
    return state


def _check_package_layout(package_dir: Path) -> None:
    for entry in package_dir.iterdir():
        name = entry.name
        if name in PACKAGE_ENTRIES:
            if name != MANIFEST_NAME and not entry.is_dir():
                raise PackageError(f"{entry} must be a folder.")
            continue
        upper = name.upper()
        if name.startswith(".") or _is_ignored(entry):
            continue
        if upper.startswith("README") or upper.startswith("LICENSE"):
            continue
        raise PackageError(
            f"Unexpected entry {name!r} in {package_dir}; packages may only "
            f"contain {MANIFEST_NAME} plus {ADD_DIR}/, {EDIT_DIR}/ and "
            f"{REMOVE_DIR}/."
        )


def _plan_changes(package_dir: Path) -> list[Change]:
    _check_package_layout(package_dir)
    changes: dict[str, Change] = {}
    folded_targets: set[str] = set()

    def register(change: Change) -> None:
        folded = change.target.casefold()
        if folded in folded_targets:
            raise PackageError(
                f"Package changes {change.target} more than once (paths are "
                "compared ignoring case, because case-insensitive filesystems "
                "would apply them ambiguously)."
            )
        folded_targets.add(folded)
        changes[change.target] = change

    add_root = package_dir / ADD_DIR
    if add_root.is_dir():
        for source in _package_files(add_root):
            target = _normalize_target(
                source.relative_to(add_root).as_posix(), str(source)
            )
            destination = _target_path(target)
            if destination.exists():
                raise PackageError(
                    f"Cannot add {target}: it already exists in the SyKit folder."
                )
            register(Change(target, "add", None, source.read_bytes()))

    edit_root = package_dir / EDIT_DIR
    if edit_root.is_dir():
        files = _package_files(edit_root)
        available = set(files)
        companions = {
            path
            for path in files
            if path.suffix == ".json" and path.with_suffix("") in available
        }
        for source in files:
            if source in companions:
                continue
            target = _normalize_target(
                source.relative_to(edit_root).as_posix(), str(source)
            )
            destination = _target_path(target)
            if not destination.is_file():
                raise PackageError(
                    f"Cannot edit {target}: it does not exist in the SyKit folder."
                )
            companion = source.parent / (source.name + ".json")
            if companion in companions:
                operations = _edit_operations(_load_json(companion), str(companion))
            else:
                operations = [
                    {"action": "replace-file", "anchor": None, "content": None}
                ]
            before = destination.read_bytes()
            after = _apply_edit(
                before, source.read_bytes(), operations, target, str(source)
            )
            register(Change(target, "edit", before, after))

    remove_root = package_dir / REMOVE_DIR
    if remove_root.is_dir():
        for list_path in _package_files(remove_root):
            if list_path.suffix != ".json":
                raise PackageError(
                    f"{remove_root} may only contain .json path lists; "
                    f"found {list_path.name}."
                )
            value = _load_json(list_path)
            if not isinstance(value, list):
                raise PackageError(
                    f"{list_path} must contain a JSON list of SyKit paths."
                )
            for raw in value:
                target = _normalize_target(raw, str(list_path))
                destination = _target_path(target)
                if destination.is_dir():
                    raise PackageError(
                        f"Cannot remove directory {target}; list files individually."
                    )
                if not destination.is_file():
                    raise PackageError(
                        f"Cannot remove {target}: it does not exist in the "
                        "SyKit folder."
                    )
                register(Change(target, "remove", destination.read_bytes(), None))

    if not changes:
        raise PackageError(
            f"{package_dir} makes no changes ({ADD_DIR}/, {EDIT_DIR}/ and "
            f"{REMOVE_DIR}/ are missing or empty)."
        )
    return list(changes.values())


def _load_index() -> list[str]:
    _ensure_package_store()
    if (INDEX_PATH.exists() or INDEX_PATH.is_symlink()) and not INDEX_PATH.is_file():
        raise PackageError(f"{INDEX_PATH} is corrupted; expected an order file.")
    if not INDEX_PATH.is_file():
        return []
    value = _load_json(INDEX_PATH)
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("order"), list)
        or not all(isinstance(entry, str) for entry in value["order"])
    ):
        raise PackageError(f"{INDEX_PATH} is corrupted; expected an order list.")
    order = list(value["order"])
    try:
        for entry in order:
            _validate_package_id(entry, str(INDEX_PATH))
    except PackageError as error:
        raise PackageError(f"{INDEX_PATH} is corrupted: {error}") from error
    folded = [entry.casefold() for entry in order]
    if len(set(folded)) != len(folded):
        raise PackageError(
            f"{INDEX_PATH} is corrupted; package ids must be unique ignoring case."
        )
    return order


def _save_index(order: list[str]) -> None:
    _write_json(INDEX_PATH, {"order": order})


def _entry_dir(package_id: str) -> Path:
    return PACKAGES_DIR / package_id


def _installed_id(package_id: str, order: list[str]) -> str | None:
    folded = package_id.casefold()
    return next((entry for entry in order if entry.casefold() == folded), None)


def _load_record(package_id: str) -> dict[str, Any]:
    path = _entry_dir(package_id) / RECORD_NAME
    value = _load_json(path)
    corrupt = PackageError(
        f"{path} is corrupted; cannot manage package '{package_id}'."
    )
    if (
        not isinstance(value, dict)
        or value.get("id") != package_id
        or not isinstance(value.get("changes"), list)
        or not isinstance(value.get("package-req"), list)
        or not isinstance(value.get("created-dirs"), list)
    ):
        raise corrupt
    try:
        requirements = value["package-req"]
        if not all(isinstance(entry, str) for entry in requirements):
            raise corrupt
        for requirement in requirements:
            _validate_package_id(requirement, str(path))
        if len({entry.casefold() for entry in requirements}) != len(requirements):
            raise corrupt

        change_paths: set[str] = set()
        for change in value["changes"]:
            if (
                not isinstance(change, dict)
                or not isinstance(change.get("path"), str)
                or change.get("action") not in CHANGE_ACTIONS
            ):
                raise corrupt
            target = change["path"]
            if _normalize_target(target, str(path)) != target or target in change_paths:
                raise corrupt
            change_paths.add(target)

        created_dirs = value["created-dirs"]
        if not all(isinstance(entry, str) for entry in created_dirs):
            raise corrupt
        for directory in created_dirs:
            if _normalize_target(directory, str(path)) != directory:
                raise corrupt
    except PackageError as error:
        if error is corrupt:
            raise
        raise corrupt from error
    return value


def _snapshot(entry: Path, folder: str, target: str, data: bytes) -> None:
    path = entry / folder / PurePosixPath(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _apply_package(
    manifest: Manifest,
    package_dir: Path,
    source: Any,
    order: list[str],
    plan: list[Change] | None = None,
) -> dict[str, Any]:
    installed = {entry.casefold() for entry in order}
    missing = [
        entry for entry in manifest.requires if entry.casefold() not in installed
    ]
    if missing:
        raise PackageError(
            f"Package '{manifest.id}' requires packages that are not "
            f"installed: {', '.join(missing)}."
        )
    if plan is None:
        plan = _plan_changes(package_dir)

    created_dirs: list[str] = []
    seen_dirs: set[str] = set()
    for change in plan:
        if change.action != "add":
            continue
        pending: list[Path] = []
        parent = _target_path(change.target).parent
        while parent != TOOL_DIR and not parent.exists():
            pending.append(parent)
            parent = parent.parent
        for directory in reversed(pending):
            relative = directory.relative_to(TOOL_DIR).as_posix()
            if relative not in seen_dirs:
                seen_dirs.add(relative)
                created_dirs.append(relative)

    record = {
        "id": manifest.id,
        "name": manifest.name,
        "desc": manifest.desc,
        "package-req": list(manifest.requires),
        "credit": list(manifest.credit),
        "sykit-req": manifest.sykit_req,
        "deps": list(manifest.deps),
        "source": source,
        "installed": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "changes": [
            {"path": change.target, "action": change.action} for change in plan
        ],
        "created-dirs": created_dirs,
    }

    entry = _entry_dir(manifest.id)
    if entry.exists() or entry.is_symlink():
        raise PackageError(
            f"Package state path {entry} already exists; refusing to overwrite it."
        )
    applied: list[Change] = []
    made_dirs: list[Path] = []
    try:
        shutil.copytree(
            package_dir,
            entry / SOURCE_COPY_NAME,
            ignore=shutil.ignore_patterns(*IGNORED_COPY_PATTERNS),
        )
        for change in plan:
            if change.before is not None:
                _snapshot(entry, BEFORE_NAME, change.target, change.before)
            if change.after is not None:
                _snapshot(entry, AFTER_NAME, change.target, change.after)
        _write_json(entry / RECORD_NAME, record)
        for change in plan:
            destination = _target_path(change.target)
            if change.action == "remove":
                destination.unlink()
            else:
                if change.action == "add":
                    pending = []
                    parent = destination.parent
                    while parent != TOOL_DIR and not parent.exists():
                        pending.append(parent)
                        parent = parent.parent
                    for directory in reversed(pending):
                        directory.mkdir()
                        made_dirs.append(directory)
                destination.write_bytes(change.after or b"")
            applied.append(change)
        order.append(manifest.id)
        _save_index(order)
        return record
    except (OSError, PackageError) as error:
        if order and order[-1] == manifest.id:
            order.pop()
        for change in reversed(applied):
            try:
                destination = _target_path(change.target)
                if change.before is None:
                    destination.unlink(missing_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(change.before)
            except (OSError, PackageError):
                pass
        for directory in reversed(made_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        shutil.rmtree(entry, ignore_errors=True)
        raise PackageError(
            f"Failed to apply package '{manifest.id}': {error}"
        ) from error


def _reverse_package(record: dict[str, Any]) -> None:
    entry = _entry_dir(record["id"])
    for change in reversed(record["changes"]):
        destination = _target_path(change["path"])
        if change["action"] == "add":
            destination.unlink(missing_ok=True)
        else:
            before = (entry / BEFORE_NAME / PurePosixPath(change["path"])).read_bytes()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(before)
    for relative in reversed(record["created-dirs"]):
        try:
            (TOOL_DIR / PurePosixPath(relative)).rmdir()
        except OSError:
            pass


def _write_authors_file(order: list[str]) -> None:
    credited: list[tuple[str, str, list[str]]] = []
    for package_id in order:
        record = _load_record(package_id)
        credit = record.get("credit")
        if not isinstance(credit, list):
            continue
        names = [entry for entry in credit if isinstance(entry, str) and entry]
        if names:
            credited.append((package_id, str(record.get("name", "")), names))
    if not credited:
        AUTHORS_PATH.unlink(missing_ok=True)
        return
    lines = [
        "# Package credits",
        "",
        "Maintained by SyKit: the authors of currently installed packages",
        "that asked to be credited.",
        "",
    ]
    for package_id, name, names in credited:
        label = name or package_id
        lines.append(f"- **{label}** (`{package_id}`) - {', '.join(names)}")
    _write_bytes_atomic(
        AUTHORS_PATH,
        ("\n".join(lines) + "\n").encode("utf-8"),
    )


def _change_summary(record: dict[str, Any]) -> str:
    counts = {"add": 0, "edit": 0, "remove": 0}
    for change in record["changes"]:
        counts[change["action"]] += 1
    parts = [
        f"{counts['add']} added" if counts["add"] else "",
        f"{counts['edit']} edited" if counts["edit"] else "",
        f"{counts['remove']} removed" if counts["remove"] else "",
    ]
    return ", ".join(part for part in parts if part)


def _source_label(record: dict[str, Any]) -> str:
    source = record.get("source")
    if isinstance(source, dict):
        kind = source.get("kind")
        spec = source.get("spec")
        if kind == "github" and isinstance(spec, str):
            return spec
        if kind == "url":
            final = source.get("final_url", spec)
            host = ""
            if isinstance(final, str):
                try:
                    host = urlsplit(final).hostname or ""
                except ValueError:
                    host = ""
            return f"url:{host}" if host else "url"
    return "local"


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _report_lines(
    manifest: Manifest,
    source: dict[str, Any],
    notes: list[str],
    operations: list[Any],
    findings: list[Any],
) -> list[str]:
    lines = [f"Package: {manifest.name} ({manifest.id})"]
    if manifest.desc:
        lines.append(f"  {manifest.desc}")
    if manifest.sykit_req:
        lines.append(f"Requires SyKit {manifest.sykit_req} or newer.")
    source_line = f"Source: {source.get('spec', '')}"
    extras = []
    if source.get("ref_type"):
        extras.append(str(source["ref_type"]))
    sha = source.get("resolved_sha")
    if isinstance(sha, str) and sha:
        extras.append(f"commit {sha[:12]}")
    if extras:
        source_line += f" ({', '.join(extras)})"
    lines.append(source_line)
    lines.extend(notes)

    adds = sum(1 for operation in operations if operation.action == "add")
    edits = [operation for operation in operations if operation.action == "edit"]
    removes = sum(1 for operation in operations if operation.action == "remove")
    critical_targets = {
        finding.path for finding in findings if finding.severity == "critical"
    }
    core_edits = sum(1 for operation in edits if operation.target in critical_targets)
    size = sum(operation.size for operation in operations)
    lines.append(
        f"Adds {adds} files, edits {len(edits)} ({core_edits} core), removes "
        f"{removes}. New content: {_format_size(size)}."
    )
    lines.append("")
    if findings:
        for finding in findings:
            lines.append(
                f"  {finding.severity.upper():<9} {finding.code:<18} "
                f"{finding.action} {finding.path} - {finding.detail}"
            )
    else:
        lines.append("  No findings.")
    lines.append("")
    counts = {"critical": 0, "warning": 0, "info": 0}
    for finding in findings:
        counts[finding.severity] += 1
    lines.append(
        f"{counts['critical']} critical, {counts['warning']} warning(s), "
        f"{counts['info']} info."
    )
    lines.append(
        "Installing a package grants it the same trust as running SyKit's own code."
    )
    return lines


def _confirm_install(
    findings: list[Any],
    operations: list[Any],
    assume_yes: bool,
    allow_core: bool,
) -> bool:
    criticals = sum(1 for finding in findings if finding.severity == "critical")
    if criticals and not allow_core:
        print(
            f"Refusing to install: {criticals} critical finding(s). Review the "
            "package, then re-run with --allow-core to accept changes to "
            "SyKit core files."
        )
        return False
    if assume_yes:
        return True
    while True:
        try:
            answer = input("Install? [y/N, d shows package content] ")
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        answer = answer.strip().lower()
        if answer == "d":
            import package_analysis

            _print_lines(package_analysis.render_details(operations))
            continue
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print('Please answer "y", "n", or "d".')


def _command_add(
    argument: str,
    *,
    assume_yes: bool = False,
    allow_core: bool = False,
) -> bool:
    remote = None
    notes: list[str] = []
    package_dir = Path(argument)
    if package_dir.is_dir():
        source_info: dict[str, Any] = {
            "spec": argument,
            "kind": "local",
            "resolved_sha": None,
            "ref_type": None,
        }
        notes.append(f"Origin: local folder {argument}")
        if ID_PATTERN.fullmatch(argument):
            notes.append(
                f"Note: the local folder {argument!r} takes priority over the "
                f"package name {argument!r}; use github:Owner/Repo/... to "
                "force a remote fetch."
            )
    else:
        import package_remote

        resolved = package_remote.resolve(argument, _tool_settings())
        if isinstance(resolved, package_remote.RepoListing):
            _print_lines(resolved.lines)
            return True
        remote = resolved
        package_dir = remote.directory
        source_info = dict(remote.source)
        notes.extend(remote.notes)
    try:
        manifest = _load_manifest(package_dir)
        _check_sykit_requirement(manifest)
        order = _load_index()
        installed = _installed_id(manifest.id, order)
        if installed is not None:
            if installed == manifest.id:
                raise PackageError(f"Package '{manifest.id}' is already installed.")
            raise PackageError(
                f"Package id '{manifest.id}' conflicts with installed id "
                f"'{installed}' on case-insensitive filesystems."
            )
        plan = _plan_changes(package_dir)
        source_info["content_hash"] = _hash_package_folder(package_dir)

        import package_analysis

        operations = package_analysis.collect_operations(package_dir)
        findings = package_analysis.analyze_operations(operations, manifest.deps)
        _print_lines(_report_lines(manifest, source_info, notes, operations, findings))
        if not _confirm_install(findings, operations, assume_yes, allow_core):
            print("Aborted; no changes were made.")
            return False
        if _hash_package_folder(package_dir) != source_info["content_hash"]:
            raise PackageError(
                "Package contents changed between analysis and installation; aborting."
            )
        try:
            PACKAGES_DIR.mkdir(exist_ok=True)
        except OSError as error:
            raise PackageError(
                f"Could not create package state folder: {error}"
            ) from error
        authors_before = _optional_file_state(AUTHORS_PATH)
        record = _apply_package(manifest, package_dir, source_info, order, plan)
        try:
            _write_authors_file(order)
        except (PackageError, OSError) as error:
            try:
                _reverse_package(record)
                shutil.rmtree(_entry_dir(manifest.id))
                order.remove(manifest.id)
                _save_index(order)
                _restore_file_state(AUTHORS_PATH, authors_before)
            except (PackageError, OSError) as restore_error:
                raise PackageError(
                    f"Adding '{manifest.id}' failed ({error}) and rolling back "
                    f"also failed ({restore_error})."
                ) from restore_error
            raise PackageError(
                f"Adding '{manifest.id}' failed; everything was rolled back. ({error})"
            ) from error
        print(f"Added package '{manifest.id}' ({_change_summary(record)}).")
        if manifest.deps:
            quoted = " ".join(f'"{entry}"' for entry in manifest.deps)
            print(
                "This package declares dependencies that SyKit does not "
                "install automatically:"
            )
            print(f"  python -m pip install {quoted}")
        return True
    finally:
        if remote is not None:
            remote.cleanup()


def _command_remove(package_id: str) -> None:
    order = _load_index()
    installed = _installed_id(package_id, order)
    if installed is None:
        raise PackageError(f"Package '{package_id}' is not installed.")
    package_id = installed
    records = {entry: _load_record(entry) for entry in order}
    dependents = [
        entry
        for entry in order
        if entry != package_id
        and package_id.casefold()
        in {requirement.casefold() for requirement in records[entry]["package-req"]}
    ]
    if dependents:
        raise PackageError(
            f"Cannot remove '{package_id}': required by {', '.join(dependents)}."
        )

    position = order.index(package_id)
    tail = order[position + 1 :]
    involved = [package_id, *tail]
    original_order = list(order)
    authors_before = _optional_file_state(AUTHORS_PATH)

    # Snapshot every touched SyKit file and every involved .packages entry so
    # a failed re-apply can restore the exact prior state.
    stash = PACKAGES_DIR / ROLLBACK_NAME
    if stash.exists() or stash.is_symlink():
        raise PackageError(
            f"Recovery state already exists at {stash}; refusing to overwrite it."
        )
    file_states: dict[str, bytes | None] = {}
    existing_created_dirs: set[str] = set()
    try:
        for entry in involved:
            for relative in records[entry]["created-dirs"]:
                if _target_path(relative).is_dir():
                    existing_created_dirs.add(relative)
            for change in records[entry]["changes"]:
                target = change["path"]
                if target not in file_states:
                    path = _target_path(target)
                    file_states[target] = path.read_bytes() if path.is_file() else None
            shutil.copytree(_entry_dir(entry), stash / entry)
    except (PackageError, OSError) as error:
        shutil.rmtree(stash, ignore_errors=True)
        raise PackageError(
            f"Could not create rollback state for '{package_id}': {error}"
        ) from error

    try:
        for entry in reversed(tail):
            _reverse_package(records[entry])
            shutil.rmtree(_entry_dir(entry))
        _reverse_package(records[package_id])
        shutil.rmtree(_entry_dir(package_id))
        order = order[:position]
        _save_index(order)
        for entry in tail:
            source_copy = stash / entry / SOURCE_COPY_NAME
            manifest = _load_manifest(source_copy)
            # Re-applying the stored copy of an already-trusted package: no
            # analysis prompt here, on purpose. The bytes were reviewed and
            # accepted at install time and cannot have changed since.
            _apply_package(
                manifest, source_copy, records[entry].get("source", ""), order
            )
        _write_authors_file(order)
    except (PackageError, OSError) as error:
        try:
            for target, data in file_states.items():
                path = _target_path(target)
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            for relative in sorted(
                existing_created_dirs,
                key=lambda item: len(PurePosixPath(item).parts),
            ):
                _target_path(relative).mkdir(parents=True, exist_ok=True)
            for entry in involved:
                current = _entry_dir(entry)
                if current.exists():
                    shutil.rmtree(current)
                shutil.copytree(stash / entry, current)
            _save_index(original_order)
            _restore_file_state(AUTHORS_PATH, authors_before)
        except (PackageError, OSError) as restore_error:
            raise PackageError(
                f"Removing '{package_id}' failed ({error}) and rolling back "
                f"also failed ({restore_error}). The pre-removal state is "
                f"kept in {stash}."
            ) from restore_error
        shutil.rmtree(stash, ignore_errors=True)
        raise PackageError(
            f"Removing '{package_id}' failed while reapplying later packages; "
            f"everything was rolled back. ({error})"
        ) from error

    shutil.rmtree(stash, ignore_errors=True)
    if tail:
        print(f"Removed package '{package_id}' and reapplied: {', '.join(tail)}.")
    else:
        print(f"Removed package '{package_id}'.")


def _command_list() -> None:
    order = _load_index()
    if not order:
        print("No packages installed.")
        return
    print(f"Installed packages ({len(order)}):")
    for position, package_id in enumerate(order, start=1):
        record = _load_record(package_id)
        line = f"  {position}. {package_id}"
        name = record.get("name", "")
        if name and name != package_id:
            line += f" ({name})"
        line += f" - {_change_summary(record)}"
        print(line)
        if record.get("desc"):
            print(f"       {record['desc']}")
        print(f"       source: {_sanitize_text(_source_label(record))}")
        if record["package-req"]:
            print(f"       requires: {', '.join(record['package-req'])}")
        deps = record.get("deps")
        if isinstance(deps, list) and deps:
            joined = ", ".join(str(entry) for entry in deps)
            print(f"       deps: {_sanitize_text(joined)}")
        credit = record.get("credit")
        if isinstance(credit, list) and credit:
            print(f"       credit: {', '.join(credit)}")


def _command_diff(argument: str) -> None:
    order = _load_index()
    if argument == "*":
        if not order:
            print("No packages installed.")
            return
        targets = order
    else:
        installed = _installed_id(argument, order)
        if installed is None:
            raise PackageError(f"Package '{argument}' is not installed.")
        targets = [installed]

    for package_id in targets:
        record = _load_record(package_id)
        entry = _entry_dir(package_id)
        position = order.index(package_id) + 1
        print(f"=== {package_id} (package {position} of {len(order)}) ===")
        for change in record["changes"]:
            target = change["path"]
            action = change["action"]
            before = b""
            after = b""
            if action != "add":
                before = (entry / BEFORE_NAME / PurePosixPath(target)).read_bytes()
            if action != "remove":
                after = (entry / AFTER_NAME / PurePosixPath(target)).read_bytes()
            try:
                before_text = before.decode("utf-8")
                after_text = after.decode("utf-8")
            except UnicodeDecodeError:
                print(
                    f"Binary file {target}: {len(before)} -> {len(after)} "
                    f"bytes ({action})"
                )
                continue
            lines = unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile="/dev/null" if action == "add" else f"a/{target}",
                tofile="/dev/null" if action == "remove" else f"b/{target}",
            )
            emitted = False
            for line in lines:
                emitted = True
                text = line if line.endswith("\n") else line + "\n"
                sys.stdout.write(_sanitize_text(text.rstrip("\n")) + "\n")
            if not emitted:
                print(f"{target}: no content changes ({action})")
        print()


def print_package_help() -> None:
    print("Usage: python SyKit package <command>")
    print("Commands:")
    print("  add <source> [--yes] [--allow-core]")
    print("      Install a package. <source> is a local folder, a package name")
    print("      (name[@ref], fetched from the official packages repo), a")
    print("      github:Owner/Repo[/subdir][@ref] spec, or an https tarball URL.")
    print("      Every install prints a static analysis and asks to confirm.")
    print("      --yes skips the prompt when there are no critical findings;")
    print("      --allow-core is additionally required when the package touches")
    print("      SyKit core files.")
    print("  remove <id>  Uninstall a package as if it was never added")
    print("  list         Show installed packages and where they came from")
    print("  diff <id|*>  Show what a package (or every package) changed")


def run(arguments: list[str]) -> bool:
    if not arguments or arguments[0].lower() == "help":
        print_package_help()
        return True
    command, extra = arguments[0].lower(), arguments[1:]
    try:
        if command == "add" and extra:
            assume_yes = False
            allow_core = False
            positional: list[str] = []
            for argument in extra:
                lowered = argument.lower()
                if lowered == "--yes":
                    assume_yes = True
                elif lowered == "--allow-core":
                    allow_core = True
                elif lowered.startswith("--"):
                    print(f"Unknown package add option: {argument}")
                    return False
                else:
                    positional.append(argument)
            if len(positional) != 1:
                print_package_help()
                return False
            return _command_add(
                positional[0], assume_yes=assume_yes, allow_core=allow_core
            )
        if command == "remove" and len(extra) == 1:
            _command_remove(extra[0])
            return True
        if command == "list" and not extra:
            _command_list()
            return True
        if command == "diff" and len(extra) == 1:
            _command_diff(extra[0])
            return True
    except (PackageError, OSError) as error:
        print(f"Package command failed: {error}")
        return False
    print_package_help()
    return False


if __name__ == "__main__":
    raise SystemExit(0 if run(sys.argv[1:]) else 1)
