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

import build

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
ADMIN_PERMS = {"Session": {"role": "admin"}}


def endpoint(
    name: str,
    *,
    kind: str = "expose",
    hidden: bool = False,
    permissions: dict | None = None,
    token: str | None = None,
) -> build.EndpointInfo:
    return build.EndpointInfo(
        kind=kind,
        method="POST" if kind == "expose" else "GET",
        endpoint=name.lower(),
        function=name,
        module="endpoints",
        file="endpoints.py",
        is_async=False,
        parameters=(),
        permissions={} if permissions is None else permissions,
        cors=(),
        limits={},
        hidden=hidden,
        token=token,
    )


class HiddenParserTests(unittest.TestCase):
    def _parse(self, body: str) -> list[build.EndpointInfo]:
        with tempfile.TemporaryDirectory(prefix="sykit-hidden-parser-") as directory:
            root = Path(directory)
            source = root / "endpoints.py"
            source.write_text(textwrap.dedent(body), encoding="utf-8")
            return build.parse_decorators(source, root)

    def test_hidden_flag_is_detected(self) -> None:
        results = self._parse(
            """
            from sykit import expose, hidden, perms

            @expose("admin_tool")
            @perms({"Session": {"role": "admin"}})
            @hidden
            def admin_tool():
                return {"ok": True}
            """
        )
        self.assertTrue(results[0].hidden)

    def test_endpoints_default_to_visible(self) -> None:
        results = self._parse(
            """
            from sykit import expose

            @expose("ping")
            def ping():
                return {"ok": True}
            """
        )
        self.assertFalse(results[0].hidden)

    def test_hidden_rejects_arguments(self) -> None:
        with self.assertRaisesRegex(build.BuildError, "without arguments"):
            self._parse(
                """
                from sykit import expose, hidden

                @expose("admin_tool")
                @hidden()
                def admin_tool():
                    return {"ok": True}
                """
            )

    def test_hidden_rejects_duplicates(self) -> None:
        with self.assertRaisesRegex(build.BuildError, "only one @hidden"):
            self._parse(
                """
                from sykit import expose, hidden

                @expose("admin_tool")
                @hidden
                @hidden
                def admin_tool():
                    return {"ok": True}
                """
            )

    def test_hidden_rejects_cors(self) -> None:
        with self.assertRaisesRegex(build.BuildError, "@cors"):
            self._parse(
                """
                from sykit import cors, expose, hidden

                @expose("admin_tool")
                @cors(["https://example.com"])
                @hidden
                def admin_tool():
                    return {"ok": True}
                """
            )

    def test_manifest_endpoint_path_is_reserved(self) -> None:
        with self.assertRaisesRegex(build.BuildError, "reserved by SyKit"):
            self._parse(
                """
                from sykit import expose

                @expose("__sykit_manifest__")
                def manifest():
                    return {"ok": True}
                """
            )

    def test_hidden_api_export_is_reserved(self) -> None:
        with self.assertRaisesRegex(build.BuildError, "generated \\$python client"):
            self._parse(
                """
                from sykit import expose

                @expose("helper")
                def hidden_api():
                    return {"ok": True}
                """
            )


class HiddenValidationTests(unittest.TestCase):
    def test_hidden_requires_session_permissions(self) -> None:
        with self.assertRaisesRegex(build.BuildError, "session permissions"):
            build.validate_hidden_endpoints([endpoint("AdminTool", hidden=True)])

    def test_hidden_with_permissions_passes(self) -> None:
        build.validate_hidden_endpoints(
            [endpoint("AdminTool", hidden=True, permissions=ADMIN_PERMS)]
        )

    def test_tokens_only_assigned_to_hidden_client_endpoints(self) -> None:
        endpoints = build.assign_hidden_tokens(
            [
                endpoint("Visible"),
                endpoint("AdminTool", hidden=True, permissions=ADMIN_PERMS),
                endpoint(
                    "hook",
                    kind="web_hook",
                    hidden=True,
                    permissions=ADMIN_PERMS,
                ),
            ]
        )
        self.assertIsNone(endpoints[0].token)
        self.assertTrue(endpoints[1].token)
        self.assertIsNone(endpoints[2].token)


