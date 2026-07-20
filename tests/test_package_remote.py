from __future__ import annotations

import contextlib
import io
import json
import tarfile
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import package
import package_remote

SHA = "4f2a91c" + "0" * 33
SETTINGS = {
    "default-repo": "Owner/Repo",
    "max-download-bytes": 1024 * 1024,
}


def make_tarball(files: dict[str, bytes], top: str = "Repo-abc123") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def make_package_tarball(extra: dict[str, bytes] | None = None) -> bytes:
    files = {
        "SyKitPackage.json": json.dumps({"id": "remote-sample"}).encode("utf-8"),
        "add/hello.txt": b"hello from remote",
    }
    files.update(extra or {})
    return make_tarball(files)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        self.server.requests.append(self.path)
        route = self.server.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, body = route
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments):
        pass


class ServerMixin(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.routes = {}
        self.server.requests = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        patcher = mock.patch.object(package_remote, "_REQUIRE_HTTPS", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def route(self, path: str, body: bytes, status: int = 200, **headers) -> None:
        self.server.routes[path] = (status, headers, body)


class ParseSourceTests(unittest.TestCase):
    def test_github_spec_forms(self) -> None:
        spec = package_remote.parse_source("github:Owner/Repo")
        self.assertEqual(
            (spec.owner, spec.repo, spec.subdir, spec.ref), ("Owner", "Repo", "", "")
        )
        spec = package_remote.parse_source("github:Owner/Repo/pkg/aws@v1.2.0")
        self.assertEqual(spec.subdir, "pkg/aws")
        self.assertEqual(spec.ref, "v1.2.0")

    def test_github_spec_rejections(self) -> None:
        for argument in (
            "github:Owner",
            "github:Owner/Repo/../evil",
            "github:Owner/Repo/a b",
            "github:Owner/Repo@",
            "github:Owner/Repo/CON",
            "github:Owner/Repo@..",
            "github:/Repo",
        ):
            with self.subTest(argument=argument):
                with self.assertRaises(package_remote.RemoteError):
                    package_remote.parse_source(argument)

    def test_http_url_is_rejected(self) -> None:
        with self.assertRaises(package_remote.RemoteError):
            package_remote.parse_source("http://example.com/pkg.tar.gz")

    def test_credentialed_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(package_remote.RemoteError, "credentials"):
            package_remote.parse_source("https://user:token@host/pkg.tar.gz")

    def test_bare_name_with_optional_ref(self) -> None:
        spec = package_remote.parse_source("aws")
        self.assertEqual((spec.kind, spec.name, spec.ref), ("name", "aws", ""))
        spec = package_remote.parse_source("aws@v2.0.0")
        self.assertEqual((spec.name, spec.ref), ("aws", "v2.0.0"))

    def test_unrecognized_sources_are_rejected(self) -> None:
        for argument in ("missing/folder", "CON", "..", "a:b"):
            with self.subTest(argument=argument):
                with self.assertRaises(package_remote.RemoteError):
                    package_remote.parse_source(argument)

    def test_strip_userinfo(self) -> None:
        self.assertEqual(
            package_remote._strip_userinfo("https://user:pw@host:8080/x?q=1"),
            "https://host:8080/x?q=1",
        )


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-extract-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def extract(self, data: bytes, max_bytes: int = 1024 * 1024) -> Path:
        tar_path = self.root / "archive.tar.gz"
        tar_path.write_bytes(data)
        destination = self.root / f"out-{len(list(self.root.iterdir()))}"
        destination.mkdir()
        package_remote._extract_archive(tar_path, destination, max_bytes)
        return destination

    def make_raw(self, members: list[tarfile.TarInfo], payloads: list[bytes]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for info, payload in zip(members, payloads):
                archive.addfile(info, io.BytesIO(payload) if payload else None)
        return buffer.getvalue()

    def member(
        self, name: str, size: int = 0, kind: bytes = tarfile.REGTYPE, link: str = ""
    ) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.size = size
        info.type = kind
        info.linkname = link
        return info

    def test_good_archive_extracts(self) -> None:
        destination = self.extract(make_package_tarball())
        root = package_remote._archive_root(destination)
        self.assertTrue((root / "SyKitPackage.json").is_file())
        self.assertEqual(
            (root / "add" / "hello.txt").read_bytes(), b"hello from remote"
        )

    def test_traversal_entries_are_rejected(self) -> None:
        for name in ("../evil.txt", "/evil.txt", "a/../evil.txt", "C:evil"):
            with self.subTest(name=name):
                data = self.make_raw([self.member(name, 4)], [b"boom"])
                with self.assertRaises(package_remote.RemoteError):
                    self.extract(data)

    def test_link_and_device_entries_are_rejected(self) -> None:
        cases = (
            self.member("top/link", kind=tarfile.SYMTYPE, link="../../outside"),
            self.member("top/hard", kind=tarfile.LNKTYPE, link="package.py"),
            self.member("top/dev", kind=tarfile.CHRTYPE),
        )
        for info in cases:
            with self.subTest(name=info.name):
                data = self.make_raw([info], [b""])
                with self.assertRaisesRegex(
                    package_remote.RemoteError, "not a regular file"
                ):
                    self.extract(data)

    def test_windows_reserved_names_are_rejected(self) -> None:
        for name in ("top/CON.txt", "top/file.", "top/file ", "top/a:b"):
            with self.subTest(name=name):
                data = self.make_raw([self.member(name, 1)], [b"x"])
                with self.assertRaises(package_remote.RemoteError):
                    self.extract(data)

    def test_size_cap_is_enforced(self) -> None:
        data = make_tarball({"big.bin": b"x" * 4096})
        with self.assertRaisesRegex(package_remote.RemoteError, "limit"):
            self.extract(data, max_bytes=1024)

    def test_entry_count_cap_is_enforced(self) -> None:
        files = {f"file-{number}.txt": b"x" for number in range(6)}
        data = make_tarball(files)
        with mock.patch.object(package_remote, "MAX_ARCHIVE_ENTRIES", 3):
            with self.assertRaisesRegex(package_remote.RemoteError, "entries"):
                self.extract(data)

    def test_case_colliding_entries_are_rejected(self) -> None:
        members = [self.member("top/a/File.txt", 1), self.member("top/A/file.TXT", 1)]
        data = self.make_raw(members, [b"x", b"y"])
        with self.assertRaisesRegex(package_remote.RemoteError, "collide"):
            self.extract(data)


class TransportTests(ServerMixin):
    def test_download_and_final_url(self) -> None:
        self.route("/file.bin", b"payload")
        destination = Path(tempfile.mkdtemp(prefix="sykit-dl-")) / "out.bin"
        final = package_remote._download(f"{self.base}/file.bin", destination, 1024)
        self.assertEqual(destination.read_bytes(), b"payload")
        self.assertTrue(final.endswith("/file.bin"))

    def test_streaming_size_cap(self) -> None:
        self.route("/big.bin", b"x" * 4096)
        destination = Path(tempfile.mkdtemp(prefix="sykit-dl-")) / "out.bin"
        with self.assertRaisesRegex(package_remote.RemoteError, "limit"):
            package_remote._download(f"{self.base}/big.bin", destination, 1024)

    def test_missing_file_is_not_found(self) -> None:
        destination = Path(tempfile.mkdtemp(prefix="sykit-dl-")) / "out.bin"
        with self.assertRaises(package_remote.RemoteNotFound):
            package_remote._download(f"{self.base}/nope.bin", destination, 1024)

    def test_redirects_are_followed(self) -> None:
        self.route("/from.bin", b"", status=302, Location=f"{self.base}/to.bin")
        self.route("/to.bin", b"real")
        destination = Path(tempfile.mkdtemp(prefix="sykit-dl-")) / "out.bin"
        final = package_remote._download(f"{self.base}/from.bin", destination, 1024)
        self.assertEqual(destination.read_bytes(), b"real")
        self.assertTrue(final.endswith("/to.bin"))

    def test_https_to_http_redirect_is_refused(self) -> None:
        with mock.patch.object(package_remote, "_REQUIRE_HTTPS", True):
            handler = package_remote._SafeRedirects()
            request = urllib.request.Request("https://example.com/a")
            with self.assertRaisesRegex(package_remote.RemoteError, "non-https"):
                handler.redirect_request(
                    request, None, 302, "Found", {}, "http://evil.example.com/b"
                )

    def test_plain_http_is_refused_when_https_required(self) -> None:
        destination = Path(tempfile.mkdtemp(prefix="sykit-dl-")) / "out.bin"
        with mock.patch.object(package_remote, "_REQUIRE_HTTPS", True):
            with self.assertRaisesRegex(package_remote.RemoteError, "non-https"):
                package_remote._download(f"{self.base}/file.bin", destination, 1024)


class GithubFlowTests(ServerMixin):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(package_remote, "GITHUB_ARCHIVE_ROOT", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, responses: dict[str, object]):
        def fake(path: str):
            value = responses.get(path)
            if value is None:
                raise package_remote.RemoteNotFound(f"GitHub API: {path}")
            if isinstance(value, Exception):
                raise value
            return value

        patcher = mock.patch.object(package_remote, "_api_json", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fetches_by_resolved_sha_not_by_ref(self) -> None:
        self.api({"/repos/Owner/Repo/commits/main": {"sha": SHA}})
        self.route(f"/Owner/Repo/archive/{SHA}.tar.gz", make_package_tarball())
        result = package_remote.resolve("github:Owner/Repo@main", SETTINGS)
        self.addCleanup(result.cleanup)
        self.assertIsInstance(result, package_remote.RemotePackage)
        self.assertEqual(result.source["resolved_sha"], SHA)
        self.assertEqual(result.source["kind"], "github")
        self.assertEqual(result.source["spec"], "github:Owner/Repo@main")
        self.assertEqual(result.source["ref_type"], "branch")
        self.assertIn(f"/Owner/Repo/archive/{SHA}.tar.gz", self.server.requests)
        self.assertNotIn("/Owner/Repo/archive/main.tar.gz", self.server.requests)
        self.assertTrue((result.directory / package.MANIFEST_NAME).is_file())

    def test_full_sha_does_not_need_the_github_api(self) -> None:
        self.api({})
        self.route(f"/Owner/Repo/archive/{SHA}.tar.gz", make_package_tarball())
        result = package_remote.resolve(f"github:Owner/Repo@{SHA}", SETTINGS)
        self.addCleanup(result.cleanup)
        self.assertEqual(result.source["resolved_sha"], SHA)
        self.assertEqual(result.source["ref_type"], "sha")

    def test_update_aborts_when_api_cannot_pin_release(self) -> None:
        self.api(
            {
                "/repos/Owner/Repo/releases/latest": package_remote.ApiUnavailable(
                    "the GitHub API is unreachable"
                )
            }
        )
        with self.assertRaisesRegex(package_remote.RemoteError, "full commit SHA"):
            package_remote.fetch_repo("Owner/Repo", "", SETTINGS)

    def test_update_branch_requires_explicit_allow(self) -> None:
        self.api({"/repos/Owner/Repo/commits/main": {"sha": SHA}})
        with self.assertRaisesRegex(package_remote.RemoteError, "allow-unreleased"):
            package_remote.fetch_repo("Owner/Repo", "main", SETTINGS)

        self.route(f"/Owner/Repo/archive/{SHA}.tar.gz", make_package_tarball())
        result = package_remote.fetch_repo(
            "Owner/Repo", "main", SETTINGS, allow_unreleased=True
        )
        self.addCleanup(result.cleanup)
        self.assertEqual(result.source["resolved_sha"], SHA)

    def test_api_unavailable_falls_back_to_moving_ref(self) -> None:
        self.api(
            {
                "/repos/Owner/Repo/commits/main": package_remote.ApiUnavailable(
                    "the GitHub API is unreachable"
                )
            }
        )
        self.route("/Owner/Repo/archive/main.tar.gz", make_package_tarball())
        result = package_remote.resolve("github:Owner/Repo@main", SETTINGS)
        self.addCleanup(result.cleanup)
        self.assertIsNone(result.source["resolved_sha"])
        self.assertTrue(
            any("moving ref" in note for note in result.notes), result.notes
        )

    def test_unknown_ref_is_a_clear_error(self) -> None:
        self.api({})
        with self.assertRaisesRegex(package_remote.RemoteError, "no branch"):
            package_remote.resolve("github:Owner/Repo@ghost", SETTINGS)

    def test_bare_name_uses_release_and_index(self) -> None:
        index = json.dumps(
            {"packages": {"aws": {"path": "packages/aws", "desc": "AWS"}}}
        ).encode("utf-8")
        tarball = make_tarball(
            {
                "index.json": index,
                "packages/aws/SyKitPackage.json": json.dumps({"id": "aws"}).encode(
                    "utf-8"
                ),
                "packages/aws/add/aws.txt": b"aws payload",
            }
        )
        self.api(
            {
                "/repos/Owner/Repo/releases/latest": {"tag_name": "v1.0.0"},
                "/repos/Owner/Repo/commits/v1.0.0": {"sha": SHA},
            }
        )
        self.route(f"/Owner/Repo/archive/{SHA}.tar.gz", tarball)
        result = package_remote.resolve("aws", SETTINGS)
        self.addCleanup(result.cleanup)
        self.assertEqual(result.source["spec"], "github:Owner/Repo/packages/aws@v1.0.0")
        self.assertEqual(result.source["ref_type"], "tag")
        self.assertTrue((result.directory / "add" / "aws.txt").is_file())

    def test_repo_without_package_lists_index(self) -> None:
        index = json.dumps(
            {"packages": {"aws": {"desc": "AWS"}, "supabase": {}}}
        ).encode("utf-8")
        self.api(
            {
                "/repos/Owner/Repo": {"default_branch": "main"},
                "/repos/Owner/Repo/commits/main": {"sha": SHA},
            }
        )
        self.route(
            f"/Owner/Repo/archive/{SHA}.tar.gz", make_tarball({"index.json": index})
        )
        result = package_remote.resolve("github:Owner/Repo", SETTINGS)
        self.assertIsInstance(result, package_remote.RepoListing)
        text = "\n".join(result.lines)
        self.assertIn("aws - AWS", text)
        self.assertIn("supabase", text)

    def test_malicious_index_path_is_rejected(self) -> None:
        index = json.dumps({"packages": {"aws": {"path": "../evil"}}}).encode("utf-8")
        tarball = make_tarball({"index.json": index})
        self.api(
            {
                "/repos/Owner/Repo/releases/latest": {"tag_name": "v1.0.0"},
                "/repos/Owner/Repo/commits/v1.0.0": {"sha": SHA},
            }
        )
        self.route(f"/Owner/Repo/archive/{SHA}.tar.gz", tarball)
        with self.assertRaises(package_remote.RemoteError):
            package_remote.resolve("aws", SETTINGS)

    def test_classify_ref(self) -> None:
        self.assertEqual(package_remote._classify_ref("Owner", "Repo", "0" * 40), "sha")
        with mock.patch.object(
            package_remote, "_api_json", return_value={"ref": "refs/tags/v1"}
        ):
            self.assertEqual(package_remote._classify_ref("Owner", "Repo", "v1"), "tag")
        with mock.patch.object(
            package_remote,
            "_api_json",
            side_effect=package_remote.RemoteNotFound("no"),
        ):
            self.assertEqual(
                package_remote._classify_ref("Owner", "Repo", "abc1234"), "sha"
            )
            self.assertEqual(
                package_remote._classify_ref("Owner", "Repo", "develop"), "branch"
            )
        with mock.patch.object(
            package_remote,
            "_api_json",
            side_effect=package_remote.ApiUnavailable("down"),
        ):
            self.assertIsNone(package_remote._classify_ref("Owner", "Repo", "develop"))


class UrlFlowTests(ServerMixin):
    def test_url_tarball_installs_with_final_url(self) -> None:
        self.route("/pkg.tar.gz", b"", status=302, Location=f"{self.base}/real.tar.gz")
        self.route("/real.tar.gz", make_package_tarball())
        result = package_remote._resolve_url(
            package_remote.SourceSpec("url", "spec", url=f"{self.base}/pkg.tar.gz"),
            SETTINGS,
        )
        self.addCleanup(result.cleanup)
        self.assertEqual(result.source["kind"], "url")
        self.assertTrue(result.source["final_url"].endswith("/real.tar.gz"))
        self.assertTrue((result.directory / package.MANIFEST_NAME).is_file())

    def test_cleanup_removes_fetched_tree(self) -> None:
        self.route("/pkg.tar.gz", make_package_tarball())
        result = package_remote._resolve_url(
            package_remote.SourceSpec("url", "spec", url=f"{self.base}/pkg.tar.gz"),
            SETTINGS,
        )
        directory = result.directory
        self.assertTrue(directory.exists())
        result.cleanup()
        self.assertFalse(directory.exists())


class RemoteInstallEndToEndTests(ServerMixin):
    """A full package add from a served GitHub-style archive."""

    def setUp(self) -> None:
        super().setUp()
        self.tool_temp = tempfile.TemporaryDirectory(prefix="sykit-remote-e2e-")
        root = Path(self.tool_temp.name)
        self.tool = root / "SyKit"
        self.tool.mkdir()
        self.original_paths = (
            package.TOOL_DIR,
            package.PACKAGES_DIR,
            package.INDEX_PATH,
            package.AUTHORS_PATH,
        )
        package.TOOL_DIR = self.tool
        package.PACKAGES_DIR = self.tool / ".packages"
        package.INDEX_PATH = package.PACKAGES_DIR / "index.json"
        package.AUTHORS_PATH = package.PACKAGES_DIR / "authors.md"
        patcher = mock.patch.object(package_remote, "GITHUB_ARCHIVE_ROOT", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

        def fake_api(path: str):
            if path == "/repos/Owner/Repo/commits/main":
                return {"sha": SHA}
            raise package_remote.RemoteNotFound(f"GitHub API: {path}")

        api_patcher = mock.patch.object(
            package_remote, "_api_json", side_effect=fake_api
        )
        api_patcher.start()
        self.addCleanup(api_patcher.stop)

    def tearDown(self) -> None:
        (
            package.TOOL_DIR,
            package.PACKAGES_DIR,
            package.INDEX_PATH,
            package.AUTHORS_PATH,
        ) = self.original_paths
        self.tool_temp.cleanup()

    def test_remote_add_records_provenance_and_removes_cleanly(self) -> None:
        self.route(f"/Owner/Repo/archive/{SHA}.tar.gz", make_package_tarball())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installed = package._command_add("github:Owner/Repo@main", assume_yes=True)
        self.assertTrue(installed)
        self.assertEqual((self.tool / "hello.txt").read_bytes(), b"hello from remote")
        record = json.loads(
            (package.PACKAGES_DIR / "remote-sample" / package.RECORD_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["source"]["kind"], "github")
        self.assertEqual(record["source"]["resolved_sha"], SHA)
        self.assertTrue(record["source"]["content_hash"].startswith("sha256:"))
        self.assertIn("third-party", output.getvalue())

        with contextlib.redirect_stdout(io.StringIO()) as listing:
            package._command_list()
        self.assertIn("github:Owner/Repo@main", listing.getvalue())

        with contextlib.redirect_stdout(io.StringIO()):
            package._command_remove("remote-sample")
        self.assertEqual(package._load_index(), [])
        self.assertFalse((self.tool / "hello.txt").exists())


if __name__ == "__main__":
    unittest.main()
