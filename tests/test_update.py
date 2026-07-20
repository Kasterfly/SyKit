from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import package_remote
import update

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns(
    ".git", ".packages", "__pycache__", "*.pyc", "*.pyo", ".ruff_cache"
)


def _version_of(tool: Path) -> str:
    text = (tool / "sykit" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    return ""


# The tests derive every version from the tree they run in, so a release
# bump does not need to touch this file.
CURRENT = _version_of(ROOT)


def _higher_version(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _lower_version(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if patch:
        return f"{major}.{minor}.{patch - 1}"
    if minor:
        return f"{major}.{minor - 1}.99"
    return f"{major - 1}.99.99"


def _copy_tool(base: Path) -> Path:
    tool = base / "SyKit"
    shutil.copytree(ROOT, tool, ignore=IGNORE)
    (tool / ".git").mkdir()
    (tool / ".git" / "marker").write_text("keep me", encoding="utf-8")
    return tool


def _make_core(base: Path, name: str, version: str) -> Path:
    core = base / name
    shutil.copytree(ROOT, core, ignore=IGNORE)
    init_path = core / "sykit" / "__init__.py"
    init_path.write_text(
        init_path.read_text(encoding="utf-8").replace(
            f'__version__ = "{CURRENT}"', f'__version__ = "{version}"'
        ),
        encoding="utf-8",
    )
    readme = core / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(CURRENT, version),
        encoding="utf-8",
    )
    return core


def _make_package(
    base: Path,
    package_id: str,
    target: str,
    operations: list[dict[str, str]],
    sykit_req: str = "",
) -> Path:
    folder = base / package_id
    manifest: dict[str, object] = {"id": package_id}
    if sykit_req:
        manifest["sykit-req"] = sykit_req
    folder.mkdir(parents=True)
    (folder / "SyKitPackage.json").write_text(json.dumps(manifest), encoding="utf-8")
    payload = folder / "edit" / Path(target)
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text("# placeholder\n", encoding="utf-8")
    payload.with_name(payload.name + ".json").write_text(
        json.dumps(operations), encoding="utf-8"
    )
    return folder


def _run_tool(tool: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tool), *arguments],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


class UpdateCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-update-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.tool = _copy_tool(self.base)
        self.newer = _higher_version(CURRENT)
        self.older = _lower_version(CURRENT)

    def test_local_tree_update_swaps_core_and_preserves_state(self) -> None:
        core = _make_core(self.base, "new-core", self.newer)
        (core / "NEW-CORE-MARKER.txt").write_text("new", encoding="utf-8")
        (core / "check_requirements.py").unlink()

        result = _run_tool(self.tool, "update", str(core), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"SyKit core updated {CURRENT} -> {self.newer}.", result.stdout)
        self.assertEqual(_version_of(self.tool), self.newer)
        self.assertTrue((self.tool / "NEW-CORE-MARKER.txt").is_file())
        self.assertFalse((self.tool / "check_requirements.py").exists())
        self.assertEqual(
            (self.tool / ".git" / "marker").read_text(encoding="utf-8"),
            "keep me",
        )

    def test_packages_are_reapplied_and_failures_reported(self) -> None:
        survivor = _make_package(
            self.base,
            "pkg-appender",
            "docs/endpoints.md",
            [{"action": "append", "content": "\nPKG-APPEND-MARKER\n"}],
        )
        breaker = _make_package(
            self.base,
            "pkg-anchored",
            "README.md",
            [
                {
                    "action": "replace",
                    "anchor": f"Beta (`{CURRENT}`)",
                    "content": f"Beta (`{CURRENT}`) B-PATCH",
                }
            ],
        )
        for folder in (survivor, breaker):
            added = _run_tool(self.tool, "package", "add", str(folder), "--yes")
            self.assertEqual(added.returncode, 0, added.stdout + added.stderr)

        core = _make_core(self.base, "new-core", self.newer)
        result = _run_tool(self.tool, "update", str(core), "--yes")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("reapplied: pkg-appender", result.stdout)
        self.assertIn("FAILED: pkg-anchored", result.stdout)
        self.assertIn("could not be reapplied", result.stdout)

        self.assertEqual(_version_of(self.tool), self.newer)
        endpoints_doc = (self.tool / "docs" / "endpoints.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("PKG-APPEND-MARKER", endpoints_doc)
        readme = (self.tool / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("B-PATCH", readme)
        self.assertIn(self.newer, readme)

        listing = _run_tool(self.tool, "package", "list")
        self.assertIn("pkg-appender", listing.stdout)
        self.assertNotIn("pkg-anchored", listing.stdout)

    def test_downgrade_refuses_packages_that_need_newer_sykit(self) -> None:
        needy = _make_package(
            self.base,
            "pkg-needy",
            "docs/endpoints.md",
            [{"action": "append", "content": "\nNEEDY-MARKER\n"}],
            sykit_req=CURRENT,
        )
        added = _run_tool(self.tool, "package", "add", str(needy), "--yes")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)

        core = _make_core(self.base, "old-core", self.older)
        result = _run_tool(self.tool, "update", str(core), "--yes")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("downgrade", result.stdout)
        self.assertIn(f"requires SyKit {CURRENT} or newer", result.stdout)
        self.assertEqual(_version_of(self.tool), self.older)
        endpoints_doc = (self.tool / "docs" / "endpoints.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("NEEDY-MARKER", endpoints_doc)

    def test_up_to_date_stops_early(self) -> None:
        core = _make_core(self.base, "same-core", CURRENT)
        result = _run_tool(self.tool, "update", str(core), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already up to date", result.stdout)

    def test_closed_stdin_aborts_without_changes(self) -> None:
        appender = _make_package(
            self.base,
            "pkg-kept",
            "docs/endpoints.md",
            [{"action": "append", "content": "\nKEPT-MARKER\n"}],
        )
        added = _run_tool(self.tool, "package", "add", str(appender), "--yes")
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)

        core = _make_core(self.base, "new-core", self.newer)
        result = _run_tool(self.tool, "update", str(core))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Aborted; no changes were made.", result.stdout)
        self.assertEqual(_version_of(self.tool), CURRENT)
        listing = _run_tool(self.tool, "package", "list")
        self.assertIn("pkg-kept", listing.stdout)

    def test_non_sykit_tree_is_rejected(self) -> None:
        bogus = self.base / "not-sykit"
        bogus.mkdir()
        (bogus / "README.md").write_text("nope", encoding="utf-8")
        result = _run_tool(self.tool, "update", str(bogus), "--yes")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("does not look like a SyKit tree", result.stdout)
        self.assertEqual(_version_of(self.tool), CURRENT)

    def test_replaced_tool_config_is_reported(self) -> None:
        config_path = self.tool / "sykit" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["package-default-repo"] = "Someone/Else"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        core = _make_core(self.base, "new-core", self.newer)
        result = _run_tool(self.tool, "update", str(core), "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sykit/config.json was replaced", result.stdout)
        self.assertIn("package-default-repo", result.stdout)

    def test_yes_still_fails_closed_when_github_api_is_down(self) -> None:
        error = package_remote.ApiUnavailable("the GitHub API is unreachable")
        with mock.patch.object(
            package_remote, "fetch_repo", side_effect=error
        ) as fetch:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = update.run(["--yes"])
        self.assertFalse(result)
        self.assertIn("Update failed", output.getvalue())
        self.assertFalse(fetch.call_args.kwargs["allow_unreleased"])

    def test_allow_unreleased_flag_is_forwarded(self) -> None:
        error = package_remote.ApiUnavailable("the GitHub API is unreachable")
        with mock.patch.object(
            package_remote, "fetch_repo", side_effect=error
        ) as fetch:
            with contextlib.redirect_stdout(io.StringIO()):
                result = update.run(["main", "--yes", "--allow-unreleased"])
        self.assertFalse(result)
        self.assertTrue(fetch.call_args.kwargs["allow_unreleased"])


if __name__ == "__main__":
    unittest.main()