class HiddenClientModuleTests(unittest.TestCase):
    def test_hidden_wrapper_leaks_no_route(self) -> None:
        endpoints = build.assign_hidden_tokens(
            [
                endpoint("AdminTool", hidden=True, permissions=ADMIN_PERMS),
                endpoint("Visible"),
            ]
        )
        module = build.generate_client_module({}, endpoints)
        self.assertNotIn("admintool", module)
        self.assertIn(endpoints[0].token or "", module)
        self.assertIn("$sykitHiddenCall", module)
        self.assertIn("hidden_api", module)
        self.assertIn('"visible"', module)

    def test_hidden_runtime_only_emitted_when_needed(self) -> None:
        module = build.generate_client_module({}, [endpoint("Visible")])
        self.assertNotIn("$sykitHiddenCall", module)
        self.assertNotIn("hidden_api", module)
        self.assertNotIn("__sykit_manifest__", module)

    def test_hidden_without_token_is_an_error(self) -> None:
        with self.assertRaisesRegex(build.BuildError, "token"):
            build.generate_client_module(
                {},
                [endpoint("AdminTool", hidden=True, permissions=ADMIN_PERMS)],
            )


HIDDEN_TOKEN = "0123456789abcdef0123456789abcdef"

SERVER_ENDPOINTS = """
def login(role, session):
    session["role"] = role
    return {"ok": True}


def admin_tool(value, session):
    return {"ok": value}


def visible_admin(session):
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
        "metadata": _metadata(
            endpoint="login",
            name="login",
            parameters=[
                {"name": "role", "injected": False, "required": True},
                SESSION,
            ],
        ),
        "function": login,
    },
    {
        "metadata": _metadata(
            endpoint="admin_tool",
            name="admin_tool",
            parameters=[
                {"name": "value", "injected": False, "required": True},
                SESSION,
            ],
            permissions={"Session": {"role": "admin"}},
            hidden=True,
            token="__TOKEN__",
        ),
        "function": admin_tool,
    },
    {
        "metadata": _metadata(
            endpoint="visible_admin",
            name="visible_admin",
            parameters=[SESSION],
            permissions={"Session": {"role": "admin"}},
        ),
        "function": visible_admin,
    },
]
""".replace("__TOKEN__", HIDDEN_TOKEN)

SERVER_PROBE = """
import asyncio
import json

import server

TOKEN = "__TOKEN__"


async def request(method, path, body=None, cookie=None):
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
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

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
    return start["status"], dict(start["headers"]), content


async def login(role):
    status, headers, _content = await request("POST", "/api/login", {"role": role})
    assert status == 200, status
    cookie = headers[b"set-cookie"].decode("latin-1").split(";")[0]
    assert cookie.startswith("sykit_session="), cookie
    return cookie


async def main():
    # A hidden endpoint denies exactly like a nonexistent one, on any method.
    status, _headers, denied = await request("POST", "/api/admin_tool", {"value": 1})
    missing_status, _headers, missing = await request(
        "POST", "/api/does_not_exist", {"value": 1}
    )
    assert status == 404 and missing_status == 404, (status, missing_status)
    assert denied == missing, (denied, missing)
    status, _headers, wrong_method = await request("GET", "/api/admin_tool")
    assert status == 404, status
    assert wrong_method == missing, (wrong_method, missing)

    # The manifest is empty without an authorized session.
    status, _headers, content = await request("POST", "/api/__sykit_manifest__", {})
    assert status == 200 and json.loads(content) == {}, (status, content)

    wrong_cookie = await login("user")
    status, _headers, content = await request(
        "POST", "/api/admin_tool", {"value": 1}, cookie=wrong_cookie
    )
    assert status == 404, status
    status, _headers, content = await request(
        "POST", "/api/__sykit_manifest__", {}, cookie=wrong_cookie
    )
    assert status == 200 and json.loads(content) == {}, (status, content)

    # An authorized session sees the manifest entry and can call the endpoint.
    admin_cookie = await login("admin")
    status, _headers, content = await request(
        "POST", "/api/__sykit_manifest__", {}, cookie=admin_cookie
    )
    assert status == 200, status
    manifest = json.loads(content)
    assert manifest == {
        TOKEN: {"e": "admin_tool", "m": "POST", "p": ["value"]}
    }, manifest
    status, _headers, content = await request(
        "POST", "/api/admin_tool", {"value": 5}, cookie=admin_cookie
    )
    assert status == 200 and json.loads(content) == {"ok": 5}, (status, content)

    # Visible endpoints keep the 401/403 contract.
    status, _headers, _content = await request("POST", "/api/visible_admin", {})
    assert status == 401, status
    status, _headers, _content = await request(
        "POST", "/api/visible_admin", {}, cookie=wrong_cookie
    )
    assert status == 403, status


asyncio.run(main())
""".replace("__TOKEN__", HIDDEN_TOKEN)


