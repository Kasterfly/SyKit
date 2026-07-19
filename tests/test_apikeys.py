from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ENDPOINTS_MODULE = """
def hook(session):
    return {"ok": True}


def limited(session):
    return {"ok": True}


def _meta(endpoint, name, api_key, limits):
    return {
        "kind": "web_hook",
        "method": "POST",
        "endpoint": endpoint,
        "name": name,
        "module": "probe",
        "file": "probe.py",
        "is_async": False,
        "parameters": [{"name": "session", "injected": True, "required": False}],
        "permissions": {},
        "cors": [],
        "limits": limits,
        "hidden": False,
        "token": None,
        "api_key": api_key,
    }


ENDPOINTS = [
    {
        "metadata": _meta("hook", "hook", {"scopes": ["reports:read"]}, {}),
        "function": hook,
    },
    {
        "metadata": _meta(
            "limited",
            "limited",
            {"scopes": []},
            {"per-key": {"requests": 2, "window": 3600}},
        ),
        "function": limited,
    },
]
"""

PROBE = """
import asyncio
import json

import server
from core import _apikeys


async def request(path, key=None):
    headers = [(b"host", b"127.0.0.1"), (b"content-type", b"application/json")]
    if key is not None:
        headers.append((b"x-api-key", key.encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        messages.append(message)

    await server.app(scope, receive, send)
    start = next(
        message for message in messages
        if message["type"] == "http.response.start"
    )
    content = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], content


async def main():
    store = server.API_KEY_STORE
    assert store is not None

    status, content = await request("/api/hook")
    assert status == 401, (status, content)

    status, content = await request("/api/hook", key="sykit_bogus_key")
    assert status == 401, (status, content)

    scoped_key, _record = _apikeys.issue_key(store, "reader", ["reports:read"])
    status, content = await request("/api/hook", key=scoped_key)
    assert status == 200 and json.loads(content)["ok"] is True, (status, content)

    plain_key, plain_record = _apikeys.issue_key(store, "plain", [])
    status, content = await request("/api/hook", key=plain_key)
    assert status == 403, (status, content)

    status, content = await request("/api/limited", key=plain_key)
    assert status == 200, (status, content)
    status, content = await request("/api/limited", key=plain_key)
    assert status == 200, (status, content)
    status, content = await request("/api/limited", key=plain_key)
    assert status == 429, (status, content)

    other_key, _other = _apikeys.issue_key(store, "other", [])
    status, content = await request("/api/limited", key=other_key)
    assert status == 200, "per-key buckets must be per key"

    assert store.revoke(plain_record["id"])
    status, content = await request("/api/limited", key=plain_key)
    assert status == 401, "revoked keys must stop working"


asyncio.run(main())
"""


