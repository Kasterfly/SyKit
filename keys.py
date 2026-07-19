"""Manage API keys for the current project.

    python SyKit keys generate <name> [--scopes a,b]
    python SyKit keys list
    python SyKit keys revoke <key-id>

Keys authenticate external callers of @api_key @web_hook endpoints via
the X-API-Key header. Only a hash of each key is stored; the key itself
is printed once at generation. The store comes from the project's
"apikey-store" setting (default: .sykit-apikeys.sqlite3 in the project
root, which survives rebuilds).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
FILES_DIR = TOOL_DIR / "files"
if str(FILES_DIR) not in sys.path:
    # Makes files/core importable as the "core" package, the same name it
    # has inside a built app, so key store packages work from the CLI too.
    sys.path.insert(0, str(FILES_DIR))

from core import _apikeys  # noqa: E402


def _store_spec() -> str:
    import build

    sykit_dir = build.find_sykit_dir(build.SRC_DIR)
    if sykit_dir is None:
        return ""
    config_path = sykit_dir / "config.json"
    if not config_path.is_file():
        return ""
    spec = build.load_config(config_path).get("apikey-store", "")
    return spec if isinstance(spec, str) else ""


def _store() -> _apikeys.ApiKeyStore:
    return _apikeys.resolve_key_store(_store_spec(), Path.cwd())


def _command_generate(name: str, scopes: list[str]) -> None:
    key, record = _apikeys.issue_key(_store(), name, scopes)
    print(f"Generated API key '{record['name']}' (id {record['id']}).")
    if record["scopes"]:
        print(f"Scopes: {', '.join(record['scopes'])}")
    print("The key is shown once and only its hash is stored:")
    print(f"  {key}")
    print('Callers send it as the "X-API-Key" header.')


def _command_list() -> None:
    records = _store().list_keys()
    if not records:
        print("No API keys.")
        return
    print(f"API keys ({len(records)}):")
    for record in records:
        created = datetime.fromtimestamp(record["created"], timezone.utc).strftime(
            "%Y-%m-%d"
        )
        line = f"  {record['id']}  {record['name']}  (created {created})"
        if record["scopes"]:
            line += f"  scopes: {', '.join(record['scopes'])}"
        if record["revoked"]:
            line += "  REVOKED"
        print(line)


def _command_revoke(key_id: str) -> None:
    if not _store().revoke(key_id):
        raise _apikeys.ApiKeyError(f"No API key with id {key_id!r}.")
    print(f"Revoked API key {key_id}.")


def print_keys_help() -> None:
    print("Usage: python SyKit keys <command>")
    print("Commands:")
    print("  generate <name> [--scopes a,b]  Create a key; it is printed once")
    print("  list                            Show keys, scopes, and status")
    print("  revoke <key-id>                 Deactivate a key immediately")


def run(arguments: list[str]) -> bool:
    if not arguments or arguments[0].lower() == "help":
        print_keys_help()
        return True
    command, extra = arguments[0].lower(), arguments[1:]
    try:
        if command == "generate" and extra:
            scopes: list[str] = []
            positional: list[str] = []
            index = 0
            while index < len(extra):
                argument = extra[index]
                if argument.lower() == "--scopes":
                    if index + 1 >= len(extra):
                        print("--scopes needs a comma-separated list.")
                        return False
                    scopes = [
                        scope.strip()
                        for scope in extra[index + 1].split(",")
                        if scope.strip()
                    ]
                    index += 2
                    continue
                if argument.startswith("--"):
                    print(f"Unknown keys generate option: {argument}")
                    return False
                positional.append(argument)
                index += 1
            if len(positional) != 1:
                print_keys_help()
                return False
            _command_generate(positional[0], scopes)
            return True
        if command == "list" and not extra:
            _command_list()
            return True
        if command == "revoke" and len(extra) == 1:
            _command_revoke(extra[0])
            return True
    except (_apikeys.ApiKeyError, RuntimeError, OSError) as error:
        print(f"Keys command failed: {error}")
        return False
    print_keys_help()
    return False
