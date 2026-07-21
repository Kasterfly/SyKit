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

    def test_container_job_uses_only_runtime_dependencies(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        container = workflow.split("\n  container:\n", 1)[1].split("\n  audit:\n", 1)[0]

        self.assertIn("cache-dependency-path: requirements.lock", container)
        self.assertIn("--require-hashes -r requirements.lock", container)
        self.assertNotIn("requirements-dev.lock", container)


class RuntimeMatrixTests(unittest.TestCase):
    def test_matrix_uses_supported_python_and_node_lines(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn('            python: "3.10"\n', workflow)
        self.assertNotIn('            node: "20.19.0"\n', workflow)
        self.assertIn('            python: "3.11"\n', workflow)
        self.assertIn('            python: "3.14"\n', workflow)
        self.assertIn('            node: "22.12.0"\n', workflow)
        self.assertIn('            node: "24"\n', workflow)

    def test_unit_matrix_uses_development_dependencies(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        test_job = workflow.split("\n  test:\n", 1)[1].split("\n  coverage:\n", 1)[0]

        self.assertIn("cache-dependency-path: requirements-dev.lock", test_job)
        self.assertIn("--require-hashes -r requirements-dev.lock", test_job)
        self.assertNotIn("-r requirements.lock", test_job)


if __name__ == "__main__":
    unittest.main()
