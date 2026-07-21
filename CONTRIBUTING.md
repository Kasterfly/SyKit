# Contributing

Small, focused changes with tests are welcome.

## Development setup

```text
git clone https://github.com/Kasterfly/SyKit
cd SyKit
python -m pip install --require-hashes -r requirements-dev.lock
```

Install Node.js 22.12+ or 24. The quick-start build installs the exact
frontend tree from `files/frontend-build/package-lock.json`.

## Checks

Run these before opening a pull request:

```text
ruff check .
ruff format --check .
python -m coverage run -m unittest discover -s tests
python -m coverage report
python tests/smoke_quickstart.py
```

Changes to browser/server integration should also run
`python tests/e2e_quickstart.py` after installing the matching Chromium with
`python -m playwright install chromium`. Docker changes should build and start
a generated quick-start image.

## Dependencies

Edit `requirements.in` or `requirements-dev.in`, then regenerate the matching
lock with pip-tools and `--generate-hashes`. Do not hand-edit a lockfile. Keep
frontend versions exact and update `package-lock.json` with npm.

## Pull requests

- Explain the user-visible problem and the chosen behavior.
- Add regression tests for fixes and docs for public behavior.
- Keep unrelated formatting or refactors out of the change.
- Update `CHANGELOG.md` and detailed release notes when behavior changes.

Report vulnerabilities privately through `SECURITY.md`.
