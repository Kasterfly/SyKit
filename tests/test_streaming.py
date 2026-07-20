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
    hidden: bool = False,
    token: str | None = None,
    parameters: tuple[build.ParameterInfo, ...] = (),
) -> build.EndpointInfo:
    return build.EndpointInfo(
        kind="sse",
        method="GET",
        endpoint=name.replace("_", "-"),
        function=name,
        module="streams",
        file="streams.py",
        is_async=True,
        parameters=parameters,
        permissions=ADMIN_PERMS if hidden else {},
        cors=(),
        limits={},
        hidden=hidden,
        token=token,
    )


class StreamingParserTests(unittest.TestCase):
    def _parse(self, body: str) -> list[build.EndpointInfo]:
        with tempfile.TemporaryDirectory(prefix="sykit-sse-parser-") as directory:
            root = Path(directory)
            source = root / "streams.py"
            source.write_text(textwrap.dedent(body), encoding="utf-8")
            return build.parse_decorators(source, root)

    def test_sse_requires_an_async_generator(self) -> None:
        results = self._parse(
            """
            from sykit import sse

            @sse("updates")
            async def updates(topic="all", session=None):
                yield {"topic": topic, "role": session.get("role")}
            """
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "sse")
        self.assertEqual(results[0].method, "GET")
        self.assertTrue(results[0].is_async)
        self.assertEqual(
            [item.name for item in results[0].client_parameters],
            ["topic"],
        )

        invalid = (
            """
            from sykit import sse

            @sse("sync")
            def sync():
                yield 1
            """,
            """
            from sykit import sse

            @sse("coroutine")
            async def coroutine():
                return 1
            """,
            """
            from sykit import sse

            @sse("nested")
            async def nested():
                async def inner():
                    yield 1
                return inner()
            """,
        )
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaisesRegex(build.BuildError, "async generator"):
                    self._parse(source)

    def test_sse_rejects_raw_request_injection_and_uploads(self) -> None:
        with self.assertRaisesRegex(build.BuildError, "cannot inject request"):
            self._parse(
                """
                from sykit import sse

                @sse("request")
                async def request_stream(request):
                    yield request.url.path
                """
            )
        with self.assertRaisesRegex(build.BuildError, "only supported on @expose"):
            self._parse(
                """
                from sykit import Upload, sse

                @sse("upload")
                async def upload_stream(file: Upload):
                    yield file.size
                """
            )

    def test_sse_keeps_endpoint_guards(self) -> None:
        result = self._parse(
            """
            from sykit import cors, limits, perms, sse

            @sse("guarded")
            @perms({"Session": {"role": "admin"}})
            @cors(["https://app.example"])
            @limits({"per-client": "2m"})
            async def guarded():
                yield {"ok": True}
            """
        )[0]
        self.assertEqual(result.permissions, ADMIN_PERMS)
        self.assertEqual(result.cors, ("https://app.example",))
        self.assertEqual(
            result.limits["per-client"],
            {"requests": 2, "window": 60},
        )


