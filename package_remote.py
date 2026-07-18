"""Remote package sources for the SyKit package handler.

Fetches packages from GitHub repositories or https tarball URLs using only
the standard library. Downloads are pinned to an exact commit when the GitHub
API is reachable, extraction defends against archive attacks (path traversal,
links, bombs), and every transport hop must stay on https.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import package

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_ARCHIVE_ROOT = "https://github.com"
USER_AGENT = "SyKit-package-manager"
API_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 120
MAX_REDIRECTS = 5
MAX_API_BYTES = 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20000
INDEX_NAME = "index.json"
SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
SHORT_SHA_PATTERN = re.compile(r"[0-9a-f]{7,40}")
# Tests may patch this to exercise the network path against a local server.
# Nothing else reads it: no config key, environment variable, or CLI flag can
# turn off the https requirement.
_REQUIRE_HTTPS = True


class RemoteError(package.PackageError):
    """A user-facing remote fetch failure."""


class RemoteNotFound(RemoteError):
    """The requested repository, ref, or resource does not exist."""


class ApiUnavailable(RemoteError):
    """The GitHub API cannot be used right now (network or rate limit)."""


@dataclass
class SourceSpec:
    kind: str  # "github" | "url" | "name"
    argument: str
    owner: str = ""
    repo: str = ""
    subdir: str = ""
    ref: str = ""
    url: str = ""
    name: str = ""


@dataclass
class RemotePackage:
    """A fetched package folder plus its provenance."""

    directory: Path
    source: dict[str, Any]
    notes: list[str]
    _temporary: tempfile.TemporaryDirectory | None = None

    def cleanup(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


@dataclass
class RepoListing:
    """A repository that offers multiple packages instead of being one."""

    lines: list[str] = field(default_factory=list)


def _validate_segment(value: str, origin: str) -> None:
    valid = (
        SEGMENT_PATTERN.fullmatch(value) is not None
        and set(value) != {"."}
        and value == value.rstrip(" .")
        and not package._is_windows_reserved_component(value)
    )
    if not valid:
        raise RemoteError(f"{origin}: invalid path segment {value!r}.")


def _validate_ref(ref: str, origin: str) -> None:
    for part in ref.split("/"):
        _validate_segment(part, origin)


def _split_ref(text: str, origin: str) -> tuple[str, str]:
    if "@" not in text:
        return text, ""
    base, _, ref = text.rpartition("@")
    if not base or not ref:
        raise RemoteError(f"{origin}: expected <source>@<ref>.")
    _validate_ref(ref, origin)
    return base, ref


def parse_source(argument: str) -> SourceSpec:
    """Parse a non-local package source argument.

    The caller is responsible for checking for an existing local folder
    first; local paths always win over remote interpretations.
    """
    if argument.startswith("github:"):
        body, ref = _split_ref(argument[len("github:") :], argument)
        parts = body.split("/")
        if len(parts) < 2 or not all(parts):
            raise RemoteError(
                f"{argument!r} must look like github:Owner/Repo[/subdir][@ref]."
            )
        for part in parts:
            _validate_segment(part, argument)
        return SourceSpec(
            "github",
            argument,
            owner=parts[0],
            repo=parts[1],
            subdir="/".join(parts[2:]),
            ref=ref,
        )
    lowered = argument.lower()
    if lowered.startswith("http://"):
        raise RemoteError("Only https:// package URLs are supported.")
    if lowered.startswith("https://"):
        try:
            parsed = urllib.parse.urlsplit(argument)
        except ValueError as error:
            raise RemoteError(f"{argument!r} is not a valid URL.") from error
        if parsed.username is not None or parsed.password is not None:
            raise RemoteError(
                "Package URLs may not contain credentials; they would end up "
                "in install records."
            )
        if not parsed.hostname:
            raise RemoteError(f"{argument!r} is not a valid URL.")
        return SourceSpec("url", argument, url=argument)
    if "@" in argument or package.ID_PATTERN.fullmatch(argument):
        base, ref = _split_ref(argument, argument)
        if package.ID_PATTERN.fullmatch(base) and not base.endswith("."):
            _validate_segment(base, argument)
            return SourceSpec("name", argument, name=base, ref=ref)
    raise RemoteError(
        f"{argument!r} is not an existing package folder, a package name, a "
        '"github:Owner/Repo[/subdir][@ref]" spec, or an https tarball URL.'
    )


def _strip_userinfo(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if parsed.username is None and parsed.password is None:
        return url
    host = parsed.hostname or ""
    if parsed.port is not None:
        host += f":{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )


def _check_transport(url: str, origin: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as error:
        raise RemoteError(f"{origin}: {url!r} is not a valid URL.") from error
    scheme = parsed.scheme.lower()
    if scheme != "https" and (_REQUIRE_HTTPS or scheme != "http"):
        raise RemoteError(f"{origin}: refusing non-https URL {_strip_userinfo(url)!r}.")
    if parsed.username is not None or parsed.password is not None:
        raise RemoteError(f"{origin}: URLs with credentials are not allowed.")


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    """Cap redirect hops and require https on every hop."""

    max_redirections = MAX_REDIRECTS
    max_repeats = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_transport(newurl, "redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, *, timeout: int, accept: str | None = None):
    _check_transport(url, "download")
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_SafeRedirects())
    return opener.open(request, timeout=timeout)


def _api_json(path: str) -> Any:
    url = GITHUB_API_ROOT + path
    try:
        with _open(
            url, timeout=API_TIMEOUT, accept="application/vnd.github+json"
        ) as response:
            data = response.read(MAX_API_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        if status == 404:
            raise RemoteNotFound(f"GitHub API: {path} was not found.") from error
        if status in {403, 429}:
            raise ApiUnavailable(
                "the GitHub API rate limit was reached; try again later"
            ) from error
        raise ApiUnavailable(
            f"the GitHub API request failed with HTTP {status}"
        ) from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise ApiUnavailable(f"the GitHub API is unreachable ({error})") from error
    if len(data) > MAX_API_BYTES:
        raise RemoteError("The GitHub API response exceeded the size limit.")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemoteError(f"The GitHub API returned invalid JSON: {error}") from error


def _download(url: str, destination: Path, max_bytes: int) -> str:
    """Stream ``url`` to ``destination`` and return the final (redirect) URL.

    The size cap is enforced while streaming, not after the fact.
    """
    try:
        with _open(url, timeout=DOWNLOAD_TIMEOUT) as response:
            final_url = response.geturl()
            received = 0
            with destination.open("wb") as file:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_bytes:
                        raise RemoteError(
                            f"The download exceeded the "
                            f"{max_bytes // (1024 * 1024)} MB limit; raise "
                            '"package-max-download-mb" if this is intended.'
                        )
                    file.write(chunk)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RemoteNotFound(
                f"{_strip_userinfo(url)} was not found (HTTP 404)."
            ) from error
        raise RemoteError(
            f"The download failed with HTTP {error.code}: {_strip_userinfo(url)}"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RemoteError(f"The download failed: {error}") from error
    return _strip_userinfo(final_url)


def _member_parts(name: str, origin: str) -> tuple[str, ...]:
    text = name.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise RemoteError(f"{origin}: archive entry {name!r} uses an absolute path.")
    parts = tuple(part for part in text.split("/") if part not in ("", "."))
    for part in parts:
        if (
            part == ".."
            or ":" in part
            or part != part.rstrip(" .")
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            or package._is_windows_reserved_component(part)
        ):
            raise RemoteError(
                f"{origin}: archive entry {name!r} is not safe to extract."
            )
    return parts


def _extract_archive(tar_path: Path, destination: Path, max_bytes: int) -> None:
    """Safely extract a .tar.gz archive into ``destination``.

    Rejects absolute paths, traversal, links, devices, unsafe Windows names,
    case-colliding paths, and archives that exceed the size or entry caps.
    Never uses ``extractall``.
    """
    total = 0
    count = 0
    seen: set[str] = set()
    try:
        with tarfile.open(tar_path, mode="r:gz") as archive:
            while True:
                member = archive.next()
                if member is None:
                    break
                count += 1
                if count > MAX_ARCHIVE_ENTRIES:
                    raise RemoteError(
                        f"The archive has more than {MAX_ARCHIVE_ENTRIES} "
                        "entries; refusing to extract it."
                    )
                parts = _member_parts(member.name, tar_path.name)
                if member.isdir():
                    continue
                if not member.isreg():
                    raise RemoteError(
                        f"Archive entry {member.name!r} is not a regular file; "
                        "links and devices are not allowed."
                    )
                if not parts:
                    raise RemoteError(f"Archive entry {member.name!r} names no file.")
                total += member.size
                if total > max_bytes:
                    raise RemoteError(
                        f"The archive decompresses beyond the "
                        f"{max_bytes // (1024 * 1024)} MB limit; raise "
                        '"package-max-download-mb" if this is intended.'
                    )
                folded = "/".join(parts).casefold()
                if folded in seen:
                    raise RemoteError(
                        f"Archive entries collide ignoring case: {member.name!r}."
                    )
                seen.add(folded)
                target = destination.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RemoteError(f"Could not read archive entry {member.name!r}.")
                with extracted, target.open("wb") as file:
                    shutil.copyfileobj(extracted, file, 65536)
    except (tarfile.TarError, EOFError) as error:
        raise RemoteError(f"Could not extract {tar_path.name}: {error}") from error
    except OSError as error:
        raise RemoteError(f"Could not extract {tar_path.name}: {error}") from error


def _archive_root(tree: Path) -> Path:
    entries = list(tree.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return tree


def _quote_ref(ref: str) -> str:
    return urllib.parse.quote(ref, safe="/")


def _split_repo(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2:
        raise RemoteError(
            'The "package-default-repo" setting must look like "Owner/Repo".'
        )
    for part in parts:
        _validate_segment(part, '"package-default-repo"')
    return parts[0], parts[1]


def _index_packages(index_path: Path) -> dict[str, dict[str, str]]:
    value = package._load_json(index_path)
    packages = value.get("packages") if isinstance(value, dict) else None
    if not isinstance(packages, dict):
        raise RemoteError(f'{index_path.name} must contain a "packages" object.')
    validated: dict[str, dict[str, str]] = {}
    for name, entry in packages.items():
        if not isinstance(name, str) or not package.ID_PATTERN.fullmatch(name):
            raise RemoteError(f"{index_path.name} lists an invalid name: {name!r}.")
        if not isinstance(entry, dict):
            raise RemoteError(
                f"{index_path.name}: entry for {name!r} must be an object."
            )
        path = entry.get("path", name)
        if not isinstance(path, str) or not path:
            raise RemoteError(
                f"{index_path.name}: entry for {name!r} has an invalid path."
            )
        for part in path.split("/"):
            _validate_segment(part, f"{index_path.name}: {name!r}")
        desc = entry.get("desc", "")
        if not isinstance(desc, str):
            raise RemoteError(
                f"{index_path.name}: entry for {name!r} has an invalid desc."
            )
        validated[name] = {"path": path, "desc": desc}
    return validated


def _index_entry(index_path: Path, name: str) -> str | None:
    packages = _index_packages(index_path)
    if name in packages:
        return packages[name]["path"]
    folded = name.casefold()
    for key, entry in packages.items():
        if key.casefold() == folded:
            return entry["path"]
    return None


def _listing(index_path: Path, owner: str, repo: str, ref: str) -> RepoListing:
    packages = _index_packages(index_path)
    lines = [f"Available packages in {owner}/{repo}@{ref}:"]
    if not packages:
        lines.append("  (none listed)")
    for name in sorted(packages):
        desc = packages[name]["desc"]
        lines.append(f"  {name} - {desc}" if desc else f"  {name}")
    lines.append("")
    lines.append(
        f"Install one with: python SyKit package add github:{owner}/{repo}/<path>@{ref}"
    )
    return RepoListing(lines)


def _classify_ref(owner: str, repo: str, ref: str) -> str | None:
    if SHA_PATTERN.fullmatch(ref):
        return "sha"
    try:
        _api_json(f"/repos/{owner}/{repo}/git/ref/tags/{_quote_ref(ref)}")
        return "tag"
    except RemoteNotFound:
        if SHORT_SHA_PATTERN.fullmatch(ref):
            return "sha"
        return "branch"
    except ApiUnavailable:
        return None


def _locate_package(
    root: Path,
    spec: SourceSpec,
    bare_name: str,
    ref: str,
) -> tuple[Path, str] | RepoListing:
    owner, repo = spec.owner, spec.repo
    if spec.subdir:
        candidate = root.joinpath(*spec.subdir.split("/"))
        if not (candidate / package.MANIFEST_NAME).is_file():
            raise RemoteError(
                f"{spec.subdir!r} in {owner}/{repo}@{ref} does not contain "
                f"{package.MANIFEST_NAME}."
            )
        return candidate, spec.subdir
    if bare_name:
        index_path = root / INDEX_NAME
        subdir = bare_name
        if index_path.is_file():
            mapped = _index_entry(index_path, bare_name)
            if mapped is not None:
                subdir = mapped
        candidate = root.joinpath(*subdir.split("/"))
        if not (candidate / package.MANIFEST_NAME).is_file():
            raise RemoteError(
                f"Package {bare_name!r} was not found in {owner}/{repo}@{ref}."
            )
        return candidate, subdir
    if (root / package.MANIFEST_NAME).is_file():
        return root, ""
    index_path = root / INDEX_NAME
    if index_path.is_file():
        return _listing(index_path, owner, repo, ref)
    raise RemoteError(
        f"{owner}/{repo}@{ref} has neither {package.MANIFEST_NAME} nor "
        f"{INDEX_NAME} at its root."
    )


def _resolve_github(
    spec: SourceSpec,
    settings: dict[str, Any],
    *,
    bare_name: str = "",
    prefer_release: bool = False,
) -> RemotePackage | RepoListing:
    owner, repo = spec.owner, spec.repo
    notes: list[str] = []
    official = f"{owner}/{repo}".casefold() == settings["default-repo"].casefold()
    if official:
        notes.append(f"Origin: official packages repo {owner}/{repo}")
    else:
        notes.append(f"Origin: third-party GitHub repository {owner}/{repo}")

    ref = spec.ref
    ref_type: str | None = None
    sha: str | None = None
    api_down = False
    if not ref and prefer_release:
        try:
            release = _api_json(f"/repos/{owner}/{repo}/releases/latest")
            tag = release.get("tag_name") if isinstance(release, dict) else None
            if isinstance(tag, str) and tag:
                _validate_ref(tag, "release tag")
                ref = tag
                ref_type = "tag"
        except RemoteNotFound:
            pass
        except ApiUnavailable as error:
            api_down = True
            notes.append(f"Warning: {error}.")
    if not ref and not api_down:
        try:
            info = _api_json(f"/repos/{owner}/{repo}")
            branch = info.get("default_branch") if isinstance(info, dict) else None
            if not isinstance(branch, str) or not branch:
                raise RemoteError(
                    f"Could not determine the default branch of {owner}/{repo}."
                )
            _validate_ref(branch, "default branch")
            ref = branch
            ref_type = "branch"
        except RemoteNotFound as error:
            raise RemoteError(
                f"GitHub repository {owner}/{repo} was not found."
            ) from error
        except ApiUnavailable as error:
            api_down = True
            notes.append(f"Warning: {error}.")
    if not ref:
        ref = "HEAD"
    if ref != "HEAD" and not api_down:
        try:
            commit = _api_json(f"/repos/{owner}/{repo}/commits/{_quote_ref(ref)}")
            value = commit.get("sha") if isinstance(commit, dict) else None
            if isinstance(value, str) and SHA_PATTERN.fullmatch(value):
                sha = value
            if ref_type is None:
                ref_type = _classify_ref(owner, repo, ref)
        except RemoteNotFound as error:
            raise RemoteError(
                f"{owner}/{repo} has no branch, tag, or commit {ref!r}."
            ) from error
        except ApiUnavailable as error:
            notes.append(f"Warning: {error}.")
    if sha is None:
        notes.append(
            f"Warning: installing from moving ref {ref!r} without a pinned "
            "commit; contents may change between installs."
        )
    elif ref_type == "branch":
        notes.append(
            f"Warning: installed from branch {ref!r}; the branch moves and "
            "future installs may differ."
        )

    download_ref = sha if sha else ref
    temporary = tempfile.TemporaryDirectory(
        prefix="sykit-remote-", ignore_cleanup_errors=True
    )
    try:
        base = Path(temporary.name)
        tar_path = base / "package.tar.gz"
        url = (
            f"{GITHUB_ARCHIVE_ROOT}/{owner}/{repo}/archive/"
            f"{_quote_ref(download_ref)}.tar.gz"
        )
        try:
            _download(url, tar_path, settings["max-download-bytes"])
        except RemoteNotFound as error:
            raise RemoteError(
                f"Could not download {owner}/{repo} at {download_ref!r}: {error}"
            ) from error
        tree = base / "tree"
        tree.mkdir()
        _extract_archive(tar_path, tree, settings["max-download-bytes"])
        tar_path.unlink(missing_ok=True)
        root = _archive_root(tree)
        located = _locate_package(root, spec, bare_name, ref)
        if isinstance(located, RepoListing):
            temporary.cleanup()
            return located
        directory, subdir_used = located
        spec_text = f"github:{owner}/{repo}"
        if subdir_used:
            spec_text += f"/{subdir_used}"
        spec_text += f"@{ref}"
        source = {
            "spec": spec_text,
            "kind": "github",
            "resolved_sha": sha,
            "ref_type": ref_type,
        }
        return RemotePackage(directory, source, notes, temporary)
    except BaseException:
        temporary.cleanup()
        raise


def _resolve_url(spec: SourceSpec, settings: dict[str, Any]) -> RemotePackage:
    clean_spec = _strip_userinfo(spec.url)
    host = urllib.parse.urlsplit(clean_spec).hostname or "unknown host"
    notes = [
        f"Origin: third-party URL ({host})",
        "Warning: installing from a plain URL; there is no commit pin and "
        "contents may change between installs.",
    ]
    temporary = tempfile.TemporaryDirectory(
        prefix="sykit-remote-", ignore_cleanup_errors=True
    )
    try:
        base = Path(temporary.name)
        tar_path = base / "package.tar.gz"
        final_url = _download(spec.url, tar_path, settings["max-download-bytes"])
        tree = base / "tree"
        tree.mkdir()
        _extract_archive(tar_path, tree, settings["max-download-bytes"])
        tar_path.unlink(missing_ok=True)
        root = _archive_root(tree)
        if not (root / package.MANIFEST_NAME).is_file():
            raise RemoteError(
                f"The archive at {clean_spec} does not contain {package.MANIFEST_NAME}."
            )
        source = {
            "spec": clean_spec,
            "kind": "url",
            "resolved_sha": None,
            "ref_type": None,
            "final_url": final_url,
        }
        return RemotePackage(root, source, notes, temporary)
    except BaseException:
        temporary.cleanup()
        raise


def resolve(argument: str, settings: dict[str, Any]) -> RemotePackage | RepoListing:
    """Resolve a non-local package source to a fetched package folder.

    Returns a RepoListing when the source is a multi-package repository
    without a chosen package. Raises RemoteError (a PackageError) on failure.
    """
    spec = parse_source(argument)
    if spec.kind == "url":
        return _resolve_url(spec, settings)
    if spec.kind == "github":
        return _resolve_github(spec, settings)
    owner, repo = _split_repo(settings["default-repo"])
    github_spec = SourceSpec(
        "github", spec.argument, owner=owner, repo=repo, ref=spec.ref
    )
    return _resolve_github(
        github_spec, settings, bare_name=spec.name, prefer_release=True
    )
