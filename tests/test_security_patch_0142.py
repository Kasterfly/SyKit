from __future__ import annotations

import asyncio
import base64
import importlib.util
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import build as build_module
import check_requirements as requirements_module
from sykit import auth

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SESSIONS = _load_module(
    "sykit_test_security_patch_sessions", ROOT / "files" / "core" / "_sessions.py"
)
TASK_STORE = _load_module(
    "sykit_test_security_patch_store", ROOT / "files" / "core" / "_task_store.py"
)
TASK_RUNTIME = _load_module(
    "sykit_test_security_patch_runtime",
    ROOT / "files" / "core" / "_task_runtime.py",
)


class RecordingSessionStore:
    def __init__(self) -> None:
        self.saved = []

    def save(self, session_id, data, max_age) -> None:
        self.saved.append((session_id, dict(data), max_age))

    def delete(self, session_id) -> None:
        return None

    def touch(self, session_id, max_age) -> None:
        return None


class SessionSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_only_cookieless_session_is_not_persisted(self) -> None:
        store = RecordingSessionStore()
        middleware = SESSIONS.SessionMiddleware(
            None,
            secret="x" * 32,
            store=store,
            cookie_name="sykit_session",
            max_age=60,
            https_only=False,
        )
        scope = {"session": {"__sykit_rate_id": "anonymous-bucket"}}
        message = {"type": "http.response.start", "headers": []}

        await middleware._persist(scope, message, None, False, False, "{}")

        self.assertEqual(scope["session"], {})
        self.assertEqual(store.saved, [])
        self.assertEqual(message["headers"], [])

    async def test_user_session_still_persists(self) -> None:
        store = RecordingSessionStore()
        middleware = SESSIONS.SessionMiddleware(
            None,
            secret="x" * 32,
            store=store,
            cookie_name="sykit_session",
            max_age=60,
            https_only=False,
        )
        scope = {
            "session": {
                "__sykit_rate_id": "stable-bucket",
                "role": "member",
            }
        }
        message = {"type": "http.response.start", "headers": []}

        await middleware._persist(scope, message, None, False, False, "{}")

        self.assertEqual(store.saved[0][1]["role"], "member")
        self.assertTrue(message["headers"])


class AuthSecurityTests(unittest.TestCase):
    def test_login_preserves_rate_identity(self) -> None:
        session = {"__sykit_rate_id": "stable-bucket", "role": "guest"}
        with mock.patch.object(auth, "_session", return_value=session):
            auth.login({"role": "member"})
        self.assertEqual(session["__sykit_rate_id"], "stable-bucket")
        self.assertEqual(session["role"], "member")
        self.assertTrue(session["__sykit_rotate"])

    def test_tampered_scrypt_cost_and_key_length_are_rejected(self) -> None:
        encoded_salt = base64.b64encode(b"salt").decode("ascii")
        cases = (
            (
                2**22,
                32,
                base64.b64encode(b"x" * 32).decode("ascii"),
            ),
            (
                auth.SCRYPT_N,
                auth.SCRYPT_R,
                base64.b64encode(b"x" * (auth.MAX_VERIFY_KEY_BYTES + 1)).decode(
                    "ascii"
                ),
            ),
        )
        for cost, block_size, encoded_key in cases:
            stored = f"scrypt${cost}${block_size}$1${encoded_salt}${encoded_key}"
            with (
                self.subTest(cost=cost, block_size=block_size),
                mock.patch.object(auth.hashlib, "scrypt") as derive,
                self.assertRaisesRegex(auth.AuthError, "out-of-range"),
            ):
                auth.verify_password("password", stored)
            derive.assert_not_called()


class TaskSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovered_task_past_attempt_cap_is_not_run(self) -> None:
        calls = []
        failures = []
        event_loop = asyncio.get_running_loop()

        def background() -> None:
            calls.append(True)

        record = {
            "metadata": {
                "id": "jobs:background",
                "name": "background",
                "module": "jobs",
                "file": "jobs.py",
                "is_async": False,
                "schedule": None,
            },
            "function": background,
        }

        class Store:
            def claim(self, worker_id, lease_seconds):
                return {
                    "id": "task-id",
                    "task": "jobs:background",
                    "args": [],
                    "kwargs": {},
                    "attempt": 4,
                }

            def fail(self, task_id, worker_id, error):
                failures.append((task_id, worker_id, error))
                event_loop.call_soon_threadsafe(manager._stop.set)
                return True

        manager = TASK_RUNTIME.TaskManager(
            Store(),
            [record],
            1,
            logging.getLogger("sykit.test.security_patch.attempts"),
            max_attempts=3,
        )

        await asyncio.wait_for(manager._worker("worker"), timeout=2)

        self.assertEqual(calls, [])
        self.assertEqual(failures[0][:2], ("task-id", "worker"))
        self.assertIn("3-attempt limit", failures[0][2])

    async def test_payload_cap_counts_utf8_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "payload limit"):
            TASK_RUNTIME.TaskManager._payload(
                ("x" * TASK_RUNTIME.MAX_PAYLOAD_BYTES,),
                {},
            )

    async def test_old_failed_rows_are_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-task-cleanup-") as directory:
            database = Path(directory) / "tasks.db"
            store = TASK_STORE.SqliteTaskStore(database)
            with mock.patch.object(TASK_STORE.time, "time", return_value=100.0):
                task_id = store.enqueue("jobs:failed", [], {})
                store.claim("worker", 60)
                store.fail(task_id, "worker", "failed")

            cleanup_time = 101.0 + TASK_STORE.FAILED_RETENTION_SECONDS
            with mock.patch.object(
                TASK_STORE.time,
                "time",
                return_value=cleanup_time,
            ):
                self.assertIsNone(store.claim("worker", 60))

            connection = sqlite3.connect(database)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM sykit_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 0)


class BuildSecurityTests(unittest.TestCase):
    def test_windows_path_search_skips_current_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-path-search-") as directory:
            root = Path(directory)
            current = root / "project"
            trusted = root / "trusted"
            current.mkdir()
            trusted.mkdir()
            planted = current / "node.EXE"
            expected = trusted / "node.EXE"
            planted.write_text("planted", encoding="utf-8")
            expected.write_text("trusted", encoding="utf-8")
            planted.chmod(0o755)
            expected.chmod(0o755)

            resolved = requirements_module._find_windows_executable(
                "node",
                [str(current), str(trusted)],
                current,
                ".EXE;.CMD",
            )

            self.assertEqual(Path(resolved).resolve(), expected.resolve())

    def test_generated_paths_are_added_to_gitignore_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-gitignore-") as directory:
            gitignore = Path(directory) / ".gitignore"
            gitignore.write_text(".env\n", encoding="utf-8")
            with mock.patch.object(build_module, "GITIGNORE_PATH", gitignore):
                build_module._ensure_gitignore_build_outputs()
                build_module._ensure_gitignore_build_outputs()
            self.assertEqual(
                gitignore.read_text(encoding="utf-8"),
                ".env\nbuilt/\n__sykitcache__/\n",
            )

    def test_generated_dockerignore_excludes_secrets_and_state(self) -> None:
        self.assertIn(".env\n", build_module.DOCKERIGNORE)
        self.assertIn(".sykit-apikeys.sqlite3\n", build_module.DOCKERIGNORE)

    def test_task_max_attempts_is_a_known_config_key(self) -> None:
        self.assertIn("task-max-attempts", build_module.CONFIG_KEYS)