class HiddenServerTests(unittest.TestCase):
    def test_hidden_endpoint_is_indistinguishable_from_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-hidden-server-") as directory:
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
                json.dumps({"endpoints": "/api/", "allowed-hosts": ["127.0.0.1"]}),
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


@unittest.skipUnless(NODE, "Node.js is required for generated-client tests")
class HiddenClientRuntimeTests(unittest.TestCase):
    def test_hidden_wrapper_conceals_then_resolves(self) -> None:
        endpoints = build.assign_hidden_tokens(
            [endpoint("AdminTool", hidden=True, permissions=ADMIN_PERMS)]
        )
        token = endpoints[0].token
        module = build.generate_client_module({}, endpoints)
        manifest = {token: {"e": "admin_tool", "m": "POST", "p": []}}
        with tempfile.TemporaryDirectory(prefix="sykit-hidden-client-") as directory:
            root = Path(directory)
            module_path = root / "client.mjs"
            module_path.write_text(module, encoding="utf-8")
            runner = root / "runner.mjs"
            runner.write_text(
                "let authorized = false;\n"
                "const calls = [];\n"
                "globalThis.fetch = async (url) => {\n"
                "  calls.push(url);\n"
                "  const respond = (data) => ({\n"
                "    ok: true, status: 200,\n"
                "    text: async () => JSON.stringify(data),\n"
                "  });\n"
                '  if (url === "/api/__sykit_manifest__") {\n'
                f"    return respond(authorized ? {json.dumps(manifest)} : {{}});\n"
                "  }\n"
                '  if (url === "/api/admin_tool") return respond({ ok: true });\n'
                "  return {\n"
                "    ok: false, status: 404,\n"
                '    text: async () => \'{"error":"Endpoint not found."}\',\n'
                "  };\n"
                "};\n"
                f"const client = await import({json.dumps(module_path.as_uri())});\n"
                "let threw = null;\n"
                "try { await client.AdminTool(); } catch (error) { threw = error; }\n"
                'if (threw?.name !== "SyKitError" || threw.status !== 404) {\n'
                '  throw new Error("expected a hidden 404");\n'
                "}\n"
                'if (calls.some((url) => url.includes("admin_tool"))) {\n'
                '  throw new Error("unauthorized call leaked the route");\n'
                "}\n"
                "authorized = true;\n"
                "const result = await client.AdminTool();\n"
                'if (result?.ok !== true) throw new Error("authorized call failed");\n'
                'if (calls[calls.length - 1] !== "/api/admin_tool") {\n'
                '  throw new Error("expected an endpoint fetch");\n'
                "}\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [NODE, str(runner)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
