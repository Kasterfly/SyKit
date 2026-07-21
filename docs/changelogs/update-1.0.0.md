# SyKit 1.0.0 Stable Release

SyKit 1.0.0 declares the behavior-tested 0.14.2 contract stable. The version
bump changes no framework runtime code, dependency, configuration, generated
layout, public API, or persistent format.

## Stable contract

- The public Python, browser, CLI, configuration, package, generated-layout,
  and persistent-data contracts are documented in `docs/compatibility.md`.
- The 1.0.x line accepts compatible security, bug, dependency, runtime, and
  documentation corrections. New framework features and breaking changes
  belong in v2.
- Python 3.11 through 3.14 and Node.js 22.12+ or 24.x remain the supported and
  tested runtime lines.
- SyKit remains a side-project helper and does not claim production readiness
  or a support SLA.

## Package compatibility

The compatible official package repository release is
`Kasterfly/SyKit-Packages` tag `1.0.0`. Official add-ons declare
`sykit-before: 2.0.0`, so a future breaking major refuses them before making
changes.

## Security baseline

The 0.14.2 runtime and CLI security assessment found no critical or high
issues. Its session, task recovery, request handling, scrypt validation,
Windows tool resolution, and generated-file exclusion patches are the runtime
baseline declared stable here.

## Install

Apply the `sykit-1.0.0` package directly to SyKit 0.14.2 with
`--yes --allow-core`. Existing apps need no data migration, reinitialization,
or rebuild because this stable declaration changes no runtime files.