@unittest.skipUnless(NODE, "Node.js is required for generated-client tests")
class StreamingClientTests(unittest.TestCase):
    def test_client_parses_cancels_errors_and_resolves_hidden_streams(self) -> None:
        topic = build.ParameterInfo("topic", injected=False, required=True)
        value = build.ParameterInfo("value", injected=False, required=True)
        hidden = endpoint(
            "secret_feed",
            hidden=True,
            token="0123456789abcdef0123456789abcdef",
            parameters=(value,),
        )
        module = build.generate_client_module(
            {},
            [endpoint("events", parameters=(topic,)), hidden],
        )
        self.assertNotIn("secret-feed", module)

        with tempfile.TemporaryDirectory(prefix="sykit-sse-client-") as directory:
            root = Path(directory)
            module_path = root / "client.mjs"
            module_path.write_text(module, encoding="utf-8")
            runner = root / "runner.mjs"
            script = r"""
const calls = [];
let cancelled = false;
let authorized = false;
const encoder = new TextEncoder();
const streams = [
  [': heartbeat\r', '\ndata: {"number":1}\r\n\r', '\nevent: custom\ndata: {"number":\ndata: 2}\n\n'],
  ['data: {"number":3}\n\ndata: {"number":4}\n\n'],
  ['event: sykit-error\ndata: {"error":"The stream failed."}\n\n'],
  ['data: {"secret":"ok"}\n\n'],
];

function streamResponse(chunks) {
  let index = 0;
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'text/event-stream; charset=utf-8' },
    body: {
      getReader() {
        return {
          async read() {
            if (index >= chunks.length) return { done: true, value: undefined };
            return { done: false, value: encoder.encode(chunks[index++]) };
          },
          async cancel() { cancelled = true; },
          releaseLock() {},
        };
      },
    },
  };
}

globalThis.fetch = async (url, options = {}) => {
  calls.push({ url, options });
  if (url === '/api/__sykit_manifest__') {
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify(authorized ? {
        __TOKEN__: { e: 'secret-feed', m: 'GET', p: ['value'], s: true },
      } : {}),
    };
  }
  const chunks = streams.shift();
  if (!chunks) throw new Error(`unexpected fetch: ${url}`);
  return streamResponse(chunks);
};

const client = await import(__MODULE__);
const values = [];
for await (const item of client.events('a b')) values.push(item);
if (JSON.stringify(values) !== JSON.stringify([{ number: 1 }, { number: 2 }])) {
  throw new Error(`bad values: ${JSON.stringify(values)}`);
}
const query = new URL(`http://local${calls[0].url}`).searchParams;
if (query.get('topic') !== '"a b"') throw new Error('query encoding failed');
if (calls[0].options.credentials !== 'include') throw new Error('credentials missing');

for await (const item of client.events('cancel')) {
  if (item.number !== 3) throw new Error('bad cancellation value');
  break;
}
if (!cancelled) throw new Error('breaking iteration did not cancel the reader');

let streamError = null;
try {
  for await (const item of client.events('error')) void item;
} catch (error) {
  streamError = error;
}
if (streamError?.name !== 'SyKitError' || streamError.status !== 500) {
  throw new Error('stream error was not normalized');
}

let hiddenError = null;
try {
  for await (const item of client.secret_feed(7)) void item;
} catch (error) {
  hiddenError = error;
}
if (hiddenError?.name !== 'SyKitError' || hiddenError.status !== 404) {
  throw new Error('unauthorized hidden stream did not return 404');
}
if (calls.some((call) => call.url.startsWith('/api/secret-feed'))) {
  throw new Error('unauthorized hidden stream leaked the route');
}

authorized = true;
const secrets = [];
for await (const item of client.secret_feed(7)) secrets.push(item);
if (secrets[0]?.secret !== 'ok') throw new Error('hidden stream failed');
if (!calls.some((call) => call.url === '/api/secret-feed?value=7')) {
  throw new Error('hidden stream route was not resolved');
}
"""
            script = script.replace(
                "__MODULE__", json.dumps(module_path.as_uri())
            ).replace("__TOKEN__", json.dumps(hidden.token))
            runner.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [NODE, str(runner)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


SERVER_ENDPOINTS = r"""
import asyncio

from sykit import register_error_hook, update_session


ERRORS = []
IDLE_CLOSED = False


async def capture(error, request):
    ERRORS.append((type(error).__name__, request.url.path))


register_error_hook(capture)


def login(role, session):
    session["role"] = role
    return {"ok": True}


async def numbers(count, session):
    for number in range(count):
        yield {"number": number, "role": session.get("role", "")}


async def bad_session(session):
    session["changed"] = True
    yield {"unreachable": True}


async def bad_utility():
    update_session("changed", True)
    yield {"unreachable": True}


async def explode():
    yield {"phase": "started"}
    raise ValueError("private stream detail")


async def limited():
    yield {"ok": True}


async def cors_feed():
    yield {"ok": True}


async def hidden_feed(value):
    yield {"value": value}


async def idle():
    global IDLE_CLOSED
    try:
        await asyncio.Event().wait()
        yield {"unreachable": True}
    finally:
        IDLE_CLOSED = True


def metadata(kind, method, endpoint, name, parameters, **extra):
    return {
        "kind": kind,
        "method": method,
        "endpoint": endpoint,
        "name": name,
        "module": "stream_endpoints",
        "file": "stream_endpoints.py",
        "is_async": kind == "sse",
        "parameters": parameters,
        "permissions": {},
        "cors": [],
        "limits": {},
        "hidden": False,
        "token": None,
        "api_key": None,
        "max_upload_bytes": None,
        **extra,
    }


SESSION = {"name": "session", "injected": True, "required": False}
ROLE = {"name": "role", "injected": False, "required": True}
COUNT = {"name": "count", "injected": False, "required": True}
VALUE = {"name": "value", "injected": False, "required": True}
ENDPOINTS = [
    {
        "metadata": metadata("expose", "POST", "login", "login", [ROLE, SESSION]),
        "function": login,
    },
    {
        "metadata": metadata(
            "sse", "GET", "numbers", "numbers", [COUNT, SESSION],
            permissions={"Session": {"role": "admin"}},
        ),
        "function": numbers,
    },
    {
        "metadata": metadata("sse", "GET", "bad-session", "bad_session", [SESSION]),
        "function": bad_session,
    },
    {
        "metadata": metadata("sse", "GET", "bad-utility", "bad_utility", []),
        "function": bad_utility,
    },
    {
        "metadata": metadata("sse", "GET", "explode", "explode", []),
        "function": explode,
    },
    {
        "metadata": metadata(
            "sse", "GET", "limited", "limited", [],
            limits={"per-worker": {"requests": 1, "window": 60}},
        ),
        "function": limited,
    },
    {
        "metadata": metadata(
            "sse", "GET", "cors-feed", "cors_feed", [],
            cors=["https://allowed.example"],
        ),
        "function": cors_feed,
    },
    {
        "metadata": metadata(
            "sse", "GET", "hidden-feed", "hidden_feed", [VALUE],
            permissions={"Session": {"role": "admin"}},
            hidden=True,
            token="0123456789abcdef0123456789abcdef",
        ),
        "function": hidden_feed,
    },
    {
        "metadata": metadata("sse", "GET", "idle", "idle", []),
        "function": idle,
    },
]
"""


SERVER_PROBE = r"""
import asyncio
import json

import server
import core._endpoints as endpoint_module


async def request(method, target, *, body=None, cookie=None, origin=None, fail_stream=False):
    path, separator, query = target.partition("?")
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = [(b"host", b"127.0.0.1")]
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    if origin:
        headers.append((b"origin", origin.encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii") if separator else b"",
        "root_path": "",
        "headers": headers,
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
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        messages.append(message)
        if fail_stream and message.get("type") == "http.response.body" and message.get("body"):
            raise OSError("client disconnected")

    failure = None
    try:
        await server.app(scope, receive, send)
    except Exception as error:
        failure = error
    start = next(item for item in messages if item["type"] == "http.response.start")
    content = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    return start["status"], dict(start["headers"]), content, failure


async def login(role):
    status, headers, _content, failure = await request(
        "POST", "/api/login", body={"role": role}
    )
    assert status == 200 and failure is None, (status, failure)
    return headers[b"set-cookie"].decode("latin-1").split(";", 1)[0]


def frames(content):
    return content.decode("utf-8").split("\n\n")


async def main():
    snapshot = server._read_only_session({"nested": {"values": [1]}})
    for mutate in (
        lambda: snapshot.__setitem__("new", True),
        lambda: snapshot["nested"].__setitem__("new", True),
        lambda: snapshot["nested"]["values"].append(2),
    ):
        try:
            mutate()
        except RuntimeError as error:
            assert "read-only" in str(error), error
        else:
            raise AssertionError("session snapshot accepted a mutation")

    status, _headers, _content, _failure = await request(
        "GET", "/api/numbers?count=2"
    )
    assert status == 401, status
    user_cookie = await login("user")
    status, _headers, _content, _failure = await request(
        "GET", "/api/numbers?count=2", cookie=user_cookie
    )
    assert status == 403, status

    admin_cookie = await login("admin")
    status, headers, content, failure = await request(
        "GET", "/api/numbers?count=2", cookie=admin_cookie
    )
    assert status == 200 and failure is None, (status, failure)
    assert headers[b"content-type"].startswith(b"text/event-stream"), headers
    assert headers[b"cache-control"] == b"no-cache", headers
    assert headers[b"x-accel-buffering"] == b"no", headers
    assert frames(content)[:2] == [
        'data: {"number":0,"role":"admin"}',
        'data: {"number":1,"role":"admin"}',
    ], content

    status, _headers, _content, _failure = await request("HEAD", "/api/numbers")
    assert status == 405, status
    status, _headers, denied, _failure = await request("GET", "/api/hidden-feed?value=1")
    status_missing, _headers, missing, _failure = await request("GET", "/api/missing")
    assert status == status_missing == 404 and denied == missing, (status, status_missing)
    status, _headers, _content, _failure = await request("HEAD", "/api/hidden-feed")
    assert status == 404, status

    status, _headers, content, _failure = await request(
        "POST", "/api/__sykit_manifest__", body={}, cookie=admin_cookie
    )
    manifest = json.loads(content)
    assert manifest == {
        "0123456789abcdef0123456789abcdef": {
            "e": "hidden-feed", "m": "GET", "p": ["value"], "s": True,
        }
    }, manifest
    status, _headers, content, failure = await request(
        "GET", "/api/hidden-feed?value=7", cookie=admin_cookie
    )
    assert status == 200 and failure is None, (status, failure)
    assert frames(content)[0] == 'data: {"value":7}', content

    for path in ("bad-session", "bad-utility"):
        status, _headers, content, failure = await request("GET", f"/api/{path}")
        assert status == 200 and failure is None, (status, failure)
        assert frames(content)[0] == (
            'event: sykit-error\ndata: {"error":"The stream failed."}'
        ), content
    assert [item[0] for item in endpoint_module.ERRORS[:2]] == [
        "RuntimeError", "RuntimeError"
    ], endpoint_module.ERRORS

    status, _headers, content, failure = await request("GET", "/api/explode")
    assert status == 200 and failure is None, (status, failure)
    assert frames(content)[:2] == [
        'data: {"phase":"started"}',
        'event: sykit-error\ndata: {"error":"The stream failed."}',
    ], content
    assert endpoint_module.ERRORS[-1] == ("ValueError", "/api/explode")
    assert b"private stream detail" not in content

    status, _headers, _content, _failure = await request("GET", "/api/limited")
    assert status == 200, status
    status, headers, content, _failure = await request("GET", "/api/limited")
    assert status == 429 and b"Rate limit exceeded" in content, (status, content)
    assert int(headers[b"retry-after"]) >= 1, headers

    status, _headers, _content, _failure = await request(
        "GET", "/api/cors-feed", origin="https://blocked.example"
    )
    assert status == 403, status
    status, headers, _content, _failure = await request(
        "GET", "/api/cors-feed", origin="https://allowed.example"
    )
    assert status == 200, status
    assert headers[b"access-control-allow-origin"] == b"https://allowed.example"

    server.SSE_HEARTBEAT_SECONDS = 0.01
    status, _headers, content, failure = await request(
        "GET", "/api/idle", fail_stream=True
    )
    assert status == 200 and content == b": keepalive\n\n", (status, content)
    assert type(failure).__name__ == "ClientDisconnect", failure
    assert endpoint_module.IDLE_CLOSED, "disconnect did not close the generator"


asyncio.run(main())
"""


class StreamingServerTests(unittest.TestCase):
    def test_runtime_guards_errors_heartbeat_and_disconnect_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-sse-server-") as directory:
            runtime = Path(directory)
            (runtime / "core").mkdir()
            (runtime / "app").mkdir()
            (runtime / "static").mkdir()
            shutil.copy2(ROOT / "files" / "server.py", runtime / "server.py")
            for name in (
                "__init__.py",
                "_apikeys.py",
                "_limits.py",
                "_sessions.py",
                "_task_runtime.py",
                "_task_store.py",
                "_tasks.py",
            ):
                shutil.copy2(ROOT / "files" / "core" / name, runtime / "core" / name)
            shutil.copytree(ROOT / "sykit", runtime / "app" / "sykit")
            (runtime / "config.json").write_text(
                json.dumps(
                    {
                        "endpoints": "/api/",
                        "allowed-hosts": ["127.0.0.1"],
                        "sse-heartbeat-seconds": 1,
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "core" / "_endpoints.py").write_text(
                SERVER_ENDPOINTS,
                encoding="utf-8",
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


if __name__ == "__main__":
    unittest.main()
