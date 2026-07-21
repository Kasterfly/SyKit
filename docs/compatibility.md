# SyKit 1.0 Compatibility Contract

SyKit 1.0.0 adopts the contract frozen in 0.14.0 and corrected through 0.14.2.
The stable declaration adds no runtime behavior. Compatible security, bug,
dependency, runtime, and documentation corrections may ship in 1.0.x without
removing or reinterpreting the public surfaces below.

The SyKit 1 stable line is intentionally limited to 1.0.x security, bug,
dependency, runtime-compatibility, and documentation patches. New framework
features and breaking work belong to v2.

## Python API

The public Python API is the names in `sykit.__all__`:

- `Upload`
- `api_key`, `cors`, `expose`, `hidden`, `limits`, `perms`, `raw`, `requires`,
  `sse`, and `web_hook`
- `get_session` and `update_session`
- `enqueue`, `scheduled`, and `task`
- `register_error_hook`
- `util` and `__version__`

The documented `sykit.auth` helpers, `sykit.auth.AuthError`, and the documented
attributes and methods of `Upload` are also public. Underscore-prefixed names
and generated `files/core` modules are internal. A 1.0.x release may not remove,
rename, or reinterpret public names, arguments, return behavior, or documented
exceptions.

## Generated browser API

The generated `$python` module exports one function for every client-visible
`@expose`, `@raw`, and `@sse` top-level function. It also exports `SyKitError`.
`SyKitError.name`, `status`, `payload`, and the generic network status `0` are
public behavior. Hidden endpoint paths and manifest tokens are not
public and may change without notice.

## Static endpoint discovery

Discovery currently recognizes the exact decorator names `expose`, `raw`,
`sse`, `web_hook`, `api_key`, `cors`, `hidden`, `limits`, `perms`, and
`requires`. Decorators must be direct names, route arguments must be literals,
and endpoints and tasks must be top-level functions. The accepted signatures,
injected `session` and `request` names, `Upload` annotation, and build-time
rejections documented in `endpoints.md`, `uploads.md`, and `streaming.md` are
part of the public contract.

## Command line

The documented `init`, `build`, `keys`, `package`, `update`, `version`, and
`help` commands and options are public. Successful commands return status 0;
validation, safety refusal, and runtime failures return a nonzero status. Exact
console wording, whitespace, progress output, and internal helper functions are
not public API.

## Generated application layout

`init` creates the documented source starter and `src/sykit/config.json`.
`build` produces a runnable `built/main.py`, the generated server and core
modules it needs, and compiled static assets under `built/static`. Docker mode
also produces the documented Dockerfile, Compose file, and dockerignore.

Those entry points and configuration locations are public. Hashed asset names,
generated core module names, implementation code, temporary build folders, and
cache layout are internal and may change in a 1.0.x fix.

## Configuration

The keys listed in `configuration.md` are accepted. Unknown top-level keys are
rejected so misspellings cannot silently select a default. Third-party config
must live under the `extensions` object, with a package-owned child name.
The 1.0.x line may correct validation defects but does not add application
features. Removing a key or changing its type or security meaning requires v2.

## Packages

`SyKitPackage.json` accepts `id`, `name`, `desc`, `package-req`, `credit`,
`sykit-req`, `sykit-before`, and `deps`. `sykit-req` is an inclusive minimum;
`sykit-before` is an exclusive maximum. The `add`, `edit`, and `remove` layout
and the edit actions documented in `packages.md` are the public package format.

The 1.0.x line may fix package transactions and record migration, but it will
not reinterpret existing manifest keys or edit actions. Packages for the 1.x
major should declare an upper bound before `2.0.0`.

Installed records under `.packages` are internal recovery state. SyKit will
migrate records it created within the supported major line, but applications
must not edit or consume those files as an API.

## Persistent data

- Signed session cookies use the documented `sykit_session` cookie. Cookie
  bytes are an implementation detail, but compatible 1.0.x releases must keep
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

SyKit 1.0.0 tests Python 3.11, 3.12, 3.13, and 3.14. It accepts and tests LTS
Node.js 22.12+ and 24.x. Odd, end-of-life, and untested future Node lines are
rejected.

Linux runs the full unit, quick-start, browser, and generated-container gates.
Windows runs the supported-version unit and quick-start gates. macOS is best
effort and is not in the release matrix. Support for an environment means its
documented CI gates pass with the locked dependencies. Adding a tested runtime
is compatible; removal from the stable line requires advance notice in the
support policy.
