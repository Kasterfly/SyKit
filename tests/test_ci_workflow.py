from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class ContainerWorkflowTests(unittest.TestCase):
    def test_container_build_invokes_checkout_root(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("          python . init\n", workflow)
        self.assertIn("          python . build\n", workflow)
        self.assertNotIn("          python SyKit init\n", workflow)
        self.assertNotIn("          python SyKit build\n", workflow)


if __name__ == "__main__":
    unittest.main()
