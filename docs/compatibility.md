# Compatibility Contract

SyKit 0.13.0 is still a beta. Breaking changes may occur before 1.0, but they
must be called out in the changelog and migration guide. The following list is
the candidate semantic-versioning contract for the 1.0 release candidate.

## Python API

The public Python API is the names in `sykit.__all__`:

- `Upload`
- `api_key`, `cors`, `expose`, `hidden`, `limits`, `perms`, `raw`, `requires`,
  `sse`, and `web_hook`
- `get_session` and `update_session`
- `enqueue`, `scheduled`, and `task`
- `register_error_hook`
- `util` and `__version__`

The documented `sykit.auth` helpers and the documented attributes and methods
of `Upload` are also candidate public API. Underscore-prefixed names are
internal. A 1.x minor release may add optional arguments and exports, but may
not remove or reinterpret existing ones.

## Generated browser API

The generated `$python` module exports one function for every client-visible
`@expose`, `@raw`, and `@sse` top-level function. It also exports `SyKitError`.
`SyKitError.name`, `status`, `payload`, and the generic network status `0` are
candidate public behavior. Hidden endpoint paths and manifest tokens are not
public and may change without notice.

## Static endpoint discovery

Discovery currently recognizes the exact decorator names `expose`, `raw`,
`sse`, `web_hook`, `api_key`, `cors`, `hidden`, `limits`, `perms`, and
`requires`. Decorators must be direct names, route arguments must be literals,
and endpoints and tasks must be top-level functions. The accepted signatures,
injected `session` and `request` names, `Upload` annotation, and build-time
rejections documented in `endpoints.md`, `uploads.md`, and `streaming.md` are
part of the candidate contract.

## Configuration

The keys listed in `configuration.md` are accepted. Unknown top-level keys are
rejected so misspellings cannot silently select a default. Third-party config
must live under the `extensions` object, with a package-owned child name.
Adding a new optional key is compatible; removing a key or changing its type or
security meaning requires a major release after 1.0.

## Packages

`SyKitPackage.json` accepts `id`, `name`, `desc`, `package-req`, `credit`,
`sykit-req`, and `deps`. The `add`, `edit`, and `remove` layout and the edit
actions documented in `packages.md` are the candidate package format.

Installed records under `.packages` are internal recovery state. SyKit will
migrate records it created within the supported major line, but applications
must not edit or consume those files as an API.

## Persistent data

- Signed session cookies use the documented `sykit_session` cookie. Cookie
  bytes are an implementation detail, but compatible 1.x releases must keep
  valid cookies readable or document forced logout as a security migration.
- Password hashes returned by `auth.hash_password` start with `scrypt$` and
  remain verifiable by later compatible releases.
- API key records contain `id`, `name`, `scopes`, `created`, and `revoked`.
  Plaintext keys are never stored.
- Task payloads store a task name, JSON arguments, status, timing, lease, and
  attempt information. Python object serialization is not supported.
- Built-in SQLite stores record independent versions in
  `sykit_schema_versions`. A newer unknown version fails closed. Compatible
  releases migrate older supported versions before serving data.

## Supported environments

SyKit 0.13.0 tests Python 3.10, 3.11, 3.12, 3.13, and 3.14. It tests Node.js
20.19, 22.12, and 24. Support for an environment means the locked install,
unit suite, and quick-start build pass in CI. Removal of an environment from a
stable major line requires advance notice in the support policy.
