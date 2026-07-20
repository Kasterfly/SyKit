from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INDEX_BODY = b"<html>sykit-shell</html>"
SECRET_BODY = b"top-secret-file"
PUBLIC_BODY = b"public-file"

ENDPOINTS_MODULE = """
def do_login(session):
    from sykit import auth
    auth.login({"role": "admin"})
    return {"ok": True}


def do_logout(session):
    from sykit import auth
    auth.logout()
    return {"ok": True}


def whoami(session):
    return {"role": session.get("role", "")}


def _meta(kind, method, endpoint, name):
    return {
        "kind": kind,
        "method": method,
        "endpoint": endpoint,
        "name": name,
        "module": "probe",
        "file": "probe.py",
        "is_async": False,
        "parameters": [{"name": "session", "injected": True, "required": False}],
        "permissions": {},
        "cors": [],
        "limits": {},
    }


ENDPOINTS = [
    {"metadata": _meta("expose", "POST", "login", "login"), "function": do_login},
    {"metadata": _meta("expose", "POST", "logout", "logout"), "function": do_logout},
    {"metadata": _meta("raw", "GET", "whoami", "whoami"), "function": whoami},
]
"""

PROBE_COMMON = """
import asyncio
import base64
import json
import sqlite3

import server


async def request(method, path, cookie=None, body=b""):
    headers = [(b"host", b"127.0.0.1")]
    if cookie is not None:
        headers.append((b"cookie", ("sykit_session=" + cookie).encode("ascii")))
    if body:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    if method in {"GET", "HEAD"}:
        scope["path_params"] = {}
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

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
    return start["status"], start["headers"], content


def cookie_from(headers):
    for name, value in headers:
        if name.lower() == b"set-cookie":
            pair = value.decode("latin-1").split(";", 1)[0]
            return pair.split("=", 1)[1]
    return None


def session_rows():
    connection = sqlite3.connect("sessions-test.db")
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM sykit_sessions"
        ).fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        connection.close()
"""

STORE_PROBE = (
    PROBE_COMMON
    + """
async def main():
    secret = b"top-secret-file"
    shell = b"<html>sykit-shell</html>"

    status, _headers, content = await request("GET", "/admin/secret.txt")
    assert status == 200 and content == shell, (status, content)

    status, _headers, content = await request("GET", "/nothing/here")
    assert status == 200 and content == shell, (status, content)

    status, _headers, content = await request("GET", "/public.txt")
    assert status == 200 and content == b"public-file", (status, content)

    status, _headers, content = await request("GET", "/ADMIN/secret.txt")
    assert content != secret, "case alias must not leak a protected file"

    status, headers, content = await request("POST", "/api/login", body=b"{}")
    assert status == 200, (status, content)
    cookie = cookie_from(headers)
    assert cookie and cookie != "null", headers
    assert session_rows() == 1, session_rows()

    status, _headers, content = await request(
        "GET", "/admin/secret.txt", cookie=cookie
    )
    assert status == 200 and content == secret, (status, content)

    status, headers, _content = await request(
        "POST", "/api/login", cookie=cookie, body=b"{}"
    )
    rotated = cookie_from(headers)
    assert rotated and rotated != cookie, "login must rotate the session id"
    assert session_rows() == 1, session_rows()

    status, _headers, content = await request(
        "GET", "/admin/secret.txt", cookie=cookie
    )
    assert content == shell, "a rotated-away session id must be dead"

    status, headers, _content = await request(
        "POST", "/api/logout", cookie=rotated, body=b"{}"
    )
    assert cookie_from(headers) == "null", headers
    assert session_rows() == 0, session_rows()

    status, _headers, content = await request(
        "GET", "/admin/secret.txt", cookie=rotated
    )
    assert content == shell, "logout must revoke the session server-side"


asyncio.run(main())
"""
)

COOKIE_PROBE = (
    PROBE_COMMON
    + """
async def main():
    secret = b"top-secret-file"
    shell = b"<html>sykit-shell</html>"

    status, headers, content = await request("POST", "/api/login", body=b"{}")
    assert status == 200, (status, content)
    cookie = cookie_from(headers)
    assert cookie and cookie != "null", headers

    payload = cookie.split(".")[0]
    data = json.loads(base64.b64decode(payload))
    assert data.get("role") == "admin", data
    assert not any(key.startswith("__sykit_") for key in data), data

    status, _headers, content = await request("GET", "/api/whoami", cookie=cookie)
    assert json.loads(content)["role"] == "admin", content

    status, _headers, content = await request("GET", "/admin/secret.txt")
    assert content == shell, (status, content)

    status, _headers, content = await request(
        "GET", "/admin/secret.txt", cookie=cookie
    )
    assert status == 200 and content == secret, (status, content)

    status, headers, _content = await request(
        "POST", "/api/logout", cookie=cookie, body=b"{}"
    )
    assert cookie_from(headers) == "null", headers

    status, _headers, content = await request("GET", "/api/whoami", cookie=cookie)
    assert json.loads(content)["role"] == "admin", (
        "signed-cookie sessions cannot be revoked server-side; this pins "
        "the documented limitation the session-store setting lifts"
    )


asyncio.run(main())
"""
)

