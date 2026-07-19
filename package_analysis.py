"""Static pre-install analysis for SyKit packages.

Everything here is a read-only inspection of a package folder: nothing is
executed, and the live SyKit tree is never touched. The rules catch accidents
and lazy attacks, not determined attackers; installing a package still means
trusting its code.

Rules are kept as data (pattern lists and path sets) so adding one is a
one-line change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import package

# --- Path rules ------------------------------------------------------------

# SyKit tool code: a package touching these can change how SyKit itself
# behaves, including this analyzer and the package handler.
CORE_FILES = frozenset(
    {
        "__main__.py",
        "build.py",
        "check_requirements.py",
        "help.py",
        "init.py",
        "package.py",
        "package_analysis.py",
        "package_remote.py",
    }
)
CORE_PREFIXES = ("sykit/",)
CONFIG_FILES = frozenset({"sykit/config.json"})
CI_PREFIXES = (".github/",)
DEPS_FILES = frozenset({"requirements.txt", "requirements-dev.txt", "pyproject.toml"})
SCRIPT_SUFFIXES = frozenset({".sh", ".bash", ".bat", ".cmd", ".ps1"})
GIT_CONFIG_NAMES = frozenset({".gitmodules", ".gitattributes"})
EDITOR_AUTORUN_FILES = frozenset(
    {".vscode/tasks.json", ".vscode/settings.json", ".vscode/launch.json"}
)

# --- Content rules ---------------------------------------------------------

CODE_SUFFIXES = frozenset({".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".svelte"})
DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})
ALLOWED_URL_HOSTS = frozenset(
    {"github.com", "raw.githubusercontent.com", "pypi.org", "npmjs.com"}
)
EXEC_PATTERNS = (
    "subprocess",
    "os.system",
    "os.popen",
    "eval(",
    "exec(",
    "compile(",
    "importlib",
    "ctypes",
    "socket",
)
ENV_PATTERNS = ("os.environ", "os.getenv", "getenv(", "process.env", "$env:")
SESSION_SECRET_NAME = "SYKIT_SESSION_SECRET"
URL_PATTERN = re.compile(r"https?://[^\s\"'<>()\[\]{}`]+", re.IGNORECASE)
RAW_IP_PATTERN = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
BLOB_THRESHOLD = 200
BLOB_PATTERN = re.compile(r"[A-Za-z0-9+/=_-]{%d,}" % BLOB_THRESHOLD)
MAX_URL_FINDINGS_PER_FILE = 5
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    severity: str  # "critical" | "warning" | "info"
    code: str  # stable id, e.g. "core-edit", "url", "exec-call"
    action: str  # "add" | "edit" | "remove"
    path: str  # target path inside SyKit that triggered it
    detail: str  # human-readable, one line


@dataclass
class Operation:
    """One effective package operation, as review-relevant data.

    ``chunks`` holds only the text the operation introduces into SyKit: added
    file content, edit payloads, and inline ``content`` strings from edit
    instruction files. Instruction files themselves are never treated as
    payload content.
    """

    target: str
    action: str
    chunks: tuple[str, ...]
    binary: bool
    replace_file: bool
    edit_actions: tuple[str, ...]
    size: int


def _decode_or_none(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def collect_operations(package_dir: Path) -> list[Operation]:
    """Walk a package folder and return its effective operations.

    Mirrors the traversal of the package handler's planning step, but never
    reads or requires the live SyKit tree, so it can run on any resolved
    package folder before anything is applied.
    """
    operations: list[Operation] = []

    add_root = package_dir / package.ADD_DIR
    if add_root.is_dir():
        for source in package._package_files(add_root):
            target = package._normalize_target(
                source.relative_to(add_root).as_posix(), str(source)
            )
            data = source.read_bytes()
            text = _decode_or_none(data)
            operations.append(
                Operation(
                    target,
                    "add",
                    (text,) if text is not None else (),
                    text is None,
                    False,
                    (),
                    len(data),
                )
            )

    edit_root = package_dir / package.EDIT_DIR
    if edit_root.is_dir():
        files = package._package_files(edit_root)
        available = set(files)
        companions = {
            path
            for path in files
            if path.suffix == ".json" and path.with_suffix("") in available
        }
        for source in files:
            if source in companions:
                continue
            target = package._normalize_target(
                source.relative_to(edit_root).as_posix(), str(source)
            )
            companion = source.parent / (source.name + ".json")
            if companion in companions:
                specs = package._edit_operations(
                    package._load_json(companion), str(companion)
                )
            else:
                specs = [{"action": "replace-file", "anchor": None, "content": None}]
            data = source.read_bytes()
            payload_text = _decode_or_none(data)
            chunks: list[str] = [] if payload_text is None else [payload_text]
            size = len(data)
            labels: list[str] = []
            for spec in specs:
                labels.append(str(spec["action"]))
                inline = spec["content"]
                if inline is not None:
                    chunks.append(inline)
                    size += len(inline.encode("utf-8"))
            operations.append(
                Operation(
                    target,
                    "edit",
                    tuple(chunks),
                    payload_text is None,
                    "replace-file" in labels,
                    tuple(labels),
                    size,
                )
            )

    remove_root = package_dir / package.REMOVE_DIR
    if remove_root.is_dir():
        for list_path in package._package_files(remove_root):
            if list_path.suffix != ".json":
                continue
            value = package._load_json(list_path)
            if not isinstance(value, list):
                continue
            for raw in value:
                target = package._normalize_target(raw, str(list_path))
                operations.append(Operation(target, "remove", (), False, False, (), 0))

    return operations


def _suffix(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1].casefold() if "." in name else ""


def _is_core_target(folded: str) -> bool:
    return folded in CORE_FILES or any(
        folded.startswith(prefix) for prefix in CORE_PREFIXES
    )


def _path_findings(operation: Operation) -> list[Finding]:
    findings: list[Finding] = []
    target = operation.target
    action = operation.action
    folded = target.casefold()
    basename = folded.rsplit("/", 1)[-1]

    if folded in CONFIG_FILES:
        findings.append(
            Finding(
                "critical",
                "config-edit",
                action,
                target,
                "changes live SyKit configuration; settings like "
                '"package-default-repo" quietly persist into later installs',
            )
        )
    elif _is_core_target(folded):
        if basename == "package.py":
            detail = (
                "modifies the package handler itself; the reversibility of "
                "later installs can no longer be guaranteed"
            )
        elif basename in {"package_analysis.py", "package_remote.py"}:
            detail = "modifies the package analyzer or downloader itself"
        else:
            detail = "changes SyKit tool code"
        findings.append(Finding("critical", "core-edit", action, target, detail))
    if any(folded.startswith(prefix) for prefix in CI_PREFIXES):
        findings.append(
            Finding(
                "critical",
                "ci-edit",
                action,
                target,
                "changes CI configuration; CI executes code on push",
            )
        )
    if folded in DEPS_FILES:
        findings.append(
            Finding(
                "critical",
                "deps-edit",
                action,
                target,
                "changes dependency lists; new dependencies are new runtime code",
            )
        )
    if action == "add" and _suffix(basename) in SCRIPT_SUFFIXES:
        findings.append(
            Finding(
                "warning",
                "script-file",
                action,
                target,
                "adds an executable script; the code content rules do not "
                "scan shell scripts",
            )
        )
    if action in {"add", "edit"} and basename in GIT_CONFIG_NAMES:
        findings.append(
            Finding(
                "warning",
                "git-remote-config",
                action,
                target,
                "submodules and filter drivers route future git operations "
                "toward external code",
            )
        )
    if action in {"add", "edit"} and folded in EDITOR_AUTORUN_FILES:
        findings.append(
            Finding(
                "warning",
                "editor-config",
                action,
                target,
                "editor workspace files can auto-run commands when the "
                "folder is opened",
            )
        )
    if action == "remove":
        findings.append(
            Finding(
                "warning",
                "remove",
                action,
                target,
                "removes this file from SyKit",
            )
        )
    if action == "edit" and operation.replace_file:
        findings.append(
            Finding(
                "warning",
                "replace-file",
                action,
                target,
                "replaces the entire file instead of an anchored edit",
            )
        )
    return findings


def _classify_url(url: str, doc_only: bool) -> tuple[str, str]:
    display = url if len(url) <= 100 else url[:97] + "..."
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        scheme = parsed.scheme.lower()
    except ValueError:
        return ("warning", f"{display} (unparseable URL)")
    if RAW_IP_PATTERN.fullmatch(host) or ":" in host:
        return ("warning", f"{display} (raw IP address)")
    allowed = scheme == "https" and (
        host in ALLOWED_URL_HOSTS
        or any(host.endswith("." + entry) for entry in ALLOWED_URL_HOSTS)
    )
    if allowed or doc_only:
        return ("info", display)
    return ("warning", display)


def _content_findings(operation: Operation) -> list[Finding]:
    findings: list[Finding] = []
    if operation.action == "remove":
        return findings
    target = operation.target
    action = operation.action
    basename = target.casefold().rsplit("/", 1)[-1]
    suffix = _suffix(basename)

    if operation.binary:
        findings.append(
            Finding(
                "warning",
                "opaque-blob",
                action,
                target,
                f"binary content ({operation.size} bytes) cannot be reviewed as text",
            )
        )
    text = "\n".join(operation.chunks)
    if not text:
        return findings

    doc_only = (
        suffix in DOC_SUFFIXES
        or basename.startswith("readme")
        or basename.startswith("license")
    )
    seen_urls: set[str] = set()
    url_findings: list[Finding] = []
    for match in URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        severity, detail = _classify_url(url, doc_only)
        url_findings.append(Finding(severity, "url", action, target, detail))
    if len(url_findings) > MAX_URL_FINDINGS_PER_FILE:
        skipped = len(url_findings) - MAX_URL_FINDINGS_PER_FILE
        worst = min(
            (finding.severity for finding in url_findings),
            key=lambda severity: SEVERITY_ORDER[severity],
        )
        url_findings = url_findings[:MAX_URL_FINDINGS_PER_FILE]
        url_findings.append(
            Finding(worst, "url", action, target, f"and {skipped} more URL(s)")
        )
    findings.extend(url_findings)

    if suffix in CODE_SUFFIXES:
        for pattern in EXEC_PATTERNS:
            if pattern in text:
                findings.append(
                    Finding(
                        "warning",
                        "exec-call",
                        action,
                        target,
                        f"contains {pattern!r}, which can run or load external code",
                    )
                )
    if SESSION_SECRET_NAME in text:
        findings.append(
            Finding(
                "warning",
                "env-read",
                action,
                target,
                f"references {SESSION_SECRET_NAME}; leaking it compromises "
                "every session of a built app",
            )
        )
    for pattern in ENV_PATTERNS:
        if pattern in text:
            findings.append(
                Finding(
                    "warning",
                    "env-read",
                    action,
                    target,
                    f"reads environment variables ({pattern!r})",
                )
            )
            break
    if not operation.binary:
        match = BLOB_PATTERN.search(text)
        if match:
            findings.append(
                Finding(
                    "warning",
                    "opaque-blob",
                    action,
                    target,
                    f"contains an opaque {len(match.group(0))}-character "
                    "literal (content hidden from review)",
                )
            )
    return findings


def analyze_operations(
    operations: list[Operation],
    deps: tuple[str, ...] | list[str] = (),
) -> list[Finding]:
    findings: list[Finding] = []
    for operation in operations:
        findings.extend(_path_findings(operation))
        findings.extend(_content_findings(operation))
    for entry in deps:
        findings.append(
            Finding(
                "warning",
                "dependency",
                "manifest",
                package.MANIFEST_NAME,
                f"declares runtime dependency {entry!r}; SyKit does not "
                "install dependencies",
            )
        )
    findings.sort(
        key=lambda finding: (
            SEVERITY_ORDER[finding.severity],
            finding.path,
            finding.code,
        )
    )
    return findings


def analyze_package(package_dir: Path) -> tuple[list[Operation], list[Finding]]:
    operations = collect_operations(package_dir)
    manifest = package._load_manifest(package_dir)
    return operations, analyze_operations(operations, manifest.deps)


def render_details(operations: list[Operation]) -> list[str]:
    """Render the content every operation introduces, for review.

    The caller is expected to sanitize each line before printing; payload
    content is attacker-authored.
    """
    lines: list[str] = []
    for operation in operations:
        header = f"=== {operation.action} {operation.target}"
        if operation.edit_actions:
            header += f" ({', '.join(operation.edit_actions)})"
        header += " ==="
        lines.append(header)
        if operation.action == "remove":
            lines.append("(file is deleted)")
        elif operation.binary:
            lines.append(f"(binary content, {operation.size} bytes; not shown)")
        if operation.chunks:
            for chunk in operation.chunks:
                lines.extend(chunk.splitlines() or [""])
        lines.append("")
    return lines