def _load_apikeys_module():
    spec = importlib.util.spec_from_file_location(
        "sykit_test_apikeys", ROOT / "files" / "core" / "_apikeys.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_tool(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(ROOT), *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


class ApiKeyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.apikeys = _load_apikeys_module()
        self._directory = tempfile.TemporaryDirectory(prefix="sykit-keys-test-")
        self.addCleanup(self._directory.cleanup)
        self.store = self.apikeys.SqliteApiKeyStore(
            Path(self._directory.name) / "keys.db"
        )

    def test_issue_lookup_revoke_roundtrip(self) -> None:
        key, record = self.apikeys.issue_key(self.store, "ci bot", ["a", "b"])
        self.assertTrue(key.startswith("sykit_"))
        looked_up = self.store.lookup(self.apikeys.hash_key(key))
        self.assertEqual(looked_up["id"], record["id"])
        self.assertEqual(looked_up["name"], "ci bot")
        self.assertEqual(looked_up["scopes"], ["a", "b"])
        self.assertFalse(looked_up["revoked"])

        self.assertIsNone(self.store.lookup(self.apikeys.hash_key("wrong")))
        self.assertTrue(self.store.revoke(record["id"]))
        self.assertTrue(self.store.lookup(self.apikeys.hash_key(key))["revoked"])
        self.assertFalse(self.store.revoke("missing-id"))

    def test_list_keys_ordered_without_hashes(self) -> None:
        self.apikeys.issue_key(self.store, "first", [])
        self.apikeys.issue_key(self.store, "second", ["x"])
        records = self.store.list_keys()
        self.assertEqual([entry["name"] for entry in records], ["first", "second"])
        for entry in records:
            self.assertNotIn("key_hash", entry)
            self.assertNotIn("hash", entry)

    def test_name_and_scope_validation(self) -> None:
        for bad_name in ("", None, "x" * 100, "bad\nname"):
            with self.assertRaises(self.apikeys.ApiKeyError):
                self.apikeys.issue_key(self.store, bad_name, [])
        for bad_scopes in ("text", [1], ["ok", "du p"], ["dup", "DUP"]):
            with self.assertRaises(self.apikeys.ApiKeyError):
                self.apikeys.issue_key(self.store, "fine", bad_scopes)

    def test_keys_are_unique_and_hash_is_stable(self) -> None:
        key_one, _ = self.apikeys.issue_key(self.store, "one", [])
        key_two, _ = self.apikeys.issue_key(self.store, "two", [])
        self.assertNotEqual(key_one, key_two)
        self.assertEqual(self.apikeys.hash_key(key_one), self.apikeys.hash_key(key_one))


class ResolveKeyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.apikeys = _load_apikeys_module()
        self.root = Path("project-root")

    def test_default_and_sqlite_specs(self) -> None:
        store = self.apikeys.resolve_key_store("", self.root)
        self.assertEqual(store.database_path, self.root / ".sykit-apikeys.sqlite3")
        store = self.apikeys.resolve_key_store("sqlite:keys/k.db", self.root)
        self.assertEqual(store.database_path, self.root / "keys" / "k.db")

    def test_invalid_and_unknown_specs(self) -> None:
        with self.assertRaises(RuntimeError):
            self.apikeys.resolve_key_store(7, self.root)
        with self.assertRaises(RuntimeError):
            self.apikeys.resolve_key_store("no good:x", self.root)
        with self.assertRaisesRegex(RuntimeError, "_keystore_vault"):
            self.apikeys.resolve_key_store("vault:secret/keys", self.root)

    def test_provider_convention(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-keystore-pkg-") as directory:
            package = Path(directory) / "core"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "_keystore_fake.py").write_text(
                textwrap.dedent(
                    """
                    class FakeStore:
                        def __init__(self, target):
                            self.target = target

                        def lookup(self, key_hash):
                            return None

                        def create(self, record, key_hash):
                            pass

                        def list_keys(self):
                            return []

                        def revoke(self, key_id):
                            return False


                    def create(target):
                        return FakeStore(target)
                    """
                ),
                encoding="utf-8",
            )
            sys.path.insert(0, directory)
            try:
                store = self.apikeys.resolve_key_store("fake:tenant-a", self.root)
                self.assertEqual(store.target, "tenant-a")
            finally:
                sys.path.remove(directory)
                for name in [
                    name
                    for name in sys.modules
                    if name == "core" or name.startswith("core.")
                ]:
                    del sys.modules[name]


class BuildValidationTests(unittest.TestCase):
    def _parse(self, source: str):
        import build

        with tempfile.TemporaryDirectory(prefix="sykit-keys-build-") as directory:
            path = Path(directory) / "endpoints.py"
            path.write_text(textwrap.dedent(source), encoding="utf-8")
            return build.parse_decorators(path, Path(directory))

    def test_api_key_parses_bare_and_scoped(self) -> None:
        endpoints = self._parse(
            """
            from sykit.utils import api_key, web_hook

            @web_hook("plain")
            @api_key
            def plain():
                return {}

            @web_hook("scoped")
            @api_key(["reports:read", "reports:write"])
            def scoped():
                return {}
            """
        )
        by_name = {endpoint.function: endpoint for endpoint in endpoints}
        self.assertEqual(by_name["plain"].api_key, {"scopes": []})
        self.assertEqual(
            by_name["scoped"].api_key,
            {"scopes": ["reports:read", "reports:write"]},
        )

    def test_api_key_requires_web_hook(self) -> None:
        import build

        with self.assertRaisesRegex(build.BuildError, "web_hook"):
            self._parse(
                """
                from sykit.utils import api_key, expose

                @expose("nope")
                @api_key
                def nope():
                    return {}
                """
            )

    def test_api_key_rejects_hidden_and_duplicates_and_bad_scopes(self) -> None:
        import build

        with self.assertRaisesRegex(build.BuildError, "hidden"):
            self._parse(
                """
                from sykit.utils import api_key, hidden, web_hook

                @web_hook("nope")
                @api_key
                @hidden
                def nope():
                    return {}
                """
            )
        with self.assertRaisesRegex(build.BuildError, "only one @api_key"):
            self._parse(
                """
                from sykit.utils import api_key, web_hook

                @web_hook("nope")
                @api_key
                @api_key(["a"])
                def nope():
                    return {}
                """
            )
        with self.assertRaisesRegex(build.BuildError, "scope"):
            self._parse(
                """
                from sykit.utils import api_key, web_hook

                @web_hook("nope")
                @api_key(["bad scope"])
                def nope():
                    return {}
                """
            )

    def test_per_key_limit_requires_api_key(self) -> None:
        import build

        with self.assertRaisesRegex(build.BuildError, "per-key"):
            self._parse(
                """
                from sykit.utils import limits, web_hook

                @web_hook("nope")
                @limits({"per-key": "5m"})
                def nope():
                    return {}
                """
            )


class KeysCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="sykit-keys-cli-")
        self.addCleanup(self._directory.cleanup)
        self.project = Path(self._directory.name)

    def test_generate_list_revoke_flow(self) -> None:
        generated = _run_tool(
            ["keys", "generate", "ci bot", "--scopes", "reports:read"],
            self.project,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        key_line = next(
            line.strip()
            for line in generated.stdout.splitlines()
            if line.strip().startswith("sykit_")
        )
        self.assertTrue((self.project / ".sykit-apikeys.sqlite3").is_file())

        listing = _run_tool(["keys", "list"], self.project)
        self.assertIn("ci bot", listing.stdout)
        self.assertIn("reports:read", listing.stdout)
        self.assertNotIn(key_line, listing.stdout)
        key_id = key_line.split("_")[1]
        self.assertIn(key_id, listing.stdout)

        revoked = _run_tool(["keys", "revoke", key_id], self.project)
        self.assertEqual(revoked.returncode, 0, revoked.stdout + revoked.stderr)
        listing = _run_tool(["keys", "list"], self.project)
        self.assertIn("REVOKED", listing.stdout)

        missing = _run_tool(["keys", "revoke", "nope"], self.project)
        self.assertEqual(missing.returncode, 1)

    def test_project_config_store_spec_is_used(self) -> None:
        sykit_dir = self.project / "src" / "sykit"
        sykit_dir.mkdir(parents=True)
        (sykit_dir / "config.json").write_text(
            json.dumps({"apikey-store": "sqlite:custom-keys.db"}),
            encoding="utf-8",
        )
        generated = _run_tool(["keys", "generate", "bot"], self.project)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertTrue((self.project / "custom-keys.db").is_file())
        self.assertFalse((self.project / ".sykit-apikeys.sqlite3").exists())


class KeyedEndpointTests(unittest.TestCase):
    def test_keyed_web_hooks_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-keys-app-") as directory:
            runtime = Path(directory) / "app-root" / "built"
            runtime.mkdir(parents=True)
            (runtime / "core").mkdir()
            (runtime / "app").mkdir()
            (runtime / "static").mkdir()
            shutil.copy2(ROOT / "files" / "server.py", runtime / "server.py")
            for name in ("__init__.py", "_limits.py", "_sessions.py", "_apikeys.py"):
                shutil.copy2(ROOT / "files" / "core" / name, runtime / "core" / name)
            shutil.copytree(
                ROOT / "sykit",
                runtime / "app" / "sykit",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            (runtime / "config.json").write_text(
                json.dumps({"endpoints": "/api/", "allowed-hosts": ["127.0.0.1"]}),
                encoding="utf-8",
            )
            (runtime / "core" / "_endpoints.py").write_text(
                ENDPOINTS_MODULE, encoding="utf-8"
            )
            probe = runtime / "probe.py"
            probe.write_text(PROBE, encoding="utf-8")
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["SYKIT_SESSION_SECRET"] = (
                "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
            )
            result = subprocess.run(
                [sys.executable, str(probe)],
                cwd=runtime,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue(
                (runtime.parent / ".sykit-apikeys.sqlite3").is_file(),
                "default key store must live in the project root, not built/",
            )


if __name__ == "__main__":
    unittest.main()
