"""Regression tests for the SyKit 0.3.0 security hardening update."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build as build_module
import package as package_module
from sykit import __version__

ROOT = Path(__file__).resolve().parents[1]

SERVER_ENDPOINTS = """
def login(session):
    session["role"] = "user"
    return {"ok": True}


def admin_tool(session):
    return {"ok": True}


def limited(session):
    return {"ok": True}


def _metadata(**overrides):
    metadata = {
        "kind": "expose",
        "method": "POST",
        "module": "endpoints",
        "file": "endpoints.py",
        "is_async": False,
        "permissions": {},
        "cors": [],
        "limits": {},
        "hidden": False,
        "token": None,
    }
    metadata.update(overrides)
    return metadata


SESSION = {"name": "session", "injected": True, "required": False}
ENDPOINTS = [
    {
        "metadata": _metadata(endpoint="login", name="login", parameters=[SESSION]),
        "function": login,
    },
    {
        "metadata": _metadata(
            endpoint="admin_tool",
            name="admin_tool",
            parameters=[SESSION],
            permissions={"Session": {"role": "admin"}},
            hidden=True,
            token="0123456789abcdef0123456789abcdef",
        ),
        "function": admin_tool,
    },
    {
        "metadata": _metadata(
            endpoint="limited",
            name="limited",
            parameters=[SESSION],
            limits={
                "per-session": None,
                "site-wide": None,
                # A wide window keeps the probe from flaking when it
                # straddles a window boundary.
                "per-client": {"requests": 2, "window": 3600},
                "per-worker": None,
            },
        ),
        "function": limited,
    },
]
"""

SERVER_PROBE = """
import asyncio
import json

import server


async def request(method, path, body=None, cookie=None, client=("127.0.0.1", 12345)):
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = [(b"host", b"127.0.0.1")]
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
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
        "client": client,
        "server": ("127.0.0.1", 8000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        messages.append(message)

    await server.app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    content = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return start["status"], dict(start["headers"]), content


def header(headers, name):
    lowered = name.lower().encode("latin-1")
    return next((v for k, v in headers.items() if k.lower() == lowered), None)


async def main():
    # Hidden endpoints stay indistinguishable from missing ones on every
    # method, including OPTIONS and TRACE (previously leaked via Allow).
    for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "HEAD"):
        body = {} if method in {"POST", "PUT", "PATCH"} else None
        status, _h, hidden = await request(method, "/api/admin_tool", body)
        m_status, _h, missing = await request(method, "/api/does_not_exist", body)
        assert status == m_status == 404, (method, status, m_status)
        assert hidden == missing, (method, hidden, missing)

    # The session cookie carries the configured Max-Age, and the configured
    # Content-Security-Policy header is emitted.
    status, headers, _ = await request("POST", "/api/login", {})
    assert status == 200, status
    cookie_header = header(headers, "set-cookie").decode("latin-1")
    assert "Max-Age=60" in cookie_header, cookie_header
    assert header(headers, "content-security-policy") == b"default-src 'self'", headers
    cookie = cookie_header.split(";")[0]

    # per-client limiting: the third call from the same address is rejected
    # even without a session cookie; a different address is unaffected.
    for _ in range(2):
        status, _h, _ = await request("POST", "/api/limited", {})
        assert status == 200, status
    status, _h, _ = await request("POST", "/api/limited", {})
    assert status == 429, status
    status, _h, _ = await request(
        "POST", "/api/limited", {}, client=("203.0.113.9", 9999)
    )
    assert status == 200, status
    # An existing session does not reset the client budget either.
    status, _h, _ = await request("POST", "/api/limited", {}, cookie=cookie)
    assert status == 429, status


asyncio.run(main())

run_arguments = {}


def capture_run(*_args, **kwargs):
    run_arguments.update(kwargs)


server.uvicorn.run = capture_run
server.run()
assert run_arguments["proxy_headers"] is False
"""


class SecurityRuntimeTests(unittest.TestCase):
    def test_hardened_runtime_behavior(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-security-test-") as directory:
            runtime = Path(directory)
            (runtime / "core").mkdir()
            (runtime / "app").mkdir()
            (runtime / "static").mkdir()
            shutil.copy2(ROOT / "files" / "server.py", runtime / "server.py")
            shutil.copy2(
                ROOT / "files" / "core" / "_limits.py",
                runtime / "core" / "_limits.py",
            )
            shutil.copy2(
                ROOT / "files" / "core" / "__init__.py",
                runtime / "core" / "__init__.py",
            )
            shutil.copy2(
                ROOT / "files" / "core" / "_sessions.py",
                runtime / "core" / "_sessions.py",
            )
            shutil.copy2(
                ROOT / "files" / "core" / "_apikeys.py",
                runtime / "core" / "_apikeys.py",
            )
            for name in ("_task_runtime.py", "_task_store.py", "_tasks.py"):
                shutil.copy2(ROOT / "files" / "core" / name, runtime / "core" / name)
            shutil.copytree(ROOT / "sykit", runtime / "app" / "sykit")
            (runtime / "config.json").write_text(
                json.dumps(
                    {
                        "endpoints": "/api/",
                        "allowed-hosts": ["127.0.0.1"],
                        "session-max-age": 60,
                        "content-security-policy": "default-src 'self'",
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "core" / "_endpoints.py").write_text(
                SERVER_ENDPOINTS, encoding="utf-8"
            )
            probe = runtime / "probe.py"
            probe.write_text(SERVER_PROBE, encoding="utf-8")
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


class PackageManifestSecurityTests(unittest.TestCase):
    def test_manifest_rejects_control_characters(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-manifest-test-") as directory:
            root = Path(directory)
            cases = (
                ("name", "bad\nname"),
                ("desc", "bad \x1b[2J desc"),
                ("credit", ["bad \x9b credit"]),
            )
            for key, value in cases:
                with self.subTest(key=key):
                    manifest = {"id": "control-probe", key: value}
                    (root / package_module.MANIFEST_NAME).write_text(
                        json.dumps(manifest), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(
                        package_module.PackageError, "control characters"
                    ):
                        package_module._load_manifest(root)


class BuildSecurityTests(unittest.TestCase):
    def test_dotenv_module_name_is_reserved(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="sykit-dotenv-module-test-"
        ) as directory:
            source_root = Path(directory)
            module = source_root / "dotenv.py"
            module.write_text("value = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(build_module.BuildError, "generated runtime"):
                build_module.validate_module_roots([module], source_root)

    def test_env_creation_updates_gitignore_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-env-test-") as directory:
            root = Path(directory)
            env_path = root / ".env"
            example_path = root / ".env.example"
            gitignore_path = root / ".gitignore"
            example_path.write_text("SYKIT_SESSION_SECRET=\n", encoding="utf-8")
            gitignore_path.write_text("built/", encoding="utf-8")

            with (
                mock.patch.object(build_module, "ENV_PATH", env_path),
                mock.patch.object(build_module, "ENV_EXAMPLE_PATH", example_path),
                mock.patch.object(build_module, "GITIGNORE_PATH", gitignore_path),
            ):
                build_module._ensure_env_files()
                build_module._ensure_env_files()

            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                example_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                gitignore_path.read_text(encoding="utf-8"), "built/\n.env\n"
            )
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_is_0_12_1(self) -> None:
        self.assertEqual(__version__, "0.12.1")


if __name__ == "__main__":
    unittest.main()
