# SyKit 0.14.0 - Compatibility Freeze

SyKit 0.14.0 freezes the candidate compatibility contract for a short,
patch-only soak before 1.0. It does not add application features.

## Package compatibility

- Packages may declare `"sykit-before": "2.0.0"` as an exclusive upper
  SyKit version bound alongside the existing minimum `sykit-req`.
- Package add refuses an incompatible minimum or upper bound before analysis,
  confirmation, or filesystem changes.
- Self-update applies both bounds before reapplying installed packages. An
  incompatible package stays uninstalled with a package-specific reason.
- Install records and `package list` preserve and display both bounds. Records
  created by older handlers default to no upper bound.

## Supported environments

- The supported Python floor is now 3.11. Python 3.11 through 3.14 are tested.
- Supported Node.js versions are LTS 22.12+ and 24.x.
- End-of-life Node 20 and untested odd or future Node lines are rejected.
- The frontend manifest, lockfile, CI matrix, requirements checks, and docs use
  the same supported versions.

## Runtime dependency scope

- `httpx2` remains locked for development because Starlette's test client uses
  it, but generated runtime installs no longer include that test-only HTTP
  transport.
- Python and frontend dependencies remain hash-locked or exactly pinned.

## Stability gates

- The compatibility document now defines the frozen Python, browser, CLI,
  generated-layout, configuration, package, persistence, and environment
  contracts proposed for 1.0.
- The branch coverage floor is raised from 60 to 64 percent so it cannot fall
  below the measured release-hardening baseline.
- Release guidance requires all supported-runtime, lint, audit, coverage,
  browser, and container checks before a protected release tag.

## Upgrade notes

Read [Migrating from 0.13.1 to 0.14.0](../migration-0.13.1-to-0.14.0.md).
Existing applications and persisted data need no format conversion. Review the
new Python and Node floors, and add `sykit-before` to packages intended for a
bounded major line.
