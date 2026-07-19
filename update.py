"""Update the SyKit tool folder to a new release, keeping packages.

    python SyKit update [source] [--yes]

Without a source this fetches the latest release of the update repo (the
"update-repo" tool setting, default Kasterfly/SyKit), falling back to
the default branch. The source can also be a tag, branch, or commit of
that repo, or a local folder holding a SyKit tree.

The command removes every installed package (removal restores a clean
core), replaces the core files with the fetched release, then reapplies
the stored copies of the packages in order and reports exactly which
ones no longer fit the new version.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import package

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_UPDATE_REPO = "Kasterfly/SyKit"
VERSION_LINE = re.compile(r'^__version__ = "(\d+\.\d+\.\d+)"$', re.MULTILINE)
REQUIRED_TREE_FILES = (
    "package.py",
    "build.py",
    "files/server.py",
    "sykit/__init__.py",
)
# Everything else at the tool root is owned by the release and replaced
# wholesale; these entries are never touched or deleted.
PRESERVED_ROOTS = frozenset({".git", ".packages"})
_MISSING = object()


class UpdateError(package.PackageError):
    """A user-facing update failure."""


def _tree_version(root: Path) -> str:
    path = root / "sykit" / "__init__.py"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise UpdateError(f"Could not read {path}: {error}") from error
    match = VERSION_LINE.search(text)
    if match is None:
        raise UpdateError(f"{path} does not declare __version__.")
    return match.group(1)


def _check_sykit_tree(root: Path) -> None:
    missing = [name for name in REQUIRED_TREE_FILES if not (root / name).is_file()]
    if missing:
        raise UpdateError(
            "The update source does not look like a SyKit tree; missing: "
            + ", ".join(missing)
            + "."
        )


def _read_tool_config() -> Any:
    path = TOOL_DIR / "sykit" / "config.json"
    if not path.is_file():
        return None
    try:
        return package._load_json(path)
    except package.PackageError:
        return None


def _update_repo_setting() -> str:
    config = _read_tool_config()
    if not isinstance(config, dict):
        config = {}
    repo = config.get("update-repo", DEFAULT_UPDATE_REPO)
    if not isinstance(repo, str) or "/" not in repo.strip():
        raise UpdateError(
            'The "update-repo" setting must be a string like "Owner/Repo".'
        )
    return repo.strip()


def _config_changes(old: Any, new: Any) -> list[str]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []
    return sorted(
        key
        for key in set(old) | set(new)
        if old.get(key, _MISSING) != new.get(key, _MISSING)
    )


def _confirm(assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input("Update? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _snapshot_packages(order: list[str], base: Path) -> dict[str, Path]:
    copies: dict[str, Path] = {}
    for package_id in order:
        source_copy = package._entry_dir(package_id) / package.SOURCE_COPY_NAME
        if not source_copy.is_dir():
            raise UpdateError(
                f"No stored copy for installed package '{package_id}'; "
                "cannot update safely."
            )
        target = base / package_id
        shutil.copytree(
            source_copy,
            target,
            ignore=shutil.ignore_patterns(*package.IGNORED_COPY_PATTERNS),
        )
        copies[package_id] = target
    return copies


def _remove_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _copy_entry(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(*package.IGNORED_COPY_PATTERNS),
        )
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _clear_core() -> None:
    for entry in list(TOOL_DIR.iterdir()):
        if entry.name.casefold() in PRESERVED_ROOTS:
            continue
        _remove_entry(entry)


def _replace_core(new_root: Path, backup: Path) -> None:
    """Make the tool folder equal the release tree, except preserved roots.

    A failure before anything is touched aborts cleanly; a failure during
    the swap restores the previous core from the backup copy.
    """
    try:
        for entry in TOOL_DIR.iterdir():
            if entry.name.casefold() in PRESERVED_ROOTS:
                continue
            _copy_entry(entry, backup / entry.name)
    except OSError as error:
        raise UpdateError(f"Could not back up the SyKit core: {error}") from error
    try:
        _clear_core()
        for entry in sorted(new_root.iterdir(), key=lambda item: item.name):
            if entry.name.casefold() in PRESERVED_ROOTS:
                continue
            _copy_entry(entry, TOOL_DIR / entry.name)
    except OSError as error:
        _clear_core()
        for entry in backup.iterdir():
            _copy_entry(entry, TOOL_DIR / entry.name)
        raise UpdateError(
            f"Replacing the SyKit core failed and was rolled back: {error}"
        ) from error


def _requirement_failure(manifest: package.Manifest, target_version: str) -> str | None:
    if not manifest.sykit_req:
        return None
    required = package._parse_version(manifest.sykit_req, f"package '{manifest.id}'")
    installed = package._parse_version(target_version, "the updated SyKit")
    if installed < required:
        return (
            f"requires SyKit {manifest.sykit_req} or newer; the updated "
            f"SyKit is {target_version}. Look for a release of the package "
            "made for this version."
        )
    return None


def _reapply_from_copies(
    order_ids: list[str],
    copies: dict[str, Path],
    records: dict[str, dict[str, Any]],
    target_version: str,
) -> list[tuple[str, str, str]]:
    """Reapply stored package copies in order; returns (id, status, detail).

    The bytes were reviewed and accepted at install time, so there is no
    analysis prompt here, mirroring how removal reapplies later packages.
    """
    results: list[tuple[str, str, str]] = []
    for package_id in order_ids:
        try:
            manifest = package._load_manifest(copies[package_id])
            failure = _requirement_failure(manifest, target_version)
            if failure is not None:
                results.append((package_id, "refused", failure))
                continue
            live_order = package._load_index()
            package._apply_package(
                manifest,
                copies[package_id],
                records[package_id].get("source", ""),
                live_order,
            )
            results.append((package_id, "reapplied", ""))
        except (package.PackageError, OSError) as error:
            results.append((package_id, "failed", str(error)))
    return results


def _command_update(source_argument: str, assume_yes: bool) -> bool:
    current = _tree_version(TOOL_DIR)
    order = package._load_index()
    records = {entry: package._load_record(entry) for entry in order}

    remote = None
    notes: list[str] = []
    if source_argument and Path(source_argument).is_dir():
        new_root = Path(source_argument)
        notes.append(f"Origin: local folder {source_argument}")
    else:
        import package_remote

        repo = _update_repo_setting()
        remote = package_remote.fetch_repo(
            repo, source_argument, package._tool_settings()
        )
        new_root = remote.directory
        notes.append(f"Origin: {remote.source['spec']}")
        sha = remote.source.get("resolved_sha")
        if sha:
            notes.append(f"Pinned commit: {sha[:12]}")
        notes.extend(remote.notes)
    try:
        _check_sykit_tree(new_root)
        new_version = _tree_version(new_root)
        if new_version == current:
            print(f"SyKit is already up to date ({current}).")
            return True

        print(f"Update SyKit {current} -> {new_version}.")
        for note in notes:
            print(package._sanitize_text(note))
        if order:
            print(f"Installed packages to reapply ({len(order)}): " + ", ".join(order))
        new_tuple = package._parse_version(new_version, "the update source")
        if new_tuple < package._parse_version(current, "this SyKit"):
            print(
                "Warning: this is a downgrade; packages made for newer "
                "SyKit versions may be refused."
            )
        print("The SyKit core files will be replaced by this release.")
        if not _confirm(assume_yes):
            print("Aborted; no changes were made.")
            return False

        old_config = _read_tool_config()
        with tempfile.TemporaryDirectory(
            prefix="sykit-update-", ignore_cleanup_errors=True
        ) as staging:
            base = Path(staging)
            copies = _snapshot_packages(order, base / "packages")

            removed: list[str] = []
            try:
                for package_id in reversed(order):
                    package._command_remove(package_id)
                    removed.append(package_id)
            except (package.PackageError, OSError) as error:
                recovery = _reapply_from_copies(
                    [entry for entry in order if entry in removed],
                    copies,
                    records,
                    current,
                )
                broken = [
                    entry for entry, status, _ in recovery if status != "reapplied"
                ]
                detail = f"Removing installed packages failed ({error})."
                if broken:
                    detail += (
                        " Reinstalling the already-removed packages also "
                        f"failed for: {', '.join(broken)}."
                    )
                else:
                    detail += " The already-removed packages were reinstalled."
                raise UpdateError(detail) from error

            backup = base / "core-backup"
            backup.mkdir()
            try:
                _replace_core(new_root, backup)
            except UpdateError:
                _reapply_from_copies(order, copies, records, current)
                raise

            results = _reapply_from_copies(order, copies, records, new_version)

        package._write_authors_file(package._load_index())
        print(f"SyKit core updated {current} -> {new_version}.")
        config_keys = _config_changes(old_config, _read_tool_config())
        if config_keys:
            print(
                "Note: sykit/config.json was replaced by the release "
                "template; differing keys: " + ", ".join(sorted(config_keys)) + ". "
                "Re-apply tool settings you still want."
            )
        failures = 0
        for package_id, status, detail in results:
            if status == "reapplied":
                print(f"  reapplied: {package_id}")
            else:
                failures += 1
                print(package._sanitize_text(f"  FAILED: {package_id} - {detail}"))
        if failures:
            print(
                f"{failures} package(s) could not be reapplied and are not "
                "installed. Look for newer releases and reinstall with: "
                "python SyKit package add <source>"
            )
            return False
        return True
    finally:
        if remote is not None:
            remote.cleanup()


def run(arguments: list[str]) -> bool:
    assume_yes = False
    positional: list[str] = []
    for argument in arguments:
        lowered = argument.lower()
        if lowered == "--yes":
            assume_yes = True
        elif lowered.startswith("--"):
            print(f"Unknown update option: {argument}")
            return False
        else:
            positional.append(argument)
    if len(positional) > 1:
        print("Usage: python SyKit update [ref-or-folder] [--yes]")
        return False
    try:
        return _command_update(positional[0] if positional else "", assume_yes)
    except (package.PackageError, OSError) as error:
        print(f"Update failed: {error}")
        return False
