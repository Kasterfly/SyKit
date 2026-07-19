# SyKit 0.7.0 - Services Update

SyKit 0.7.0 adds an API key layer for machine callers: generation,
validation, scopes, and per-key rate limits. Full guide:
`docs/apikeys.md`.

## Added

- **`@api_key` guard:** require an API key (the `X-API-Key` header) on a
  `@web_hook` endpoint. Bare `@api_key` accepts any active key;
  `@api_key(["scope"])` requires scopes. Missing, unknown, or revoked
  keys answer 401; missing scopes answer 403. Browser-facing endpoints
  stay session-based: `@api_key` is deliberately `@web_hook`-only.
- **`python SyKit keys <generate|list|revoke>`:** manage keys from the
  project root. A key is printed once at generation; only its sha256
  hash is stored, and revocation is immediate.
- **`per-key` rate-limit scope:** each key gets its own bucket in
  `@limits(...)` and `default-limits`; build refuses `per-key` on
  endpoints without `@api_key`.
- **`apikey-store` setting:** keys default to `.sykit-apikeys.sqlite3`
  in the project root (outside `built/`, so issued keys survive
  rebuilds). `sqlite:path` moves the file; `scheme:target` loads a
  store shipped by a package as `files/core/_keystore_<scheme>.py` with
  a `create(target)` function - the same provider convention session
  stores use, and the `keys` command works against any store.

## Upgrade notes

- Existing apps keep working without changes; the new setting defaults
  to the sqlite store and nothing is created until an endpoint uses
  `@api_key`. Rebuild deployed apps so the new runtime lands in
  `built/`, and re-run `python SyKit init` so the updated `sykit/`
  modules are copied into `src/`.
- Keys are bearer credentials: serve keyed endpoints over HTTPS and
  treat keys like passwords.
