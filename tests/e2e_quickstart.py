"""Build a real app and drive it through a local reverse proxy with Chromium."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

ENDPOINTS = """\
from sykit import Upload, auth, expose, hidden, perms, sse


@expose("ping")
def ping(session: dict):
    session["count"] = session.get("count", 0) + 1
    return {"count": session["count"]}


@expose("sign-in")
def sign_in():
    auth.login({"role": "tester"})
    return {"ok": True}


@expose("secret")
@perms({"Session": {"role": "tester"}})
@hidden
def secret():
    return {"secret": "visible"}


@expose("upload")
def upload_file(file: Upload):
    return {"name": file.client_filename, "size": file.size}


@sse("events")
async def events():
    yield {"step": 1}
    yield {"step": 2}
"""

APP = """\
<script>
  import { events, ping, secret, sign_in, upload_file } from "$python";

  let pingResult = "waiting";
  let hiddenResult = "waiting";
  let uploadResult = "waiting";
  let streamResult = "waiting";

  async function doPing() {
    const result = await ping();
    pingResult = `count:${result.count}`;
  }

  async function checkHidden() {
    try {
      const result = await secret();
      hiddenResult = result.secret;
    } catch (error) {
      hiddenResult = `denied:${error.status}`;
    }
  }

  async function login() {
    await sign_in();
    hiddenResult = "signed-in";
  }

  async function upload(event) {
    const result = await upload_file(event.target.files[0]);
    uploadResult = `${result.name}:${result.size}`;
  }

  async function stream() {
    const steps = [];
    for await (const event of events()) steps.push(event.step);
    streamResult = steps.join(",");
  }
</script>

<main>
  <button id="ping" onclick={doPing}>Ping</button>
  <p id="ping-result">{pingResult}</p>

  <button id="hidden" onclick={checkHidden}>Hidden</button>
  <button id="login" onclick={login}>Login</button>
  <p id="hidden-result">{hiddenResult}</p>

  <input id="upload" type="file" onchange={upload}>
  <p id="upload-result">{uploadResult}</p>

  <button id="stream" onclick={stream}>Stream</button>
  <p id="stream-result">{streamResult}</p>
</main>
"""

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def proxy_handler(upstream_port: int):
    class ProxyHandler(BaseHTTPRequestHandler):
        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.casefold() not in HOP_HEADERS
            }
            headers["X-Forwarded-For"] = self.client_address[0]
            headers["X-Forwarded-Proto"] = "http"
            connection = http.client.HTTPConnection(
                "127.0.0.1", upstream_port, timeout=30
            )
            try:
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                content = response.read()
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.casefold() not in HOP_HEADERS:
                        self.send_header(name, value)
                self.end_headers()
                self.wfile.write(content)
            finally:
                connection.close()

        do_GET = _forward
        do_HEAD = _forward
        do_POST = _forward

        def log_message(self, *_arguments) -> None:
            pass

    return ProxyHandler


def wait_until_ready(url: str, process: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Built server exited with {process.returncode}.")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("Built server did not become ready.")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sykit-browser-e2e-") as directory:
        project = Path(directory)
        subprocess.run([sys.executable, str(ROOT), "init"], cwd=project, check=True)
        source = project / "src"
        (source / "endpoints.py").write_text(ENDPOINTS, encoding="utf-8")
        (source / "App.svelte").write_text(APP, encoding="utf-8")

        upstream_port = free_port()
        proxy_port = free_port()
        config_path = source / "sykit" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.update(
            {
                "host-ip": "127.0.0.1",
                "host-port": upstream_port,
                "session-store": "sqlite:e2e-sessions.sqlite3",
                "trust-proxy": True,
            }
        )
        config_path.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT), "build"], cwd=project, check=True)

        environment = os.environ.copy()
        environment["SYKIT_SESSION_SECRET"] = "e2e-secret-that-is-longer-than-32-bytes"
        log_path = project / "server.log"
        with log_path.open("w", encoding="utf-8") as server_log:
            process = subprocess.Popen(
                [sys.executable, str(project / "built" / "main.py")],
                cwd=project,
                env=environment,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            proxy = ThreadingHTTPServer(
                ("127.0.0.1", proxy_port), proxy_handler(upstream_port)
            )
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            try:
                base = f"http://127.0.0.1:{proxy_port}"
                wait_until_ready(base + "/healthz", process)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_page()
                    page.goto(base)

                    page.locator("#ping").click()
                    page.locator("#ping-result").get_by_text("count:1").wait_for()
                    page.locator("#ping").click()
                    page.locator("#ping-result").get_by_text("count:2").wait_for()

                    page.locator("#hidden").click()
                    page.locator("#hidden-result").get_by_text("denied:404").wait_for()
                    page.locator("#login").click()
                    page.locator("#hidden-result").get_by_text("signed-in").wait_for()
                    page.locator("#hidden").click()
                    page.locator("#hidden-result").get_by_text("visible").wait_for()

                    page.locator("#upload").set_input_files(
                        {
                            "name": "sample.txt",
                            "mimeType": "text/plain",
                            "buffer": b"hello",
                        }
                    )
                    page.locator("#upload-result").get_by_text(
                        "sample.txt:5"
                    ).wait_for()

                    page.locator("#stream").click()
                    page.locator("#stream-result").get_by_text("1,2").wait_for()
                    browser.close()
            except BaseException as error:
                server_log.flush()
                details = log_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError(
                    f"Browser E2E failed. Server log:\n{details}"
                ) from error
            finally:
                proxy.shutdown()
                proxy.server_close()
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    print("Browser-to-server E2E test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
