from __future__ import annotations

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
MANIFEST_KEYS = {"id", "name", "desc", "package-req", "credit"}
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


@dataclass
class Change:
    target: str
    action: str
    before: bytes | None
    after: bytes | None


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
    return Manifest(
        package_id,
        name,
        desc,
        tuple(requires),
        tuple(entry.strip() for entry in credit),
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
    return sorted(
        path for path in root.rglob("*") if path.is_file() and not _is_ignored(path)
    )


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

    def register(change: Change) -> None:
        if change.target in changes:
            raise PackageError(f"Package changes {change.target} more than once.")
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
    source_label: str,
    order: list[str],
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
        "source": source_label,
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
        lines.append(f"- **{label}** (`{package_id}`) — {', '.join(names)}")
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


def _command_add(argument: str) -> None:
    package_dir = Path(argument)
    if not package_dir.is_dir():
        raise PackageError(f"{package_dir} is not a package folder.")
    manifest = _load_manifest(package_dir)
    order = _load_index()
    installed = _installed_id(manifest.id, order)
    if installed is not None:
        if installed == manifest.id:
            raise PackageError(f"Package '{manifest.id}' is already installed.")
        raise PackageError(
            f"Package id '{manifest.id}' conflicts with installed id '{installed}' "
            "on case-insensitive filesystems."
        )
    try:
        PACKAGES_DIR.mkdir(exist_ok=True)
    except OSError as error:
        raise PackageError(f"Could not create package state folder: {error}") from error
    authors_before = _optional_file_state(AUTHORS_PATH)
    record = _apply_package(manifest, package_dir, argument, order)
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
                f"Adding '{manifest.id}' failed ({error}) and rolling back also "
                f"failed ({restore_error})."
            ) from restore_error
        raise PackageError(
            f"Adding '{manifest.id}' failed; everything was rolled back. ({error})"
        ) from error
    print(f"Added package '{manifest.id}' ({_change_summary(record)}).")


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
        if record["package-req"]:
            print(f"       requires: {', '.join(record['package-req'])}")
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
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
            if not emitted:
                print(f"{target}: no content changes ({action})")
        print()


def print_package_help() -> None:
    print("Usage: python SyKit package <command>")
    print("Commands:")
    print("  add <path>   Install the package folder at <path> into SyKit")
    print("  remove <id>  Uninstall a package as if it was never added")
    print("  list         Show installed packages in install order")
    print("  diff <id|*>  Show what a package (or every package) changed")


def run(arguments: list[str]) -> bool:
    if not arguments or arguments[0].lower() == "help":
        print_package_help()
        return True
    command, extra = arguments[0].lower(), arguments[1:]
    try:
        if command == "add" and len(extra) == 1:
            _command_add(extra[0])
            return True
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
