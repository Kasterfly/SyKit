# SyKit 0.13.0 - Release Hardening

SyKit 0.13.0 hardens the beta before the compatibility freeze and release
candidate soak. It does not declare the framework stable.

## Security

- Updates require a full commit SHA before downloading. API outages and rate
  limits abort even when `--yes` is used.
- Branch updates require `--allow-unreleased`; full SHAs and release tags stay
  explicit, pinned sources.
- Package edits and rollbacks remove matching cached bytecode so Python cannot
  reuse a stale module after a same-second source edit.
- The package and self-update subsystems are now included in the security
  review scope. `SECURITY.md` provides a private reporting route.

## Reproducible builds

- `requirements.in` and `requirements-dev.in` hold supported ranges and tool
  choices. Their lockfiles pin all resolved packages with hashes.
- CI, generated applications, and Docker install the lockfiles with hash
  verification.
- Generated Dockerfiles pin Python 3.12.13 slim-trixie by OCI index digest and
  run the application as the unprivileged `sykit` user.
- CI builds and starts a generated image, checks its health endpoint, and
  verifies its user is not root.

## Compatibility and state

- Unknown top-level config keys are rejected. `extensions` is the reserved
  namespace for package-specific data.
- SQLite sessions, API keys, tasks, and rate limits use the shared
  `sykit_schema_versions` metadata table with independent component versions.
- 0.12.2 database fixtures are upgraded in tests without losing stored rows.
  Unknown newer schema versions fail closed.
- `docs/compatibility.md` records the candidate 1.0 API and data contract.

## Test and project hygiene

- CI covers Python 3.10 through 3.14 and Node.js 20.19, 22.12, and 24.
- Branch coverage has a 60 percent minimum gate. The first measured baseline
  is 64 percent, leaving room for platform-specific paths while preventing
  regressions.
- A Playwright suite builds a real app, starts its server, and drives session
  persistence, login and hidden endpoints, upload, and SSE through a local
  reverse proxy.
- Release consistency, contribution, support, security, issue-template,
  changelog, and migration documents were added.