class ServerSecurityTests(unittest.TestCase):
    def test_header_cache_and_warning_regressions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-server-patch-") as directory:
            runtime = Path(directory) / "built"
            (runtime / "core").mkdir(parents=True)
            (runtime / "app").mkdir()
            (runtime / "static" / "assets" / "private").mkdir(parents=True)
            shutil.copy2(ROOT / "files" / "server.py", runtime / "server.py")
            for source in (ROOT / "files" / "core").glob("*.py"):
                shutil.copy2(source, runtime / "core" / source.name)
            shutil.copytree(ROOT / "sykit", runtime / "app" / "sykit")
            (runtime / "static" / "index.html").write_text(
                "<main>fallback</main>", encoding="utf-8"
            )
            (runtime / "static" / "assets" / "private" / "secret.js").write_text(
                "export const secret = true;\n", encoding="utf-8"
            )
            (runtime / "static" / "assets" / "public.js").write_text(
                "export const value = true;\n", encoding="utf-8"
            )
            (runtime / "config.json").write_text(
                """
                {
                    "endpoints": "/api/",
                    "allowed-hosts": ["testserver"],
                    "page-perms": {
                        "/assets/private": {"Session": {"role": "admin"}}
                    }
                }
                """,
                encoding="utf-8",
            )
            (runtime / "app" / "endpoints.py").write_text(
                "def hook():\n    return {'ok': True}\n", encoding="utf-8"
            )
            metadata = {
                "kind": "web_hook",
                "method": "POST",
                "endpoint": "hook",
                "name": "hook",
                "module": "endpoints",
                "file": "endpoints.py",
                "is_async": False,
                "parameters": [],
                "permissions": {},
                "cors": [],
                "limits": {"per-session": {"count": 10, "window": 60}},
                "hidden": False,
                "token": None,
                "api_key": {"scopes": []},
                "max_upload_bytes": None,
            }
            (runtime / "core" / "_endpoints.py").write_text(
                "from endpoints import hook\n"
                f"ENDPOINTS = [{{'metadata': {metadata!r}, 'function': hook}}]\n",
                encoding="utf-8",
            )
            (runtime / "core" / "_tasks.py").write_text(
                "TASKS = []\n", encoding="utf-8"
            )
            probe = runtime / "probe.py"
            probe.write_text(
                textwrap.dedent(
                    """
                    import asyncio
                    import logging
                    from pathlib import Path

                    from starlette.requests import Request
                    import server

                    duplicate_scope = {
                        "type": "http",
                        "method": "POST",
                        "path": "/api/hook",
                        "headers": [
                            (b"x-api-key", b"first"),
                            (b"x-api-key", b"second"),
                        ],
                    }
                    duplicate = Request(duplicate_scope)
                    response = asyncio.run(
                        server._check_api_key(
                            duplicate,
                            {"api_key": {"scopes": []}},
                        )
                    )
                    assert response.status_code == 400, response.status_code
                    assert server._caller_identity(duplicate_scope) == "anonymous"

                    protected_scope = {
                        "type": "http",
                        "method": "GET",
                        "path": "/assets/private/secret.js",
                        "path_params": {"path": "assets/private/secret.js"},
                        "headers": [(b"cookie", b"sykit_session=present")],
                        "session": {"role": "admin"},
                    }
                    protected = asyncio.run(server._spa(Request(protected_scope)))
                    assert protected.headers["cache-control"] == "no-cache"

                    public_scope = {
                        "type": "http",
                        "method": "GET",
                        "path": "/assets/public.js",
                        "path_params": {"path": "assets/public.js"},
                        "headers": [],
                        "session": {},
                    }
                    public = asyncio.run(server._spa(Request(public_scope)))
                    assert public.headers["cache-control"] == (
                        "public, max-age=31536000, immutable"
                    )

                    messages = []

                    class Capture(logging.Handler):
                        def emit(self, record):
                            messages.append(record.getMessage())

                    handler = Capture()
                    server.LOGGER.addHandler(handler)
                    try:
                        server.CONFIG["session-store"] = ""
                        server.create_app()
                        server.CONFIG["session-store"] = "sqlite:warning-test.db"
                        server.create_app()
                    finally:
                        server.LOGGER.removeHandler(handler)
                    warnings = [
                        message
                        for message in messages
                        if "Anonymous clients" in message
                    ]
                    assert len(warnings) == 2, warnings
                    """
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["SYKIT_SESSION_SECRET"] = "s" * 40
            result = subprocess.run(
                [sys.executable, str(probe)],
                cwd=runtime,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
