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


class CORSPolicyTests(unittest.TestCase):
    def test_head_uses_get_endpoint_policy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-server-test-") as directory:
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
            shutil.copytree(ROOT / "sykit", runtime / "app" / "sykit")
            (runtime / "config.json").write_text(
                json.dumps(
                    {
                        "endpoints": "/api/",
                        "allowed-hosts": ["127.0.0.1"],
                        "default-CORS": ["https://default.example"],
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "core" / "_endpoints.py").write_text(
                textwrap.dedent(
                    """
                    def probe(session):
                        session["head_probe"] = True
                        return {"ok": True}


                    ENDPOINTS = [{
                        "metadata": {
                            "kind": "raw",
                            "method": "GET",
                            "endpoint": "probe",
                            "name": "probe",
                            "module": "probe",
                            "file": "probe.py",
                            "is_async": False,
                            "parameters": [{
                                "name": "session",
                                "injected": True,
                                "required": False,
                            }],
                            "permissions": {},
                            "cors": ["https://endpoint.example"],
                            "limits": {},
                        },
                        "function": probe,
                    }]
                    """
                ),
                encoding="utf-8",
            )
            probe = runtime / "probe.py"
            probe.write_text(
                textwrap.dedent(
                    """
                    import asyncio
                    import server


                    async def request(method, origin, requested_method=None):
                        headers = [
                            (b"host", b"127.0.0.1"),
                            (b"origin", origin.encode("ascii")),
                        ]
                        if requested_method:
                            headers.append((
                                b"access-control-request-method",
                                requested_method.encode("ascii"),
                            ))
                        scope = {
                            "type": "http",
                            "asgi": {"version": "3.0"},
                            "http_version": "1.1",
                            "method": method,
                            "scheme": "http",
                            "path": "/api/probe",
                            "raw_path": b"/api/probe",
                            "query_string": b"",
                            "root_path": "",
                            "headers": headers,
                            "client": ("127.0.0.1", 12345),
                            "server": ("127.0.0.1", 8000),
                        }
                        messages = []

                        async def receive():
                            return {"type": "http.request", "body": b"", "more_body": False}

                        async def send(message):
                            messages.append(message)

                        await server.app(scope, receive, send)
                        start = next(
                            message for message in messages
                            if message["type"] == "http.response.start"
                        )
                        return start["status"], dict(start["headers"])


                    async def main():
                        status, headers = await request("HEAD", "https://endpoint.example")
                        assert status == 200, (status, headers)
                        assert b"set-cookie" in headers, headers

                        status, _headers = await request("HEAD", "https://default.example")
                        assert status == 403, status

                        status, _headers = await request(
                            "OPTIONS", "https://endpoint.example", "HEAD"
                        )
                        assert status == 200, status

                        status, _headers = await request(
                            "OPTIONS", "https://default.example", "HEAD"
                        )
                        assert status == 403, status


                    asyncio.run(main())
                    """
                ),
                encoding="utf-8",
            )
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
