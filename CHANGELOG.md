# Changelog

All notable SyKit changes are listed here. Detailed release notes remain under
`docs/changelogs/`.

## 0.14.2 - 2026-07-21

- Prevented anonymous rate-limit traffic from creating server-side session
  rows and kept the per-session identity across login.
- Capped task crash recovery attempts and payload size, and expired old failed
  task records.
- Rejected duplicate API key headers and removed long-lived caching from
  permission-protected assets.
- Hardened password-hash validation, Windows Node/npm resolution, generated
  ignore files, and release security guidance.

## 0.14.1 - 2026-07-20

- Fixed the unit-test matrix to install the development dependency lock.
- Kept Starlette's test-client HTTP transport out of production installs.
- Added a regression check that separates unit-test and container dependency
  jobs.

## 0.14.0 - 2026-07-20

- Added an exclusive `sykit-before` package compatibility bound.
- Froze the candidate 1.0 API, CLI, package, generated-layout, and data
  contracts for the 0.14.x soak.
- Moved the supported floor to Python 3.11 and LTS Node 22/24.
- Removed the test-client HTTP transport from runtime dependencies.
- Raised the branch coverage floor to the measured 64 percent baseline.
- Added beta-to-release-candidate migration, support, and release guidance.

## 0.13.1 - 2026-07-20

- Fixed the container CI job to invoke SyKit from the checkout root.
- Added a regression test for the checkout-root command paths.

## 0.13.0 - 2026-07-20

- Made self-updates fail closed unless the source resolves to a full commit.
- Invalidated stale Python bytecode after package edits and rollbacks.
- Added hash-locked backend dependencies and hardened generated containers.
- Added versioned SQLite migrations and strict top-level config validation.
- Added browser, container, compatibility, coverage, and release checks.
- Added security, support, contribution, migration, and compatibility docs.

## 0.12.2 - 2026-07-20

- Closed hidden-endpoint method probing gaps.
- Charged failed authentication requests against configured rate limits.
- Added trusted reverse-proxy support and security-event logging.

## Earlier beta releases

See `docs/changelogs/` for 0.1.0 through 0.12.1 release notes.
