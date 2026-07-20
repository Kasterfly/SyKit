from __future__ import annotations

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


ENDPOINTS = r"""
from sykit import register_error_hook


ERRORS = []


async def capture_error(error, request):
    ERRORS.append((type(error).__name__, request.url.path))


register_error_hook(capture_error)


def boom(request):
    raise RuntimeError("private failure detail")


def login(session):
    session["user"] = "test-user"
    return {"ok": True}


def keyed():
    return {"ok": True}


def metadata(kind, method, endpoint, name, parameters, **extra):
    return {
        "kind": kind,
        "method": method,
        "endpoint": endpoint,
        "name": name,
        "module": "observability_endpoints",
        "file": "observability_endpoints.py",
        "parameters": parameters,
        "permissions": {},
        "cors": [],
        "limits": {},
        **extra,
    }


ENDPOINTS = [
    {
        "metadata": metadata(
            "raw",
            "GET",
            "boom",
            "boom",
            [{"name": "request", "injected": True, "required": False}],
        ),
        "function": boom,
    },
    {
        "metadata": metadata(
            "expose",
            "POST",
            "login",
            "login",
            [{"name": "session", "injected": True, "required": False}],
        ),
        "function": login,
    },
    {
        "metadata": metadata(
            "web_hook",
            "GET",
            "keyed",
            "keyed",
            [],
            api_key={"scopes": []},
        ),
        "function": keyed,
    },
]
"""


PROBE = r"""
import asyncio
import hashlib
import io
import json
import logging
from pathlib import Path

import server
import core._endpoints as endpoint_module


stream = io.StringIO()
handler = logging.StreamHandler(stream)
handler.setFormatter(logging.Formatter("%(message)s"))
server.LOGGER.handlers.clear()
server.LOGGER.addHandler(handler)
server.LOGGER.propagate = False


async def request(method, path, *, body=b"", headers=None, application=None):
    request_headers = [(b"host", b"127.0.0.1")]
    if body:
        request_headers.extend([
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ])
    request_headers.extend(headers or [])
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
        "headers": request_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    messages = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await (application or server.app)(scope, receive, send)
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    content = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], dict(start["headers"]), content


def json_access_records():
    records = []
    for line in stream.getvalue().splitlines():
        if line.startswith("{"):
            value = json.loads(line)
            if value.get("event") == "request":
                records.append(value)
    return records


async def main():
    root = Path(__file__).resolve().parent
    session_path = root / "sessions-test.db"
    key_path = root.parent / ".sykit-apikeys.sqlite3"
    assert not session_path.exists()
    assert not key_path.exists()

    raw_key = b"sykit_test_secret_value"
    status, headers, content = await request(
        "GET",
        "/status/live",
        headers=[(b"x-api-key", raw_key)],
    )
    assert status == 200, (status, content)
    assert json.loads(content) == {"status": "ok"}
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert not session_path.exists(), "liveness touched the session store"
    assert not key_path.exists(), "liveness touched the API key store"
    first = json_access_records()[-1]
    expected = hashlib.sha256(raw_key).hexdigest()[:12]
    assert first["caller"] == f"api_key:{expected}", first
    assert first["method"] == "GET" and first["status"] == 200, first
    assert first["path"] == "/status/live", first
    assert "sykit_test_secret_value" not in stream.getvalue()

    status, _headers, content = await request("GET", "/status/ready")
    assert status == 200, (status, content)
    readiness = json.loads(content)
    assert readiness == {
        "status": "ready",
        "checks": {"sessions": "ok", "api_keys": "ok"},
    }, readiness
    assert session_path.is_file(), "readiness did not check the session store"
    assert key_path.is_file(), "readiness did not check the API key store"

    status, _headers, content = await request("GET", "/api/boom")
    assert status == 500, (status, content)
    assert json.loads(content) == {"error": "The endpoint failed."}
    assert endpoint_module.ERRORS == [("RuntimeError", "/api/boom")]
    assert json_access_records()[-1]["status"] == 500

    status, _headers, content = await request("POST", "/api/login", body=b"{}")
    assert status == 200, (status, content)
    assert json_access_records()[-1]["caller"] == "session"

    class BrokenStore:
        def __init__(self):
            self.calls = 0

        def load(self, _session_id):
            self.calls += 1
            raise OSError("offline")

    async def should_not_run(_scope, _receive, _send):
        raise AssertionError("health middleware called the application")

    broken = BrokenStore()
    health = server.HealthMiddleware(
        should_not_run,
        "/live",
        "/ready",
        broken,
        None,
    )
    status, _headers, content = await request(
        "GET", "/live", application=health
    )
    assert status == 200 and broken.calls == 0, (status, broken.calls, content)
    status, _headers, content = await request(
        "GET", "/ready", application=health
    )
    assert status == 503 and broken.calls == 1, (status, broken.calls, content)
    assert json.loads(content)["checks"] == {"sessions": "unavailable"}

    server.LOG_FORMAT = "text"
    before = len(stream.getvalue().splitlines())
    status, _headers, _content = await request("HEAD", "/status/live")
    assert status == 200
    new_lines = stream.getvalue().splitlines()[before:]
    assert any(
        line.startswith("request method=HEAD")
        and 'path="/status/live"' in line
        and "caller=anonymous" in line
        for line in new_lines
    ), new_lines


asyncio.run(main())
"""


class ObservabilityRuntimeTests(unittest.TestCase):
    def test_health_logging_and_error_hook(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-observability-test-") as folder:
            runtime = Path(folder) / "built"
            runtime.mkdir()
            (runtime / "app").mkdir()
            (runtime / "static").mkdir()
            shutil.copy2(ROOT / "files" / "server.py", runtime / "server.py")
            shutil.copytree(ROOT / "files" / "core", runtime / "core")
            shutil.copytree(ROOT / "sykit", runtime / "app" / "sykit")
            (runtime / "core" / "_endpoints.py").write_text(
                textwrap.dedent(ENDPOINTS),
                encoding="utf-8",
            )
            (runtime / "config.json").write_text(
                json.dumps(
                    {
                        "endpoints": "/api/",
                        "allowed-hosts": ["127.0.0.1"],
                        "health-path": "/status/live",
                        "readiness-path": "/status/ready",
                        "log-format": "json",
                        "log-level": "INFO",
                        "session-store": "sqlite:sessions-test.db",
                    }
                ),
                encoding="utf-8",
            )
            probe = runtime / "probe.py"
            probe.write_text(textwrap.dedent(PROBE), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
