from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import package
import package_analysis


class ManifestValidationTests(unittest.TestCase):
    """Parsing and validation of the sykit-req and deps manifest keys."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-manifest-test-")
        self.root = Path(self.temporary.name)
        self.counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def load(self, extra: dict) -> package.Manifest:
        self.counter += 1
        folder = self.root / f"fixture-{self.counter}"
        folder.mkdir()
        manifest = {"id": f"fixture-{self.counter}", **extra}
        (folder / package.MANIFEST_NAME).write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return package._load_manifest(folder)

    def test_new_keys_parse(self) -> None:
        manifest = self.load(
            {"sykit-req": "0.4.1", "deps": ["boto3>=1.34,<2", "requests"]}
        )
        self.assertEqual(manifest.sykit_req, "0.4.1")
        self.assertEqual(manifest.deps, ("boto3>=1.34,<2", "requests"))

    def test_absent_keys_default_empty(self) -> None:
        manifest = self.load({})
        self.assertEqual(manifest.sykit_req, "")
        self.assertEqual(manifest.deps, ())

    def test_deps_accepts_a_single_string(self) -> None:
        manifest = self.load({"deps": "requests>=2"})
        self.assertEqual(manifest.deps, ("requests>=2",))

    def test_invalid_sykit_req_is_rejected(self) -> None:
        for bad in ("1.2", "v1.2.3", "1.2.3.4", 123, [4, 1]):
            with self.subTest(bad=bad):
                with self.assertRaises(package.PackageError):
                    self.load({"sykit-req": bad})

    def test_invalid_deps_are_rejected(self) -> None:
        for bad in (
            [""],
            [5],
            ["ok", "OK"],
            ["-flag"],
            ["x" * (package.DEP_MAX_LENGTH + 1)],
            ["bad\x01dep"],
            "   ",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(package.PackageError):
                    self.load({"deps": bad})

    def test_unknown_keys_are_still_rejected(self) -> None:
        with self.assertRaisesRegex(package.PackageError, "unknown keys"):
            self.load({"future-key": 1})

    def test_parse_version(self) -> None:
        self.assertEqual(package._parse_version("1.10.2", "test"), (1, 10, 2))
        for bad in ("1.2", "abc", "", None):
            with self.subTest(bad=bad):
                with self.assertRaises(package.PackageError):
                    package._parse_version(bad, "test")


class ToolTreeCase(unittest.TestCase):
    """Shared scratch SyKit tree with the package module paths patched."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-keys-test-")
        self.root = Path(self.temporary.name)
        self.tool = self.root / "SyKit"
        self.sources = self.root / "sources"
        self.tool.mkdir()
        self.sources.mkdir()
        self.original_paths = (
            package.TOOL_DIR,
            package.PACKAGES_DIR,
            package.INDEX_PATH,
            package.AUTHORS_PATH,
        )
        package.TOOL_DIR = self.tool
        package.PACKAGES_DIR = self.tool / ".packages"
        package.INDEX_PATH = package.PACKAGES_DIR / "index.json"
        package.AUTHORS_PATH = package.PACKAGES_DIR / "authors.md"

    def tearDown(self) -> None:
        (
            package.TOOL_DIR,
            package.PACKAGES_DIR,
            package.INDEX_PATH,
            package.AUTHORS_PATH,
        ) = self.original_paths
        self.temporary.cleanup()

    def make_package(
        self,
        folder: str,
        package_id: str,
        *,
        manifest_extra: dict | None = None,
    ) -> Path:
        source = self.sources / folder
        source.mkdir()
        manifest = {"id": package_id, **(manifest_extra or {})}
        (source / package.MANIFEST_NAME).write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        payload = source / "add" / f"{folder}.txt"
        payload.parent.mkdir(parents=True)
        payload.write_text("payload", encoding="utf-8")
        return source

    def add(self, source: Path) -> bool:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = package._command_add(str(source), assume_yes=True)
        self.output = output.getvalue()
        return result


class RequirementGateTests(ToolTreeCase):
    def test_older_requirement_installs(self) -> None:
        source = self.make_package(
            "old-req", "old-req", manifest_extra={"sykit-req": "0.0.1"}
        )
        self.assertTrue(self.add(source))
        self.assertIn("Requires SyKit 0.0.1 or newer.", self.output)

    def test_equal_requirement_installs(self) -> None:
        current = package._current_sykit_version()
        source = self.make_package(
            "same-req", "same-req", manifest_extra={"sykit-req": current}
        )
        self.assertTrue(self.add(source))

    def test_future_requirement_refuses_before_prompt(self) -> None:
        source = self.make_package(
            "new-req", "new-req", manifest_extra={"sykit-req": "999.0.0"}
        )
        with mock.patch("builtins.input", side_effect=AssertionError):
            with self.assertRaisesRegex(package.PackageError, "requires SyKit 999.0.0"):
                with contextlib.redirect_stdout(io.StringIO()):
                    package._command_add(str(source))
        self.assertFalse((self.tool / "new-req.txt").exists())
        self.assertEqual(package._load_index(), [])
        self.assertFalse(package.PACKAGES_DIR.exists())


class DepsFlowTests(ToolTreeCase):
    def test_report_record_and_install_note(self) -> None:
        source = self.make_package(
            "with-deps", "with-deps", manifest_extra={"deps": ["boto3>=1.34,<2"]}
        )
        self.assertTrue(self.add(source))
        self.assertIn("dependency", self.output)
        self.assertIn("boto3>=1.34,<2", self.output)
        self.assertIn("python -m pip install", self.output)
        record = json.loads(
            (package.PACKAGES_DIR / "with-deps" / package.RECORD_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["deps"], ["boto3>=1.34,<2"])
        self.assertEqual(record["sykit-req"], "")

    def test_list_shows_deps(self) -> None:
        source = self.make_package(
            "listed", "listed", manifest_extra={"deps": ["requests>=2"]}
        )
        self.assertTrue(self.add(source))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            package._command_list()
        self.assertIn("deps: requests>=2", output.getvalue())

    def test_analyzer_emits_dependency_finding(self) -> None:
        findings = package_analysis.analyze_operations([], ("boto3>=1.34,<2",))
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.code, "dependency")
        self.assertEqual(finding.severity, "warning")
        self.assertEqual(finding.path, package.MANIFEST_NAME)
        self.assertIn("boto3>=1.34,<2", finding.detail)

    def test_reapply_during_removal_preserves_deps(self) -> None:
        first = self.make_package("first", "first")
        second = self.make_package(
            "second", "second", manifest_extra={"deps": ["requests>=2"]}
        )
        self.assertTrue(self.add(first))
        self.assertTrue(self.add(second))
        with contextlib.redirect_stdout(io.StringIO()):
            package._command_remove("first")
        record = json.loads(
            (package.PACKAGES_DIR / "second" / package.RECORD_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["deps"], ["requests>=2"])


if __name__ == "__main__":
    unittest.main()
