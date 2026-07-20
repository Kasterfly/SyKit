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
from sykit import Upload, expose

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _upload_endpoint(*, hidden: bool = False) -> build.EndpointInfo:
    return build.EndpointInfo(
        kind="expose",
        method="POST",
        endpoint="store",
        function="store",
        module="endpoints",
        file="endpoints.py",
        is_async=False,
        parameters=(
            build.ParameterInfo("file", False, True, True),
            build.ParameterInfo("metadata", False, True),
            build.ParameterInfo("optional", False, False, True),
        ),
        permissions={"Session": {"admin": True}} if hidden else {},
        cors=(),
        limits={},
        hidden=hidden,
        token="0123456789abcdef0123456789abcdef" if hidden else None,
        max_upload_bytes=4096,
    )


class UploadDecoratorTests(unittest.TestCase):
    def test_runtime_metadata_records_endpoint_limit(self) -> None:
        @expose("store", max_upload_bytes=4096)
        def store(file: Upload):
            return file.size

        self.assertEqual(store.__sykit__["max_upload_bytes"], 4096)

    def test_runtime_decorator_rejects_invalid_limits(self) -> None:
        for value in (0, -1, True, 1.5, "10"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    expose("store", max_upload_bytes=value)  # type: ignore[arg-type]


class UploadBuildTests(unittest.TestCase):
    def _parse(self, source: str) -> build.EndpointInfo:
        with tempfile.TemporaryDirectory(prefix="sykit-upload-parser-") as directory:
            root = Path(directory)
            path = root / "endpoints.py"
            path.write_text(textwrap.dedent(source), encoding="utf-8")
            return build.parse_decorators(path, root)[0]

    def test_parser_records_uploads_and_endpoint_limit(self) -> None:
        endpoint = self._parse(
            """
            from typing import Optional
            from sykit import Upload, expose

            @expose("store", max_upload_bytes=4096)
            def store(
                file: Upload,
                note: str,
                backup: Optional[Upload] = None,
                alternate: Upload | None = None,
            ):
                return None
            """
        )
        self.assertEqual(endpoint.max_upload_bytes, 4096)
        self.assertEqual(
            [(item.name, item.upload, item.required) for item in endpoint.parameters],
            [
                ("file", True, True),
                ("note", False, True),
                ("backup", True, False),
                ("alternate", True, False),
            ],
        )
        manifest = build.generate_backend_manifest([endpoint])
        self.assertIn("'upload': True", manifest)
        self.assertIn("'max_upload_bytes': 4096", manifest)

    def test_invalid_upload_declarations_fail_at_build_time(self) -> None:
        cases = {
            "Upload parameters are only supported": """
                from sykit import Upload, raw
                @raw("download")
                def download(file: Upload):
                    return None
            """,
            "requires at least one Upload": """
                from sykit import expose
                @expose("store", max_upload_bytes=10)
                def store(value):
                    return value
            """,
            "positive integer": """
                from sykit import Upload, expose
                @expose("store", max_upload_bytes=True)
                def store(file: Upload):
                    return None
            """,
        }
        for message, source in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(build.BuildError, message):
                    self._parse(source)

    def test_endpoint_limit_cannot_exceed_global_limit(self) -> None:
        endpoint = _upload_endpoint()
        with self.assertRaisesRegex(build.BuildError, "cannot exceed"):
            build.apply_endpoint_defaults(
                {"max-request-bytes": 1024},
                [endpoint],
                Path("sykit/config.json"),
            )


@unittest.skipUnless(NODE, "Node.js is required for generated-client tests")
class UploadClientTests(unittest.TestCase):
    def test_client_sends_form_data_and_validates_blob_parameters(self) -> None:
        module = build.generate_client_module({}, [_upload_endpoint()])
        with tempfile.TemporaryDirectory(prefix="sykit-upload-client-") as directory:
            root = Path(directory)
            module_path = root / "client.mjs"
            module_path.write_text(module, encoding="utf-8")
            runner = root / "runner.mjs"
            runner.write_text(
                "const calls = [];\n"
                "globalThis.fetch = async (url, options) => {\n"
                "  calls.push({ url, options });\n"
                "  return { ok: true, status: 200, text: async () => '{\"ok\":true}' };\n"
                "};\n"
                f"const client = await import({json.dumps(module_path.as_uri())});\n"
                "const blob = new Blob(['hello'], { type: 'application/not-trusted' });\n"
                "const result = await client.store(blob, { title: 'demo' });\n"
                "if (result?.ok !== true || calls.length !== 1) throw new Error('request failed');\n"
                "const call = calls[0];\n"
                "if (call.url !== '/api/store' || call.options.method !== 'POST') throw new Error('bad request');\n"
                "if (!(call.options.body instanceof FormData)) throw new Error('expected FormData');\n"
                "if (call.options.headers) throw new Error('multipart content type must come from the browser');\n"
                "const entries = Array.from(call.options.body.entries());\n"
                "if (entries.length !== 2) throw new Error(`bad entries: ${entries.length}`);\n"
                "if (entries[0][0] !== 'file' || !(entries[0][1] instanceof Blob)) throw new Error('bad file');\n"
                "if (entries[1][0] !== 'metadata' || entries[1][1] !== '{\"title\":\"demo\"}') throw new Error('bad metadata');\n"
                "let error = null;\n"
                "try { await client.store('not-a-file', {}); } catch (caught) { error = caught; }\n"
                "if (!(error instanceof TypeError) || !error.message.includes('File or Blob')) throw new Error('missing type error');\n"
                "if (calls.length !== 1) throw new Error('invalid upload reached fetch');\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [NODE, str(runner)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_hidden_upload_uses_runtime_route_and_upload_metadata(self) -> None:
        endpoint = _upload_endpoint(hidden=True)
        module = build.generate_client_module({}, [endpoint])
        manifest = {
            endpoint.token: {
                "e": "store",
                "m": "POST",
                "p": ["file", "metadata", "optional"],
                "u": ["file", "optional"],
            }
        }
        with tempfile.TemporaryDirectory(prefix="sykit-hidden-upload-") as directory:
            root = Path(directory)
            module_path = root / "client.mjs"
            module_path.write_text(module, encoding="utf-8")
            runner = root / "runner.mjs"
            runner.write_text(
                "const calls = [];\n"
                "globalThis.fetch = async (url, options) => {\n"
                "  calls.push({ url, options });\n"
                "  const data = url.endsWith('__sykit_manifest__')\n"
                f"    ? {json.dumps(manifest)} : {{ ok: true }};\n"
                "  return { ok: true, status: 200, text: async () => JSON.stringify(data) };\n"
                "};\n"
                f"const client = await import({json.dumps(module_path.as_uri())});\n"
                "await client.store(new Blob(['hidden']), { ok: true });\n"
                "if (calls.length !== 2 || calls[1].url !== '/api/store') throw new Error('hidden route failed');\n"
                "if (!(calls[1].options.body instanceof FormData)) throw new Error('hidden upload was not multipart');\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [NODE, str(runner)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


class UploadServerTests(unittest.TestCase):
    def test_runtime_streams_to_disk_enforces_caps_and_always_closes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-upload-server-") as directory:
            runtime = Path(directory)
            (runtime / "core").mkdir()
            (runtime / "app").mkdir()
            (runtime / "static").mkdir()
            shutil.copy2(ROOT / "files" / "server.py", runtime / "server.py")
            for name in ("__init__.py", "_limits.py", "_sessions.py", "_apikeys.py"):
                shutil.copy2(ROOT / "files" / "core" / name, runtime / "core" / name)
            shutil.copytree(ROOT / "sykit", runtime / "app" / "sykit")
            (runtime / "config.json").write_text(
                json.dumps(
                    {
                        "endpoints": "/api/",
                        "allowed-hosts": ["127.0.0.1"],
                        "max-request-bytes": 1024,
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "core" / "_endpoints.py").write_text(
                textwrap.dedent(
                    """
                    LAST_UPLOAD = None


                    def accept(file, metadata):
                        global LAST_UPLOAD
                        LAST_UPLOAD = file
                        return {
                            "body": file.read().decode("utf-8"),
                            "size": file.size,
                            "rolled": bool(getattr(file.file, "_rolled", False)),
                            "filename": file.client_filename,
                            "content_type": file.client_content_type,
                            "metadata": metadata,
                        }


                    def fail(file):
                        global LAST_UPLOAD
                        LAST_UPLOAD = file
                        raise RuntimeError("expected failure")


                    def record(name, function, parameters, maximum):
                        return {
                            "metadata": {
                                "kind": "expose",
                                "method": "POST",
                                "endpoint": name,
                                "name": name,
                                "module": "upload_endpoints",
                                "file": "upload_endpoints.py",
                                "is_async": False,
                                "parameters": parameters,
                                "permissions": {},
                                "cors": [],
                                "limits": {},
                                "max_upload_bytes": maximum,
                            },
                            "function": function,
                        }


                    FILE = {"name": "file", "injected": False, "required": True, "upload": True}
                    METADATA = {"name": "metadata", "injected": False, "required": True, "upload": False}
                    ENDPOINTS = [
                        record("accept", accept, [FILE, METADATA], 700),
                        record("small", accept, [FILE, METADATA], 180),
                        record("fail", fail, [FILE], 700),
                    ]
                    """
                ),
                encoding="utf-8",
            )
            probe = runtime / "probe.py"
            probe.write_text(
                textwrap.dedent(
                    r"""
                    import asyncio
                    import json

                    import core._endpoints as endpoint_module
                    import server


                    ORIGINAL_PARSER = server.DiskMultiPartParser
                    CREATED = []


                    class TrackingParser(ORIGINAL_PARSER):
                        def on_headers_finished(self):
                            super().on_headers_finished()
                            if self._current_part.file is not None:
                                CREATED.append(self._current_part.file.file)


                    server.DiskMultiPartParser = TrackingParser


                    def multipart(payload=b"hello", *, include_metadata=True, as_file=True):
                        boundary = "sykit-boundary"
                        disposition = 'form-data; name="file"'
                        if as_file:
                            disposition += '; filename="../client.txt"'
                        parts = [
                            f"--{boundary}\r\nContent-Disposition: {disposition}\r\nContent-Type: application/not-trusted\r\n\r\n".encode(),
                            payload,
                            b"\r\n",
                        ]
                        if include_metadata:
                            parts.extend([
                                f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode(),
                                json.dumps({"title": "demo"}).encode(),
                                b"\r\n",
                            ])
                        parts.append(f"--{boundary}--\r\n".encode())
                        return boundary, b"".join(parts)


                    async def request(path, body, content_type, *, chunks=None, content_length=True):
                        headers = [
                            (b"host", b"127.0.0.1"),
                            (b"content-type", content_type.encode("ascii")),
                        ]
                        if content_length:
                            headers.append((b"content-length", str(len(body)).encode("ascii")))
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
                        pending = list(chunks or [body])

                        async def receive():
                            if not pending:
                                return {"type": "http.disconnect"}
                            chunk = pending.pop(0)
                            return {
                                "type": "http.request",
                                "body": chunk,
                                "more_body": bool(pending),
                            }

                        async def send(message):
                            messages.append(message)

                        await server.app(scope, receive, send)
                        start = next(item for item in messages if item["type"] == "http.response.start")
                        content = b"".join(
                            item.get("body", b"")
                            for item in messages
                            if item["type"] == "http.response.body"
                        )
                        return start["status"], json.loads(content)


                    def all_closed():
                        return CREATED and all(item.closed for item in CREATED)


                    async def main():
                        boundary, body = multipart()
                        content_type = f"multipart/form-data; boundary={boundary}"

                        CREATED.clear()
                        status, data = await request("/api/accept", body, content_type)
                        assert status == 200, (status, data)
                        assert data == {
                            "body": "hello",
                            "size": 5,
                            "rolled": True,
                            "filename": "../client.txt",
                            "content_type": "application/not-trusted",
                            "metadata": {"title": "demo"},
                        }, data
                        assert endpoint_module.LAST_UPLOAD.closed
                        assert all_closed()

                        CREATED.clear()
                        status, data = await request("/api/accept", b"{}", "application/json")
                        assert status == 400 and "multipart/form-data" in data["error"], data

                        CREATED.clear()
                        _boundary, text_body = multipart(as_file=False)
                        status, data = await request("/api/accept", text_body, content_type)
                        assert status == 400 and "must be a file" in data["error"], data
                        assert not CREATED

                        CREATED.clear()
                        split = body.find(b"\r\n\r\n") + 8
                        status, data = await request(
                            "/api/small",
                            body,
                            content_type,
                            chunks=[body[:split], body[split:]],
                        )
                        assert status == 413 and "180-byte" in data["error"], data
                        assert all_closed()

                        CREATED.clear()
                        fail_boundary, fail_body = multipart(include_metadata=False)
                        status, data = await request(
                            "/api/fail",
                            fail_body,
                            f"multipart/form-data; boundary={fail_boundary}",
                        )
                        assert status == 500, (status, data)
                        assert endpoint_module.LAST_UPLOAD.closed
                        assert all_closed()

                        CREATED.clear()
                        global_boundary, global_body = multipart(payload=b"x" * 1200)
                        split = global_body.find(b"\r\n\r\n") + 8
                        status, data = await request(
                            "/api/accept",
                            global_body,
                            f"multipart/form-data; boundary={global_boundary}",
                            chunks=[global_body[:split], global_body[split:]],
                            content_length=False,
                        )
                        assert status == 413 and "1024-byte" in data["error"], data
                        assert all_closed()


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
