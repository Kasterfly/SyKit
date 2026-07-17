from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str, cwd: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT), *arguments],
        cwd=cwd,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sykit-quickstart-") as directory:
        project = Path(directory)
        run("init", cwd=project)
        run("build", cwd=project)
        required = [
            project / "built" / "main.py",
            project / "built" / "server.py",
            project / "built" / "static" / "index.html",
            project / "built" / "core" / "endpoints.mjs",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise SystemExit("Quick-start build is missing: " + ", ".join(missing))
        shipped_lock = json.loads(
            (ROOT / "files" / "frontend-build" / "package-lock.json").read_text(
                encoding="utf-8"
            )
        )
        cache_lock = json.loads(
            (project / "__sykitcache__" / "package-lock.json").read_text(
                encoding="utf-8"
            )
        )
        if cache_lock != shipped_lock:
            raise SystemExit("Quick-start build did not use the shipped lockfile.")
    print("Quick-start smoke test passed.")


if __name__ == "__main__":
    main()
