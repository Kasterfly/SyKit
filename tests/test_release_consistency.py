from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r'^__version__ = "(\d+\.\d+\.\d+)"$', re.MULTILINE)


class ReleaseConsistencyTests(unittest.TestCase):
    def test_version_readme_changelog_and_install_tag_match(self) -> None:
        init_text = (ROOT / "sykit" / "__init__.py").read_text(encoding="utf-8")
        match = VERSION_PATTERN.search(init_text)
        self.assertIsNotNone(match)
        version = match.group(1)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        detailed = ROOT / "docs" / "changelogs" / f"update-{version}.md"
        status = "Stable" if int(version.split(".", 1)[0]) >= 1 else "Beta"
        self.assertIn(f"{status} (`{version}`)", readme)
        self.assertIn(f"--branch {version}", readme)
        self.assertIn(f"## {version} -", changelog)
        self.assertTrue(detailed.is_file())
        self.assertTrue(
            detailed.read_text(encoding="utf-8").startswith(f"# SyKit {version} ")
        )

    def test_github_tag_matches_source_version_when_present(self) -> None:
        if os.environ.get("GITHUB_REF_TYPE") != "tag":
            self.skipTest("not a tag workflow")
        init_text = (ROOT / "sykit" / "__init__.py").read_text(encoding="utf-8")
        version = VERSION_PATTERN.search(init_text).group(1)
        self.assertEqual(os.environ.get("GITHUB_REF_NAME"), version)


if __name__ == "__main__":
    unittest.main()
