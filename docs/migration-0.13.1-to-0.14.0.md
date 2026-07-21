# Migrating from SyKit 0.13.1 to 0.14.0

SyKit 0.14.0 is a compatibility freeze, not an application feature release.
Endpoint code, configuration values, cookies, password hashes, API keys,
tasks, and SQLite state from 0.13.1 remain compatible.

## Before updating

1. Use Python 3.11 through 3.14.
2. Use Node.js 22.12+ or 24.x. Node 20 is no longer supported.
3. Back up application data and confirm `python SyKit version` reports
   `0.13.1`.
4. Run `python SyKit package list` and keep the source of each installed
   package available.

## Update

Use the normal updater or install the reviewed 0.14.0 release package. The
updater removes installed packages, replaces the SyKit core, and reapplies
compatible packages from their stored copies.

Afterward, run:

```text
python SyKit version
python SyKit package list
python SyKit build
python -m pip check
```

## Package authors

The existing `sykit-req` remains the inclusive minimum. The new
`sykit-before` is an exclusive upper bound:

```json
{
    "id": "example",
    "sykit-req": "0.14.0",
    "sykit-before": "2.0.0"
}
```

This package supports SyKit versions from 0.14.0 up to, but not including,
2.0.0. Update package-repository validators before publishing manifests that
use the new key because handlers before 0.14.0 reject unknown manifest keys.

## Dependency change

`httpx2` is no longer a runtime dependency. It remains in the development lock
for Starlette test-client use. Applications that directly import it must
declare it themselves; it was never part of SyKit's public runtime API.

## Rollback

The 0.14.0 package is reversible to the exact 0.13.1 tree. A downgrade through
the updater refuses to reapply any package whose `sykit-req` requires 0.14.0 or
newer. No persisted schema downgrade is needed for this release.
