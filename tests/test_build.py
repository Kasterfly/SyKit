from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import build
from check_requirements import RequirementError, validate_node_version

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def endpoint(name: str, *, kind: str = "expose") -> build.EndpointInfo:
    return build.EndpointInfo(
        kind=kind,
        method="POST" if kind == "expose" else "GET",
        endpoint=name.lower(),
        function=name,
        module="endpoints",
        file="endpoints.py",
        is_async=False,
        parameters=(),
        permissions={},
        cors=(),
        limits={},
    )


class NodeRequirementTests(unittest.TestCase):
    def test_supported_node_versions(self) -> None:
        for version in ("v20.19.0", "22.12.0", "v24.0.0", "26.1.2"):
            with self.subTest(version=version):
                validate_node_version(version)

    def test_unsupported_node_versions(self) -> None:
        for version in ("18.20.0", "20.18.9", "21.7.3", "22.11.0", "23.9.0"):
            with self.subTest(version=version):
                with self.assertRaises(RequirementError):
                    validate_node_version(version)


class FrontendManifestTests(unittest.TestCase):
    def test_manifest_and_lockfile_match(self) -> None:
        manifest, dependencies = build._load_frontend_manifest()
        lock = json.loads(build.FRONTEND_LOCK_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["dependencies"], dependencies)
        self.assertEqual(lock["lockfileVersion"], 3)
        self.assertEqual(lock["packages"][""]["dependencies"], dependencies)
        self.assertTrue(
            all(
                build.PINNED_NPM_VERSION.fullmatch(value)
                for value in dependencies.values()
            )
        )


@unittest.skipUnless(NODE, "Node.js is required for generated-client tests")
class GeneratedClientTests(unittest.TestCase):
    def test_previous_collisions_are_valid_and_callable(self) -> None:
        names = [
            "API_PREFIX",
            "Error",
            "JSON",
            "Object",
            "URLSearchParams",
            "compact",
            "decodeResponse",
            "endpointUrl",
            "fetch",
            "get",
            "post",
            "undefined",
        ]
        module = build.generate_client_module({}, [endpoint(name) for name in names])
        with tempfile.TemporaryDirectory(prefix="sykit-client-test-") as directory:
            root = Path(directory)
            module_path = root / "client.mjs"
            module_path.write_text(module, encoding="utf-8")
            syntax = subprocess.run(
                [NODE, "--check", str(module_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

            runner = root / "runner.mjs"
            runner.write_text(
                "globalThis.fetch = async () => ({\n"
                "  ok: true, status: 200, text: async () => '{\"ok\":true}'\n"
                "});\n"
                f"const client = await import({json.dumps(module_path.as_uri())});\n"
                f"for (const name of {json.dumps(names)}) {{\n"
                "  const result = await client[name]();\n"
                "  if (result?.ok !== true) throw new Error(`failed: ${name}`);\n"
                "}\n",
                encoding="utf-8",
            )
            runtime = subprocess.run(
                [NODE, str(runner)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)

    def test_public_client_bindings_remain_reserved(self) -> None:
        for name in ("SyKitError", "globalThis"):
            with self.subTest(name=name):
                with self.assertRaises(build.BuildError):
                    build.generate_client_module({}, [endpoint(name)])

    def test_parser_rejects_public_client_bindings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-parser-test-") as directory:
            root = Path(directory)
            source = root / "endpoints.py"
            source.write_text(
                "from sykit import expose\n\n"
                "@expose('reserved')\n"
                "def SyKitError():\n"
                "    return None\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(build.BuildError, "generated \\$python client"):
                build.parse_decorators(source, root)


if __name__ == "__main__":
    unittest.main()
