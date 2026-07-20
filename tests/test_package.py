from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import package


class PackageManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-package-test-")
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
        add: dict[str, str] | None = None,
        edit: dict[str, str] | None = None,
        remove: list[str] | None = None,
        credit: list[str] | None = None,
    ) -> Path:
        source = self.sources / folder
        source.mkdir()
        manifest: dict[str, object] = {"id": package_id}
        if credit is not None:
            manifest["credit"] = credit
        (source / package.MANIFEST_NAME).write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        for section, files in (("add", add), ("edit", edit)):
            for relative, content in (files or {}).items():
                path = source / section / Path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        if remove is not None:
            remove_root = source / "remove"
            remove_root.mkdir()
            (remove_root / "paths.json").write_text(
                json.dumps(remove),
                encoding="utf-8",
            )
        return source

    def add(self, source: Path) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            package._command_add(str(source), assume_yes=True)

    def remove(self, package_id: str) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            package._command_remove(package_id)

    def test_python_edits_invalidate_cached_bytecode(self) -> None:
        module = self.tool / "module.py"
        module.write_text("value = 'old'\n", encoding="utf-8")
        cache = self.tool / "__pycache__"
        cache.mkdir()
        bytecode = cache / "module.cpython-314.pyc"
        bytecode.write_bytes(b"stale")
        source = self.make_package(
            "bytecode-package",
            "bytecode-package",
            edit={"module.py": "value = 'new'\n"},
        )

        self.add(source)
        self.assertFalse(bytecode.exists())
        bytecode.write_bytes(b"new-stale")
        self.remove("bytecode-package")
        self.assertFalse(bytecode.exists())
        self.assertEqual(module.read_text(encoding="utf-8"), "value = 'old'\n")

    def test_protected_roots_are_case_insensitive(self) -> None:
        git = self.tool / ".git"
        git.mkdir()
        config = git / "config"
        config.write_text("original", encoding="utf-8")
        source = self.make_package(
            "protected",
            "protected-probe",
            edit={".GIT/config": "modified"},
        )
        with self.assertRaises(package.PackageError):
            self.add(source)
        self.assertEqual(config.read_text(encoding="utf-8"), "original")
        self.assertEqual(package._load_index(), [])
        self.assertFalse(package.PACKAGES_DIR.exists())

    def test_ambiguous_target_components_are_rejected(self) -> None:
        for target in (
            ".PACKAGES/index.json",
            " folder/file.txt",
            "folder./file.txt",
            "folder/CON.txt",
            "folder/file.txt ",
        ):
            with self.subTest(target=target):
                with self.assertRaises(package.PackageError):
                    package._normalize_target(target, "test")

    def test_package_state_must_stay_inside_tool(self) -> None:
        outside = self.root / "outside-state"
        outside.mkdir()
        package.PACKAGES_DIR = outside
        package.INDEX_PATH = outside / "index.json"
        package.AUTHORS_PATH = outside / "authors.md"
        with self.assertRaises(package.PackageError):
            package._load_index()

    def test_reserved_and_unsafe_ids_are_rejected(self) -> None:
        for position, package_id in enumerate(
            ("authors.md", "INDEX.JSON", "con", "name.", "line\n")
        ):
            with self.subTest(package_id=package_id):
                source = self.make_package(
                    f"reserved-{position}",
                    package_id,
                    add={f"probe-{position}.txt": "payload"},
                )
                with self.assertRaises(package.PackageError):
                    self.add(source)
                self.assertFalse((self.tool / f"probe-{position}.txt").exists())

    def test_case_colliding_ids_leave_original_state_unchanged(self) -> None:
        first = self.make_package("first", "Foo", add={"one.txt": "one"})
        second = self.make_package("second", "foo", add={"two.txt": "two"})
        self.add(first)
        with self.assertRaises(package.PackageError):
            self.add(second)
        self.assertEqual(package._load_index(), ["Foo"])
        self.assertTrue((self.tool / "one.txt").is_file())
        self.assertFalse((self.tool / "two.txt").exists())
        self.assertTrue((package.PACKAGES_DIR / "Foo" / "record.json").is_file())

    def test_author_update_failure_rolls_back_add(self) -> None:
        source = self.make_package("rollback-add", "rollback-add", add={"x.txt": "x"})
        with mock.patch.object(
            package,
            "_write_authors_file",
            side_effect=OSError("injected author failure"),
        ):
            with self.assertRaisesRegex(
                package.PackageError, "everything was rolled back"
            ):
                self.add(source)
        self.assertFalse((self.tool / "x.txt").exists())
        self.assertEqual(package._load_index(), [])
        self.assertFalse((package.PACKAGES_DIR / "rollback-add").exists())

    def test_author_update_failure_rolls_back_remove(self) -> None:
        source = self.make_package(
            "rollback-remove",
            "Rollback-Remove",
            add={"x.txt": "x"},
            credit=["Test Author"],
        )
        self.add(source)
        index_before = package.INDEX_PATH.read_bytes()
        authors_before = package.AUTHORS_PATH.read_bytes()
        with mock.patch.object(
            package,
            "_write_authors_file",
            side_effect=OSError("injected author failure"),
        ):
            with self.assertRaisesRegex(
                package.PackageError, "everything was rolled back"
            ):
                self.remove("rollback-remove")
        self.assertTrue((self.tool / "x.txt").is_file())
        self.assertEqual(package.INDEX_PATH.read_bytes(), index_before)
        self.assertEqual(package.AUTHORS_PATH.read_bytes(), authors_before)
        self.assertTrue(
            (package.PACKAGES_DIR / "Rollback-Remove" / "record.json").is_file()
        )

    def test_remove_and_diff_resolve_id_case_insensitively(self) -> None:
        source = self.make_package("case-command", "Mixed-Case", add={"x.txt": "x"})
        self.add(source)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            package._command_diff("mixed-case")
        self.assertIn("Mixed-Case", output.getvalue())
        self.remove("MIXED-CASE")
        self.assertEqual(package._load_index(), [])
        self.assertFalse((self.tool / "x.txt").exists())

    def test_failed_reapply_restores_empty_created_directories(self) -> None:
        first = self.make_package(
            "directory-owner",
            "directory-owner",
            add={"created/file.txt": "payload"},
        )
        second = self.make_package(
            "file-remover",
            "file-remover",
            remove=["created/file.txt"],
        )
        self.add(first)
        self.add(second)
        self.assertTrue((self.tool / "created").is_dir())
        self.assertEqual(list((self.tool / "created").iterdir()), [])

        with self.assertRaisesRegex(package.PackageError, "everything was rolled back"):
            self.remove("directory-owner")

        self.assertTrue((self.tool / "created").is_dir())
        self.assertEqual(list((self.tool / "created").iterdir()), [])
        self.assertEqual(package._load_index(), ["directory-owner", "file-remover"])


if __name__ == "__main__":
    unittest.main()