BAD_CONFIG_PROBE = """
try:
    import server  # noqa: F401
except RuntimeError as error:
    assert "page-perms" in str(error), error
    raise SystemExit(0)
raise SystemExit(1)
"""


def _load_sessions_module():
    spec = importlib.util.spec_from_file_location(
        "sykit_test_sessions", ROOT / "files" / "core" / "_sessions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_runtime(directory: Path, config: dict) -> None:
    (directory / "core").mkdir()
    (directory / "app").mkdir()
    (directory / "static" / "admin").mkdir(parents=True)
    shutil.copy2(ROOT / "files" / "server.py", directory / "server.py")
    for name in (
        "__init__.py",
        "_apikeys.py",
        "_limits.py",
        "_sessions.py",
        "_task_runtime.py",
        "_task_store.py",
        "_tasks.py",
    ):
        shutil.copy2(ROOT / "files" / "core" / name, directory / "core" / name)
    shutil.copytree(
        ROOT / "sykit",
        directory / "app" / "sykit",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (directory / "core" / "_endpoints.py").write_text(
        ENDPOINTS_MODULE, encoding="utf-8"
    )
    (directory / "static" / "index.html").write_bytes(INDEX_BODY)
    (directory / "static" / "public.txt").write_bytes(PUBLIC_BODY)
    (directory / "static" / "admin" / "secret.txt").write_bytes(SECRET_BODY)


def _run_probe(directory: Path, script: str) -> subprocess.CompletedProcess[str]:
    probe = directory / "probe.py"
    probe.write_text(script, encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["SYKIT_SESSION_SECRET"] = (
        "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
    )
    return subprocess.run(
        [sys.executable, str(probe)],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


BASE_CONFIG = {
    "endpoints": "/api/",
    "allowed-hosts": ["127.0.0.1"],
    "page-perms": {"/admin": {"Session": {"role": "admin"}}},
}


class LoginAccessAppTests(unittest.TestCase):
    def test_store_mode_gates_pages_and_revokes_sessions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-login-test-") as directory:
            runtime = Path(directory)
            config = dict(BASE_CONFIG)
            config["session-store"] = "sqlite:sessions-test.db"
            _build_runtime(runtime, config)
            result = _run_probe(runtime, STORE_PROBE)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_cookie_mode_still_signs_sessions_and_gates_pages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-login-test-") as directory:
            runtime = Path(directory)
            _build_runtime(runtime, dict(BASE_CONFIG))
            result = _run_probe(runtime, COOKIE_PROBE)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_invalid_page_perms_refuse_to_start(self) -> None:
        bad_configs = [
            {"page-perms": {"/": {"Session": {"role": "admin"}}}},
            {"page-perms": {"/admin": {"Session": {}}}},
            {"page-perms": {"/admin": {"Other": {"role": "admin"}}}},
            {"page-perms": {"/api/tool": {"Session": {"role": "admin"}}}},
            {
                "page-perms": {
                    "/admin": {"Session": {"role": "a"}},
                    "admin/": {"Session": {"role": "b"}},
                }
            },
        ]
        for bad in bad_configs:
            config = {"endpoints": "/api/", "allowed-hosts": ["127.0.0.1"], **bad}
            with tempfile.TemporaryDirectory(prefix="sykit-login-test-") as directory:
                runtime = Path(directory)
                _build_runtime(runtime, config)
                result = _run_probe(runtime, BAD_CONFIG_PROBE)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{bad}: {result.stderr or result.stdout}",
                )


class AuthHelperTests(unittest.TestCase):
    def test_hash_and_verify_roundtrip(self) -> None:
        from sykit import auth

        stored = auth.hash_password("correct horse battery staple")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(auth.verify_password("correct horse battery staple", stored))
        self.assertFalse(auth.verify_password("wrong password", stored))
        self.assertNotEqual(stored, auth.hash_password("correct horse battery staple"))

    def test_verify_rejects_unusable_passwords_quietly(self) -> None:
        from sykit import auth

        stored = auth.hash_password("a real password")
        self.assertFalse(auth.verify_password("", stored))
        self.assertFalse(auth.verify_password(None, stored))
        self.assertFalse(auth.verify_password("x" * 2000, stored))

    def test_hash_rejects_invalid_passwords(self) -> None:
        from sykit import auth

        for bad in ("", None, 123, "x" * 2000):
            with self.assertRaises(auth.AuthError):
                auth.hash_password(bad)

    def test_verify_raises_on_malformed_stored_values(self) -> None:
        from sykit import auth

        for bad in (
            None,
            "",
            "plaintext",
            "scrypt$16384$8$1$notbase64!!$notbase64!!",
            "scrypt$3$8$1$YWJj$YWJj",
            "md5$1$1$1$YWJj$YWJj",
        ):
            with self.assertRaises(auth.AuthError):
                auth.verify_password("password", bad)

    def test_login_replaces_session_and_marks_rotation(self) -> None:
        from sykit import auth, utils

        token = utils._bind_session({"stale": "value"})
        try:
            auth.login({"role": "admin", "user": "ada"})
            session = utils._session()
            self.assertEqual(session["role"], "admin")
            self.assertEqual(session["user"], "ada")
            self.assertNotIn("stale", session)
            self.assertTrue(session["__sykit_rotate"])
            self.assertEqual(utils.get_session(), {"role": "admin", "user": "ada"})
            auth.logout()
            self.assertEqual(utils._session(), {})
        finally:
            utils._reset_session(token)

    def test_login_validates_claims(self) -> None:
        from sykit import auth, utils

        token = utils._bind_session({})
        try:
            for bad in ({}, "admin", {"": "x"}, {1: "x"}, {"__sykit_x": 1}):
                with self.assertRaises(auth.AuthError):
                    auth.login(bad)
            with self.assertRaises(auth.AuthError):
                auth.login({"role": object()})
        finally:
            utils._reset_session(token)

    def test_login_outside_a_request_fails(self) -> None:
        from sykit import auth

        with self.assertRaises(RuntimeError):
            auth.login({"role": "admin"})
        with self.assertRaises(RuntimeError):
            auth.logout()


class SqliteSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = _load_sessions_module()
        self._directory = tempfile.TemporaryDirectory(prefix="sykit-store-test-")
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "sessions.db"
        self.store = self.sessions.SqliteSessionStore(self.path)

    def _expires(self, session_id: str) -> int:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(
                "SELECT expires FROM sykit_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        finally:
            connection.close()

    def test_save_load_delete_roundtrip(self) -> None:
        self.assertIsNone(self.store.load("missing"))
        self.store.save("sid-1", {"role": "admin"}, 600)
        self.assertEqual(self.store.load("sid-1"), {"role": "admin"})
        self.store.save("sid-1", {"role": "user"}, 600)
        self.assertEqual(self.store.load("sid-1"), {"role": "user"})
        self.store.delete("sid-1")
        self.assertIsNone(self.store.load("sid-1"))
        self.store.delete("sid-1")

    def test_expired_sessions_vanish(self) -> None:
        self.store.save("sid-2", {"role": "admin"}, 600)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE sykit_sessions SET expires = 1 WHERE session_id = ?",
                ("sid-2",),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(self.store.load("sid-2"))
        connection = sqlite3.connect(self.path)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM sykit_sessions"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_touch_extends_expiry(self) -> None:
        self.store.save("sid-3", {"role": "admin"}, 10)
        before = self._expires("sid-3")
        self.store.touch("sid-3", 5000)
        self.assertGreater(self._expires("sid-3"), before)


class ResolveStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = _load_sessions_module()
        self.root = Path("unused-root")

    def test_empty_specs_mean_cookie_mode(self) -> None:
        self.assertIsNone(self.sessions.resolve_store("", self.root))
        self.assertIsNone(self.sessions.resolve_store("   ", self.root))
        self.assertIsNone(self.sessions.resolve_store(None, self.root))

    def test_sqlite_specs(self) -> None:
        store = self.sessions.resolve_store("sqlite", self.root)
        self.assertEqual(store.database_path, self.root / ".sykit-sessions.sqlite3")
        store = self.sessions.resolve_store("sqlite:custom/sess.db", self.root)
        self.assertEqual(store.database_path, self.root / "custom" / "sess.db")

    def test_invalid_specs_fail_loudly(self) -> None:
        with self.assertRaises(RuntimeError):
            self.sessions.resolve_store(123, self.root)
        with self.assertRaises(RuntimeError):
            self.sessions.resolve_store("not a scheme:x", self.root)
        with self.assertRaisesRegex(RuntimeError, "_store_nope"):
            self.sessions.resolve_store("nope:x", self.root)

    def test_store_packages_resolve_by_module_convention(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-store-pkg-") as directory:
            package = Path(directory) / "core"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "_store_fake.py").write_text(
                textwrap.dedent(
                    """
                    class FakeStore:
                        def __init__(self, target):
                            self.target = target

                        def load(self, session_id):
                            return None

                        def save(self, session_id, data, max_age):
                            pass

                        def touch(self, session_id, max_age):
                            pass

                        def delete(self, session_id):
                            pass


                    def create(target):
                        return FakeStore(target)
                    """
                ),
                encoding="utf-8",
            )
            (package / "_store_partial.py").write_text(
                "def create(target):\n    return object()\n",
                encoding="utf-8",
            )
            sys.path.insert(0, directory)
            try:
                store = self.sessions.resolve_store("fake:my-target", self.root)
                self.assertEqual(store.target, "my-target")
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    self.sessions.resolve_store("partial:x", self.root)
            finally:
                sys.path.remove(directory)
                for name in [
                    name
                    for name in sys.modules
                    if name == "core" or name.startswith("core.")
                ]:
                    del sys.modules[name]


if __name__ == "__main__":
    unittest.main()
